"""SharkQuant Flow Router — proxies the SuperQuant analytics backend.

Goal: give Billy, Arena, the Workspace, and any other client a single
SharkQuant backend to talk to instead of each maintaining its own
connection to SuperQuant / Exponential. This extension registers
~26 typed proxy endpoints under /api/v1/sharkquant_flow/* that forward
GET requests upstream and return the raw rows as list[Data].

Default upstream: analytics.openbb.superquant.com. Override via the
SHARKQUANT_FLOW_UPSTREAM env var to swap to the XTech mirror, a
self-hosted Postgres cache, or a local dev instance.
"""
# pylint: disable=import-outside-toplevel,unused-argument

import os
from typing import Any, Callable, Optional

from openbb_core.app.model.example import APIEx
from openbb_core.app.model.obbject import OBBject
from openbb_core.app.router import Router
from openbb_core.provider.abstract.data import Data

router = Router(prefix="", description="Proxied flow, COT, and macro analytics from SuperQuant.")

_DEFAULT_UPSTREAM = "https://analytics.openbb.superquant.com"
_TIMEOUT_SEC = 30.0


def _upstream() -> str:
    """Resolve the SuperQuant/XTech upstream base URL (env-overridable)."""
    return os.environ.get("SHARKQUANT_FLOW_UPSTREAM", _DEFAULT_UPSTREAM).rstrip("/")


class UpstreamError(Exception):
    """Raised when the SuperQuant upstream returns an error response.

    Carries enough context (HTTP status, upstream URL, parsed body, hint
    on what to try next) that callers can surface a usable MCP error
    instead of hiding it inside an OBBject results array.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        upstream_status: Optional[int] = None,
        upstream_url: Optional[str] = None,
        upstream_body: Any = None,
        hint: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.upstream_status = upstream_status
        self.upstream_url = upstream_url
        self.upstream_body = upstream_body
        self.hint = hint

    def as_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.upstream_status is not None:
            out["upstream_status"] = self.upstream_status
        if self.upstream_url is not None:
            out["upstream_url"] = self.upstream_url
        if self.upstream_body is not None:
            out["upstream_body"] = self.upstream_body
        if self.hint is not None:
            out["hint"] = self.hint
        return out


def _call_upstream(path: str, params: dict) -> Any:
    """Blocking GET against the upstream, returning parsed JSON.

    Raises UpstreamError on HTTP non-2xx, JSON decode failure, or when the
    upstream returned a success code but an error-shaped body (``{"error": ...}``
    or ``{"detail": [{...}]}``). This replaces the prior pattern of silently
    returning an error-dict that callers often confused with real data.

    Uses stdlib urllib so the extension stays dependency-free at runtime.
    """
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
    qs = urllib.parse.urlencode(clean_params, doseq=True)
    url = f"{_upstream()}/{path.lstrip('/')}" + (f"?{qs}" if qs else "")

    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SEC) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        err_body: Any
        try:
            err_body = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            err_body = None
        hint = None
        # FastAPI 422 bodies look like {"detail": [{"loc": [...], "msg": "..."}]}.
        # Surface the field name + message so the agent can retry without a
        # second trip to the schema.
        if exc.code == 422 and isinstance(err_body, dict) and err_body.get("detail"):
            parts = []
            for item in err_body["detail"][:3]:
                if isinstance(item, dict):
                    field = ".".join(str(x) for x in (item.get("loc") or [])[-2:])
                    parts.append(f"{field}: {item.get('msg')}" if field else str(item.get("msg")))
            if parts:
                hint = "Upstream validation failure: " + "; ".join(parts)
        raise UpstreamError(
            code="upstream_http_error",
            message=f"Upstream returned HTTP {exc.code}: {exc.reason}",
            upstream_status=exc.code,
            upstream_url=url,
            upstream_body=err_body,
            hint=hint,
        )
    except Exception as exc:  # noqa: BLE001 — network / timeout / DNS / etc.
        raise UpstreamError(
            code="upstream_unreachable",
            message=f"Upstream request failed: {exc}",
            upstream_url=url,
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise UpstreamError(
            code="upstream_non_json",
            message="Upstream returned non-JSON body.",
            upstream_url=url,
            upstream_body=body[:500],
        )

    # Upstream sometimes returns 200 with an application-level error object
    # (e.g. {"error": "No COT mapping found for product: ES"}). Unwrap it.
    if isinstance(payload, dict):
        if "error" in payload and "results" not in payload and "data" not in payload:
            raise UpstreamError(
                code="upstream_application_error",
                message=str(payload["error"]),
                upstream_url=url,
                upstream_body=payload,
            )
    return payload


def _to_data(payload: Any) -> list[Data]:
    """Coerce whatever the upstream returned into list[Data].

    Handles three common shapes:
      * list[dict] (tabular) — one Data per row
      * single dict (metadata, scalar responses) — wrapped as one Data
      * anything else (string, null) — wrapped with key 'value'
    """
    if isinstance(payload, list):
        return [Data(**row) if isinstance(row, dict) else Data(value=row) for row in payload]
    if isinstance(payload, dict):
        return [Data(**payload)]
    return [Data(value=payload)]


def _error_obbject(err: UpstreamError) -> OBBject[list[Data]]:
    """Pack an UpstreamError into an OBBject whose top-level error field is
    visible to MCP clients and whose results is an empty list, not a row
    with an error string hiding inside it.
    """
    obb: OBBject[list[Data]] = OBBject(results=[])
    # OBBject has an `extra` dict reserved for metadata — put the full
    # error there, AND raise it as a warning so any client that ignores
    # `extra` still sees something went wrong.
    try:
        obb.extra["error"] = err.as_dict()
    except Exception:  # noqa: BLE001
        pass
    return obb


# =============================================================================
# Apollo product aliases. Upstream expects full-text labels like
# "CME E-MINI S&P 500 FUTURE" from a 133-row catalog
# (/api/available_apollo_positioning_products). No caller passes that by
# hand — they send tickers like "ES" or "SPY". We map the common cases.
# Unknown inputs still get forwarded as-is so exotic markets aren't blocked;
# the proxy factory surfaces a helpful error with the list-tool hint when
# upstream rejects.
# =============================================================================
_APOLLO_PRODUCT_ALIASES: dict[str, str] = {
    # US equity index futures
    "ES": "CME E-MINI S&P 500 FUTURE",
    "SPY": "CME E-MINI S&P 500 FUTURE",
    "SPX": "CME E-MINI S&P 500 FUTURE",
    "MES": "CME MICRO E-MINI S&P 500 FUTURE",
    "NQ": "CME E-MINI NASDAQ 100 INDEX FUTURE",
    "QQQ": "CME E-MINI NASDAQ 100 INDEX FUTURE",
    "MNQ": "CME MICRO E-MINI NASDAQ 100 INDEX FUTURE",
    "YM": "CBOT MINI DOW $5 MULTIPLIER FUTURES",
    "MYM": "CBOT MICRO EMINI DJIA INDEX FUT",
    "DJI": "CBOT MINI DOW $5 MULTIPLIER FUTURES",
    "RTY": "CME E-MINI RUSSELL 2000 INDEX FUTURES",
    "M2K": "CME MICRO E-MINI RUSSELL FUTURE",
    "IWM": "CME E-MINI RUSSELL 2000 INDEX FUTURES",
    # US rates
    "ZN": "CBOT 10Y US TREASURY NOTE FUTURE",
    "10Y": "CBOT 10Y US TREASURY NOTE FUTURE",
    "ZF": "CBOT 5Y US TREASURY NOTE FUTURE",
    "5Y": "CBOT 5Y US TREASURY NOTE FUTURE",
    "ZB": "CBOT 20-YEAR U.S. TREASURY BOND FUTURES",
    "30Y": "CBOT 20-YEAR U.S. TREASURY BOND FUTURES",
    "UB": "CBOT ULTRA T-BOND FUTURES",
    "ZT": "CBOT 2Y US TREASURY NOTE FUTURE",
    "2Y": "CBOT 2Y US TREASURY NOTE FUTURE",
    "ZQ": "CBOT 30D INTEREST RATE (FED FUND) FUTURE",
    "FF": "CBOT 30D INTEREST RATE (FED FUND) FUTURE",
    "SOFR": "CBOT 10-YEAR ERIS SOFR SWAP FUTURE",
    # Energy
    "CL": "NYMEX LIGHT SWEET CRUDE OIL FUTURE",
    "WTI": "NYMEX LIGHT SWEET CRUDE OIL FUTURE",
    "NG": "NYMEX NATURAL GAS (HENRY HUB) FUTURE",
    "HO": "NYMEX NY HARBOR ULSD FUTURE",
    "RB": "NYMEX RBOB GASOLINE FUTURE",
    "BZ": "ICE BRENT CRUDE FUTURES",
    "BRENT": "ICE BRENT CRUDE FUTURES",
    # Metals
    "GC": "COMEX GOLD FUTURES",
    "GOLD": "COMEX GOLD FUTURES",
    "SI": "COMEX SILVER FUTURES",
    "SILVER": "COMEX SILVER FUTURES",
    "HG": "COMEX COPPER FUTURES",
    "COPPER": "COMEX COPPER FUTURES",
    "PL": "NYMEX PLATINUM FUTURE",
    "PA": "NYMEX PALLADIUM FUTURE",
    # Grains
    "ZC": "CBOT CORN FUTURE",
    "CORN": "CBOT CORN FUTURE",
    "ZS": "CBOT SOYBEAN FUTURE",
    "SOYBEAN": "CBOT SOYBEAN FUTURE",
    "ZW": "CBOT HARD RED SPRING WHEAT FUTURE",
    "WHEAT": "CBOT HARD RED SPRING WHEAT FUTURE",
    "ZM": "CBOT SOYBEAN MEAL FUTURE",
    "ZL": "CBOT SOYBEAN OIL FUTURE",
    "ZO": "CBOT OAT FUTURE",
    "ZR": "CBOT CRC ROUGH RICE FUTURE",
    # Softs
    "CC": "ICE COCOA FUTURES",
    "KC": "ICE COFFEE 'C' FUTURES",
    "COFFEE": "ICE COFFEE 'C' FUTURES",
    "SB": "ICE SUGAR NO. 11 FUTURES",
    "SUGAR": "ICE SUGAR NO. 11 FUTURES",
    "CT": "ICE COTTON NO. 2 FUTURES",
    "COTTON": "ICE COTTON NO. 2 FUTURES",
    # Livestock
    "LE": "CME LIVE CATTLE FUTURE",
    "GF": "CME FEEDER CATTLE FUTURE",
    "HE": "CME LEAN HOG FUTURE",
    # FX
    "6E": "CME EURO FX FUTURE",
    "EUR": "CME EURO FX FUTURE",
    "EURUSD": "CME EURO FX FUTURE",
    "6B": "CME BRITISH POUND FUTURE",
    "GBP": "CME BRITISH POUND FUTURE",
    "6J": "CME JAPANESE YEN FUTURE",
    "JPY": "CME JAPANESE YEN FUTURE",
    "6S": "CME SWISS FRANC FUTURE",
    "CHF": "CME SWISS FRANC FUTURE",
    "6A": "CME AUSTRALIAN DOLLAR FUTURE",
    "AUD": "CME AUSTRALIAN DOLLAR FUTURE",
    "6C": "CME CANADIAN DOLLAR FUTURE",
    "CAD": "CME CANADIAN DOLLAR FUTURE",
    "DX": "ICE US DOLLAR INDEX FUTURES",
    "DXY": "ICE US DOLLAR INDEX FUTURES",
    # Crypto
    "BTC": "CME BITCOIN FUTURE",
    "MBT": "CME MICRO BITCOIN FUTURE",
    "ETH": "CME ETHER FUTURE",
    "MET": "CME MICRO ETHER FUTURE",
}


def _resolve_apollo_product(product: str) -> Optional[str]:
    """Map a caller-supplied product string to the canonical upstream label.

    Returns the canonical label if we recognize the alias, else returns the
    original string so upstream can validate it directly. Returns None only
    if the input is obviously malformed (empty after normalization).
    """
    if not product:
        return None
    cleaned = product.strip()
    if not cleaned:
        return None
    # Alias lookup is case-insensitive for convenience.
    upper = cleaned.upper()
    if upper in _APOLLO_PRODUCT_ALIASES:
        return _APOLLO_PRODUCT_ALIASES[upper]
    # Caller may have typed the full upstream label already — pass through.
    return cleaned


# =============================================================================
# Endpoint catalog — name on our side, path on SuperQuant's side, description.
# Adding a new endpoint is one line here — widgets.json generation picks it up.
# =============================================================================

_EQUITY_FLOW = [
    ("equity_flow_daily", "equity_flow_table_daily",
     "Daily institutional + retail equity flow (net buy/sell USD per ticker)."),
    ("equity_flow_1min", "equity_flow_table_1min",
     "1-minute institutional + retail equity flow for a trading day."),
    ("flow_chart", "flow_chart",
     "Equity flow chart time-series."),
    ("flow_chart_intraday", "flow_chart_intraday",
     "Intraday equity flow chart."),
    ("sector_flow_chart", "sector_flow_chart",
     "Aggregated equity flow by sector."),
    ("etf_flow_chart", "etf_flow_chart",
     "ETF flow chart time-series."),
    ("flow_accuracy", "flow_accuracy",
     "Flow-prediction accuracy table."),
    ("top_flow_daily", "top_flow_daily_events",
     "Largest daily flow events across the universe."),
    ("top_flow_cumulative", "top_flow_cumulative",
     "Cumulative top-flow aggregates."),
]

_SHORT_INTEREST = [
    ("short_interest_factor", "short_interest_factor_chart",
     "Daily short-interest factor chart."),
    ("short_interest_factor_intraday", "short_interest_factor_intraday",
     "Intraday short-interest factor chart."),
]

_COT_FUTURES = [
    ("apollo_positioning", "apollo_positioning_dashboard",
     "Apollo-vs-COT futures positioning dashboard."),
    ("apollo_positioning_extremes", "apollo_positioning_extremes",
     "Apollo futures positioning extremes."),
    ("cot_summary_disag", "cot_summary_disag_dashboard",
     "CFTC COT disaggregated summary dashboard."),
    ("cot_detail_disag", "cot_detail_disag_dashboard",
     "CFTC COT disaggregated detail dashboard."),
]

_MACRO = [
    ("macro_surprises", "macro_surprises_dashboard",
     "Macro surprise index dashboard."),
    ("macro_predictions", "macro_predictions_dashboard",
     "Macro predictions dashboard."),
    ("macro_consensus", "macro_consensus_dashboard",
     "Macro consensus dashboard."),
    ("macro_revisions", "macro_revisions_dashboard",
     "Macro forecast revisions dashboard."),
]

_PRICE = [
    ("price_chart", "price_chart", "Daily price chart."),
    ("price_chart_intraday", "price_chart_intraday", "Intraday price chart."),
]

_INELASTICITY = [
    ("market_inelasticity_summary", "market_inelasticity_summary",
     "Market inelasticity summary."),
    ("market_inelasticity_timeseries", "market_inelasticity_timeseries",
     "Market inelasticity time series."),
    ("market_inelasticity_scatter", "market_inelasticity_scatter",
     "Market inelasticity scatter."),
    ("market_inelasticity_cross_sectional", "market_inelasticity_cross_sectional",
     "Market inelasticity cross-sectional."),
]

_REFERENCE = [
    ("security_master", "security_master", "Equity universe security master."),
]

# Upstream uses an /api/ prefix for the options suite (the rest of the analytics
# routes are bare), so options entries embed it in their upstream_path.
# options_gex_dex is intentionally absent — it has a locally-computed handler
# below that registers the same external path against Theta Terminal + FMP.
_OPTIONS = [
    ("options_cp_ratio", "api/options_cp_ratio",
     "Options call/put ratio (per-strike or aggregate)."),
    ("options_cross_section", "api/options_cross_section",
     "Per-strike options cross-section snapshot."),
    ("options_expiration_heatmap", "api/options_expiration_heatmap",
     "Open-interest / volume heatmap across strike × expiration."),
    ("options_greek_cross_section", "api/options_greek_cross_section",
     "Per-strike greeks (delta/gamma/vega/theta) cross-section."),
    ("options_greek_exposure_by_expiration", "api/options_greek_exposure_by_expiration",
     "Aggregated dealer greek exposure bucketed by expiration."),
    ("options_intraday_cum_premium", "api/options_intraday_cum_premium",
     "Intraday cumulative premium (call-vs-put split)."),
    ("options_intraday_cum_premium_breakdown", "api/options_intraday_cum_premium_breakdown",
     "Intraday cumulative premium by expiration / strike bucket."),
    ("options_intraday_cumflow", "api/options_intraday_cumflow",
     "Intraday cumulative options net flow."),
    ("options_intraday_cumflow_breakdown", "api/options_intraday_cumflow_breakdown",
     "Intraday cumulative net flow by expiration / strike bucket."),
    ("options_intraday_delta_flow", "api/options_intraday_delta_flow",
     "Intraday delta-weighted options flow."),
    ("options_intraday_delta_flow_breakdown", "api/options_intraday_delta_flow_breakdown",
     "Intraday delta-weighted flow by expiration / strike bucket."),
    ("options_intraday_gamma_flow", "api/options_intraday_gamma_flow",
     "Intraday gamma-weighted options flow."),
    ("options_intraday_gamma_flow_breakdown", "api/options_intraday_gamma_flow_breakdown",
     "Intraday gamma-weighted flow by expiration / strike bucket."),
    ("options_intraday_greek_flow", "api/options_intraday_greek_flow",
     "Combined intraday greek-weighted flow series."),
    ("options_intraday_vega_flow", "api/options_intraday_vega_flow",
     "Intraday vega-weighted options flow."),
    ("options_intraday_vega_flow_breakdown", "api/options_intraday_vega_flow_breakdown",
     "Intraday vega-weighted flow by expiration / strike bucket."),
    ("options_iv_smile", "api/options_iv_smile",
     "Implied-volatility smile across strikes for an expiration."),
    ("options_kpi_metrics", "api/options_kpi_metrics",
     "Headline options KPI metrics (gamma flip, max pain, etc.)."),
    ("options_net_flow_by_expiration", "api/options_net_flow_by_expiration",
     "Net options flow bucketed by expiration."),
    ("options_price_greeks", "api/options_price_greeks",
     "Time-series of price + greeks for a single contract."),
    ("options_top_contracts", "api/options_top_contracts",
     "Top contracts by volume / OI / premium."),
    ("options_volume_by_expiration", "api/options_volume_by_expiration",
     "Options volume bucketed by expiration."),
    ("options_volume_by_strike", "api/options_volume_by_strike",
     "Options volume bucketed by strike."),
]

_ALL_ENDPOINTS = (
    _EQUITY_FLOW + _SHORT_INTEREST + _COT_FUTURES + _MACRO + _PRICE +
    _INELASTICITY + _REFERENCE + _OPTIONS
)


# =============================================================================
# Factory — every endpoint shares the same flexible query shape.
#
# These endpoints are wildly heterogeneous (symbol, product, sector, date
# range, flow_method, interval, etc.) so rather than hand-write a typed
# Pydantic model per upstream path, we accept the common union of params
# and forward whatever the caller sent.
# =============================================================================


def _make_proxy(upstream_path: str, description: str) -> Callable:
    """Build a handler closing over the upstream path."""

    def handler(
        symbol: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        asof_date: Optional[str] = None,
        product: Optional[str] = None,
        product_name: Optional[str] = None,
        sector: Optional[str] = None,
        index: Optional[str] = None,
        interval: Optional[str] = None,
        freq: Optional[str] = None,
        flow_method: Optional[str] = None,
        detrend_window: Optional[int] = None,
        detrend_method: Optional[str] = None,
        include_extended_hours: Optional[bool] = None,
        report_type: Optional[str] = None,
        lookback: Optional[int] = None,
        category: Optional[str] = None,
        identifier: Optional[str] = None,
        signal: Optional[str] = None,
        model: Optional[str] = None,
        cp_filter: Optional[str] = None,
        expiration: Optional[str] = None,
        expiration_filter: Optional[str] = None,
        strike: Optional[float] = None,
        strike_min: Optional[float] = None,
        strike_max: Optional[float] = None,
        strike_filter: Optional[str] = None,
        max_expiration_date: Optional[str] = None,
    ) -> OBBject[list[Data]]:
        """Auto-generated SuperQuant proxy."""
        params = {
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
            "asof_date": asof_date,
            "product": product,
            "product_name": product_name,
            "sector": sector,
            "index": index,
            "interval": interval,
            "freq": freq,
            "flow_method": flow_method,
            "detrend_window": detrend_window,
            "detrend_method": detrend_method,
            "include_extended_hours": include_extended_hours,
            "report_type": report_type,
            "lookback": lookback,
            "category": category,
            "identifier": identifier,
            "signal": signal,
            "model": model,
            "cp_filter": cp_filter,
            "expiration": expiration,
            "expiration_filter": expiration_filter,
            "strike": strike,
            "strike_min": strike_min,
            "strike_max": strike_max,
            "strike_filter": strike_filter,
            "max_expiration_date": max_expiration_date,
        }

        # Apollo positioning requires a canonical upstream product name like
        # "CME E-MINI S&P 500 FUTURE" (133-value catalog). Resolve common
        # aliases like "ES"/"SPY"/"SPX" to the canonical form before sending.
        if upstream_path in ("apollo_positioning_dashboard",
                             "apollo_positioning_extremes") and product:
            resolved = _resolve_apollo_product(product)
            if resolved is None:
                err = UpstreamError(
                    code="unknown_apollo_product",
                    message=f"'{product}' is not a recognized apollo product.",
                    hint=(
                        "Call /api/v1/sharkquant_flow/apollo_list_products to see the "
                        "full catalog (133 entries), or pass one of the common aliases: "
                        "ES, SPY, SPX, NQ, QQQ, YM, RTY, CL, NG, GC, SI, HG, ZC, ZS, "
                        "ZW, 10Y, 2Y, 5Y, 30Y, EUR, GBP, JPY, BTC, ETH."
                    ),
                )
                return _error_obbject(err)
            params["product"] = resolved

        try:
            payload = _call_upstream(upstream_path, params)
        except UpstreamError as err:
            return _error_obbject(err)
        return OBBject(results=_to_data(payload))

    handler.__name__ = f"sharkquant_flow_{upstream_path}"
    handler.__doc__ = (
        f"{description}\n\nProxies GET {_DEFAULT_UPSTREAM}/{upstream_path}. "
        "Pass upstream query params as-is — unused ones are dropped. "
        "Upstream errors surface at the top-level `extra.error` field with "
        "status, URL, body, and a remediation hint."
    )
    return handler


for route_suffix, upstream_path, description in _ALL_ENDPOINTS:
    handler = _make_proxy(upstream_path, description)
    handler.__name__ = route_suffix  # route path = function name under @router.command
    router.command(
        methods=["GET"],
        examples=[APIEx(parameters={"symbol": "AAPL"})],
    )(handler)


# =============================================================================
# Discovery helpers — small endpoints that let callers learn valid enum
# values (apollo products, COT report types, etc.) without paging through
# documentation. These are cheap to call and replace the "how do I know
# what to pass here?" friction.
# =============================================================================


def apollo_list_products() -> OBBject[list[Data]]:
    """List all 133 canonical upstream product labels for apollo positioning.

    Use the `value` field of each row as the `product` argument to
    sharkquant_flow_apollo_positioning / apollo_positioning_extremes. The
    extension also accepts common aliases (ES, SPY, GC, CL, etc.) — see
    the apollo_positioning docstring.

    Returns:
      list of {label, value} rows.
    """
    try:
        payload = _call_upstream("api/available_apollo_positioning_products", {})
    except UpstreamError as err:
        return _error_obbject(err)
    return OBBject(results=_to_data(payload))


apollo_list_products.__name__ = "apollo_list_products"
router.command(methods=["GET"])(apollo_list_products)


# =============================================================================
# Locally-computed endpoints — compute against Theta Terminal + FMP instead of
# proxying SuperQuant. Each replaces a same-named upstream route so the
# externally visible path /api/v1/sharkquant_flow/<name> stays constant.
# =============================================================================
from openbb_sharkquant_flow.handlers.options_gex_dex import (  # noqa: E402
    options_gex_dex,
)

router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "SPY", "expiration": "20260516"})],
)(options_gex_dex)
