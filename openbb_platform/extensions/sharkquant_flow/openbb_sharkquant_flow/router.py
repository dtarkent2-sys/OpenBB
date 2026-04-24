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

import inspect
import os
import textwrap
import threading
import time
from collections import defaultdict
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


def _looks_like_plotly(payload: Any) -> bool:
    """True when `payload` is a Plotly figure spec (has `data` as a list of
    trace dicts AND typically a `layout`).

    Used by P4 chart-vs-data split to decide whether to unwrap.
    """
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not (isinstance(data, list) and data and isinstance(data[0], dict)):
        return False
    # Plotly traces have a `type` key (scatter / bar / heatmap / etc.). If
    # even one trace lacks it the payload is probably our own tabular shape
    # pretending to be a figure.
    return any("type" in trace or ("x" in trace and "y" in trace) for trace in data)


def _plotly_to_data(figure: dict) -> list[Data]:
    """Extract a data-shaped rows list from a Plotly figure spec.

    For each trace, zips its x/y arrays into per-point rows stamped with
    the trace name and type. Heatmaps (z array) get flattened to
    (trace, row_idx, col_idx, value) rows. Unknown shapes pass through
    with the raw trace dict so the caller can still inspect them.
    """
    out: list[Data] = []
    for trace in figure.get("data", []):
        if not isinstance(trace, dict):
            continue
        tname = trace.get("name") or trace.get("type") or "trace"
        ttype = trace.get("type", "scatter")
        xs = trace.get("x")
        ys = trace.get("y")
        zs = trace.get("z")
        # Heatmap / 2-D surface.
        if isinstance(zs, list) and zs and isinstance(zs[0], (list, tuple)):
            for r_idx, row in enumerate(zs):
                for c_idx, val in enumerate(row):
                    out.append(Data(
                        trace=tname, type=ttype,
                        row=r_idx, col=c_idx, value=val,
                    ))
            continue
        # Scatter / line / bar — zip xs/ys.
        if isinstance(xs, list) and isinstance(ys, list):
            for x, y in zip(xs, ys):
                out.append(Data(trace=tname, type=ttype, x=x, y=y))
            continue
        # Unknown trace shape — pass through the primary keys so caller
        # can still inspect.
        out.append(Data(
            trace=tname, type=ttype,
            **{k: v for k, v in trace.items() if k not in ("data", "layout")},
        ))
    return out


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


def _error_obbject(
    err: UpstreamError,
    schema: Optional[dict] = None,
) -> OBBject[list[Data]]:
    """Pack an UpstreamError into an OBBject whose top-level error field is
    visible to MCP clients and whose results is an empty list, not a row
    with an error string hiding inside it.

    When `schema` is supplied AND the error looks like a validation failure
    (upstream 422, unknown-apollo-product, or upstream_application_error),
    the input schema is echoed on the error dict so callers can self-correct
    in one round-trip instead of having to fetch /openapi.json separately.
    This implements P3 from the MCP refactor work order.
    """
    obb: OBBject[list[Data]] = OBBject(results=[])
    err_dict = err.as_dict()
    if schema is not None and _is_validation_error(err):
        err_dict["schema"] = schema
        # Nudge the caller toward the self-correct path.
        if not err_dict.get("hint"):
            err_dict["hint"] = (
                "Retry the call with arguments matching the `schema` field above. "
                "Required parameters are listed under `schema.required`."
            )
    try:
        obb.extra["error"] = err_dict
    except Exception:  # noqa: BLE001
        pass
    return obb


def _is_validation_error(err: UpstreamError) -> bool:
    """True when the error is caused by bad/missing/unknown arguments,
    not by network failure or true upstream-internal error.

    Schema-echo only makes sense for validation-class errors — echoing
    the schema on a DNS failure wastes tokens without helping the caller.
    """
    if err.code in (
        "unknown_apollo_product",
        "upstream_application_error",
    ):
        return True
    if err.code == "upstream_http_error" and err.upstream_status in (400, 422):
        return True
    return False


def _build_schema(
    required: tuple[str, ...],
    optional: tuple[str, ...],
) -> dict:
    """Build a JSON Schema describing a tool's inputs from its param tuples.

    Mirrors what FastAPI/pydantic already expose via /openapi.json, but the
    MCP protocol doesn't give callers easy access to that doc from inside
    an error response. So we build a compact inline version here and attach
    it to errors where the caller needs to self-correct.

    Types come from _PARAM_TYPES; defaults and descriptions flow through.
    """
    properties: dict[str, dict] = {}
    for p in (*required, *optional):
        py_type, default, desc = _PARAM_TYPES[p]
        type_name = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
        }.get(py_type, "string")
        prop: dict = {"type": type_name, "description": desc}
        if default is not None:
            prop["default"] = default
        properties[p] = prop
    schema: dict = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = list(required)
    return schema


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

# Each endpoint spec is:
#   (route_suffix, upstream_path, required_params, optional_params, description)
# Params are grouped in a tuple; the typed factory turns them into a real
# function signature with proper types pulled from _PARAM_TYPES. Sourced from
# /openapi.json on analytics.openbb.superquant.com 2026-04-24.

_EQUITY_FLOW = [
    ("equity_flow_daily", "equity_flow_table_daily",
     ("symbol",), ("start_date", "end_date", "columns"),
     "Daily institutional + retail equity flow table (net buy/sell USD per ticker)."),
    ("equity_flow_1min", "equity_flow_table_1min",
     ("symbol",), ("start_date", "end_date", "columns", "include_extended_hours"),
     "1-minute institutional + retail equity flow for a trading day."),
    ("flow_chart", "flow_chart",
     (), ("symbol", "start_date", "end_date", "flow_method", "detrend_window",
          "detrend_method", "zscore_window", "regression_method"),
     "Daily equity flow chart time-series for a ticker."),
    ("flow_chart_intraday", "flow_chart_intraday",
     (), ("symbol", "start_date", "end_date", "date", "flow_method", "interval",
          "include_extended_hours", "zscore_window", "regression_method"),
     "Intraday equity flow chart for a ticker."),
    ("sector_flow_chart", "sector_flow_chart",
     (), ("index", "sector", "start_date", "end_date", "flow_method",
          "detrend_window", "detrend_method", "zscore_window", "regression_method"),
     "Aggregated equity flow by GICS sector."),
    ("etf_flow_chart", "etf_flow_chart",
     (), ("sector", "start_date", "end_date", "flow_method", "detrend_window",
          "detrend_method", "zscore_window", "regression_method"),
     "ETF flow chart time-series."),
    ("flow_accuracy", "flow_accuracy", (), (),
     "Flow-prediction accuracy table (global, no params)."),
    ("top_flow_daily", "top_flow_daily_events",
     (), ("start_date", "end_date", "flow_method", "rank_by", "zscore_window",
          "median_threshold"),
     "Largest daily flow events across the universe, ranked by flow magnitude."),
    ("top_flow_cumulative", "top_flow_cumulative",
     (), ("start_date", "end_date", "flow_method"),
     "Cumulative top-flow aggregates across the universe."),
]

_SHORT_INTEREST = [
    ("short_interest_factor", "short_interest_factor_chart",
     ("symbol",),
     ("start_date", "end_date", "flow_type", "lookback", "zscore_window"),
     "Daily short-interest factor chart for a ticker."),
    ("short_interest_factor_intraday", "short_interest_factor_intraday",
     ("symbol",),
     ("start_date", "end_date", "date", "flow_type", "lookback", "interval",
      "include_extended_hours", "zscore_window"),
     "Intraday short-interest factor chart for a ticker."),
]

_COT_FUTURES = [
    ("apollo_positioning", "apollo_positioning_dashboard",
     ("product",), ("format",),
     "Apollo-vs-COT futures positioning dashboard. `product` must be a canonical upstream label; aliases (ES, SPY, GC, …) are resolved automatically — see apollo_list_products."),
    ("apollo_positioning_extremes", "apollo_positioning_extremes",
     ("product",), ("format",),
     "Apollo futures positioning extremes. Same product-alias rules as apollo_positioning."),
    ("cot_summary_disag", "cot_summary_disag_dashboard",
     (), ("product", "product_name", "category", "report_type", "format"),
     "CFTC COT disaggregated summary dashboard."),
    ("cot_detail_disag", "cot_detail_disag_dashboard",
     (), ("product", "product_name", "category", "report_type", "format"),
     "CFTC COT disaggregated detail dashboard."),
]

_MACRO = [
    ("macro_surprises", "macro_surprises_dashboard",
     (), ("symbol", "identifier", "start_datetime", "end_datetime", "rounded",
          "futures_symbols", "frequency", "format"),
     "Macro surprise index dashboard (Citi-surprise-index class)."),
    ("macro_predictions", "macro_predictions_dashboard",
     (), ("symbol", "view", "start_datetime", "end_datetime", "overlays", "format"),
     "Macro predictions dashboard with optional overlay series."),
    ("macro_consensus", "macro_consensus_dashboard",
     (), ("identifier", "symbol", "view", "start_datetime", "end_datetime",
          "show_actual", "consensus_view", "format"),
     "Macro consensus dashboard comparing forecasts against actual prints."),
    ("macro_revisions", "macro_revisions_dashboard",
     (), ("symbol", "identifier", "start_datetime", "end_datetime",
          "show_residuals", "show_abs_residuals", "rounded", "show_all_buckets", "format"),
     "Macro forecast revisions dashboard."),
]

_PRICE = [
    ("price_chart", "price_chart", (),
     ("symbol", "start_date", "end_date"),
     "Daily price chart time-series."),
    ("price_chart_intraday", "price_chart_intraday", (),
     ("symbol", "start_date", "end_date", "date", "include_extended_hours"),
     "Intraday price chart time-series."),
]

_INELASTICITY = [
    ("market_inelasticity_summary", "market_inelasticity_summary",
     (), ("format",),
     "Market inelasticity summary (Koijen/Gabaix demand-system top-level view)."),
    ("market_inelasticity_timeseries", "market_inelasticity_timeseries",
     ("ticker",), ("metric", "format"),
     "Market inelasticity time-series for a single ticker."),
    ("market_inelasticity_scatter", "market_inelasticity_scatter",
     ("ticker",), ("stage", "format"),
     "Market inelasticity scatter by stage (stage_1 / stage_2 / stage_3)."),
    ("market_inelasticity_cross_sectional", "market_inelasticity_cross_sectional",
     (), ("metric", "format"),
     "Market inelasticity cross-sectional snapshot across tickers."),
]

_REFERENCE = [
    ("security_master", "security_master",
     ("symbol",), (),
     "Equity universe security master row for a single symbol."),
]

# Options suite. Upstream uses an /api/ prefix — kept on the upstream_path side
# so route_suffix stays bare (no /api/ in client-visible paths).
# options_gex_dex is intentionally absent from this table — it has a locally
# computed handler below against Theta Terminal + FMP (see handlers/).
_OPTIONS_SNAPSHOT = ("symbol", "asof_date")  # both required
_OPTIONS_STRIKE_BAND = ("model", "cp_filter", "strike_min", "strike_max")
_OPTIONS_EXPIRATION_BAND = ("model", "cp_filter", "max_expiration_date")
_OPTIONS_INTRADAY = ("model", "cp_filter", "freq")
_OPTIONS_INTRADAY_BREAKDOWN = ("model", "cp_filter", "freq",
                               "expiration_filter", "strike_filter")

_OPTIONS = [
    ("options_cp_ratio", "api/options_cp_ratio",
     _OPTIONS_SNAPSHOT, _OPTIONS_STRIKE_BAND,
     "Call/put ratio snapshot (per-strike or aggregate)."),
    ("options_cross_section", "api/options_cross_section",
     _OPTIONS_SNAPSHOT, _OPTIONS_STRIKE_BAND,
     "Per-strike options cross-section (OI, volume, IV, greeks) snapshot."),
    ("options_expiration_heatmap", "api/options_expiration_heatmap",
     _OPTIONS_SNAPSHOT, _OPTIONS_EXPIRATION_BAND,
     "Open-interest / volume heatmap across strike × expiration."),
    ("options_greek_cross_section", "api/options_greek_cross_section",
     _OPTIONS_SNAPSHOT, _OPTIONS_STRIKE_BAND,
     "Per-strike greeks (delta/gamma/vega/theta) cross-section."),
    ("options_greek_exposure_by_expiration",
     "api/options_greek_exposure_by_expiration",
     _OPTIONS_SNAPSHOT, _OPTIONS_EXPIRATION_BAND,
     "Dealer greek exposure bucketed by expiration."),
    ("options_intraday_cum_premium", "api/options_intraday_cum_premium",
     _OPTIONS_SNAPSHOT, _OPTIONS_INTRADAY,
     "Intraday cumulative premium (call-vs-put split)."),
    ("options_intraday_cum_premium_breakdown",
     "api/options_intraday_cum_premium_breakdown",
     _OPTIONS_SNAPSHOT, _OPTIONS_INTRADAY_BREAKDOWN,
     "Intraday cumulative premium by expiration / strike bucket."),
    ("options_intraday_cumflow", "api/options_intraday_cumflow",
     _OPTIONS_SNAPSHOT, _OPTIONS_INTRADAY,
     "Intraday cumulative options net flow."),
    ("options_intraday_cumflow_breakdown",
     "api/options_intraday_cumflow_breakdown",
     _OPTIONS_SNAPSHOT, _OPTIONS_INTRADAY_BREAKDOWN,
     "Intraday cumulative net flow by expiration / strike bucket."),
    ("options_intraday_delta_flow", "api/options_intraday_delta_flow",
     _OPTIONS_SNAPSHOT, _OPTIONS_INTRADAY,
     "Intraday delta-weighted options flow."),
    ("options_intraday_delta_flow_breakdown",
     "api/options_intraday_delta_flow_breakdown",
     _OPTIONS_SNAPSHOT, _OPTIONS_INTRADAY_BREAKDOWN,
     "Intraday delta-weighted flow by expiration / strike bucket."),
    ("options_intraday_gamma_flow", "api/options_intraday_gamma_flow",
     _OPTIONS_SNAPSHOT, _OPTIONS_INTRADAY,
     "Intraday gamma-weighted options flow."),
    ("options_intraday_gamma_flow_breakdown",
     "api/options_intraday_gamma_flow_breakdown",
     _OPTIONS_SNAPSHOT, _OPTIONS_INTRADAY_BREAKDOWN,
     "Intraday gamma-weighted flow by expiration / strike bucket."),
    ("options_intraday_greek_flow", "api/options_intraday_greek_flow",
     _OPTIONS_SNAPSHOT, _OPTIONS_INTRADAY,
     "Combined intraday greek-weighted flow series."),
    ("options_intraday_vega_flow", "api/options_intraday_vega_flow",
     _OPTIONS_SNAPSHOT, _OPTIONS_INTRADAY,
     "Intraday vega-weighted options flow."),
    ("options_intraday_vega_flow_breakdown",
     "api/options_intraday_vega_flow_breakdown",
     _OPTIONS_SNAPSHOT, _OPTIONS_INTRADAY_BREAKDOWN,
     "Intraday vega-weighted flow by expiration / strike bucket."),
    ("options_iv_smile", "api/options_iv_smile",
     _OPTIONS_SNAPSHOT,
     ("model", "cp_filter", "expiration", "strike_min", "strike_max"),
     "Implied-volatility smile across strikes for one expiration."),
    ("options_kpi_metrics", "api/options_kpi_metrics",
     _OPTIONS_SNAPSHOT, _OPTIONS_STRIKE_BAND,
     "Headline options KPI metrics (gamma flip, max pain, zero-gamma, etc.)."),
    ("options_net_flow_by_expiration", "api/options_net_flow_by_expiration",
     _OPTIONS_SNAPSHOT, _OPTIONS_EXPIRATION_BAND,
     "Net options flow bucketed by expiration."),
    ("options_price_greeks", "api/options_price_greeks",
     _OPTIONS_SNAPSHOT, ("expiration", "strike", "cp_filter", "model"),
     "Time-series of price + greeks for a single contract (symbol + expiration + strike)."),
    ("options_top_contracts", "api/options_top_contracts",
     _OPTIONS_SNAPSHOT, _OPTIONS_STRIKE_BAND,
     "Top contracts by volume / OI / premium."),
    ("options_volume_by_expiration", "api/options_volume_by_expiration",
     _OPTIONS_SNAPSHOT, _OPTIONS_EXPIRATION_BAND,
     "Options volume bucketed by expiration."),
    ("options_volume_by_strike", "api/options_volume_by_strike",
     _OPTIONS_SNAPSHOT, _OPTIONS_STRIKE_BAND,
     "Options volume bucketed by strike."),
]

_ALL_ENDPOINTS = (
    _EQUITY_FLOW + _SHORT_INTEREST + _COT_FUTURES + _MACRO + _PRICE +
    _INELASTICITY + _REFERENCE + _OPTIONS
)


# =============================================================================
# Typed proxy factory.
#
# Each endpoint gets a handler with ONLY the params upstream actually accepts,
# typed to real Python types, with a real docstring. Spec entries are
# (suffix, path, required, optional, description) where required/optional are
# iterables of parameter names; types come from _PARAM_TYPES. The factory
# synthesizes a function whose __signature__ / __annotations__ match the
# spec, so Router.command + FastAPI generate the correct openapi schema and
# pydantic validates before we touch the upstream.
#
# This replaces the previous "28-param union passthrough" where every tool
# advertised every param regardless of what its upstream needed — which
# flooded the MCP tool-list with useless schema, forced agents to guess
# which params mattered, and wasted tokens.
# =============================================================================


# Parameter → (python_type, default, description). Sourced from
# analytics.openbb.superquant.com's openapi + operational knowledge. Upstream
# doesn't document enums for model/cp_filter/freq so they stay as str with
# documented defaults; P1-follow-up is to probe each endpoint and promote to
# Literal[...] once the valid set is confirmed per-endpoint.
_PARAM_TYPES: dict[str, tuple[type, Any, str]] = {
    # Common
    "symbol": (str, None,
               "Ticker / underlying (e.g. 'SPY', 'AAPL', 'NVDA')."),
    "asof_date": (str, None,
                  "Snapshot date, YYYY-MM-DD. Uses latest trading day when omitted."),
    "start_date": (str, None, "Range start, YYYY-MM-DD."),
    "end_date": (str, None, "Range end, YYYY-MM-DD."),
    "date": (str, None, "Single intraday date, YYYY-MM-DD."),
    # Options-specific
    "model": (str, "m1",
              "Internal model variant. Defaults to 'm1'. Valid: 'm1' (confirmed); other variants exist but aren't catalogued upstream — file a follow-up probe if you need them."),
    "cp_filter": (str, "All",
                  "Call/put filter. Defaults to 'All'. Common: 'All' | 'Call' | 'Put'."),
    "freq": (str, None,
             "Sampling frequency for intraday series. Common: '1min' | '5min' | '15min' | '1h'."),
    "expiration": (str, None, "Option expiration, YYYY-MM-DD."),
    "expiration_filter": (str, None,
                          "Expiration bucket filter (upstream-defined string)."),
    "strike": (float, None, "Strike price (for single-contract endpoints)."),
    "strike_min": (float, None, "Lower bound strike filter."),
    "strike_max": (float, None, "Upper bound strike filter."),
    "strike_filter": (str, None,
                      "Strike bucket filter (upstream-defined string)."),
    "max_expiration_date": (str, None,
                            "Only include expirations up to this date, YYYY-MM-DD."),
    # Equity flow
    "columns": (str, None, "Comma-separated column list to restrict output."),
    "upper_threshold": (float, None, "Upper bound for flow-band shading."),
    "lower_threshold": (float, None, "Lower bound for flow-band shading."),
    "use_kalman_filter": (bool, None, "Apply Kalman smoothing to residuals."),
    "add_buy_shade": (bool, None, "Shade buy-signal bands on the chart."),
    "add_sell_shade": (bool, None, "Shade sell-signal bands on the chart."),
    "residual_regression_window": (int, None,
                                   "Rolling window size for residual regression."),
    "detrend_regression_window": (int, None,
                                  "Rolling window size for detrend regression."),
    "flow_cols": (str, None, "Comma-separated flow columns to emit."),
    "include_extended_hours": (bool, None,
                               "Include 4am-8pm ET extended session bars."),
    "flow_method": (str, None,
                    "Flow construction method. Common: 'retail_vs_inst' | 'raw' | 'netted'."),
    "detrend_window": (int, None, "Detrending rolling window size."),
    "detrend_method": (str, None,
                       "Detrending strategy. Common: 'rolling' | 'ewma' | 'none'."),
    "zscore_window": (int, None, "Window for z-score normalization."),
    "regression_method": (str, None,
                          "Regression estimator. Common: 'ols' | 'kalman' | 'ridge'."),
    "interval": (str, None,
                 "Intraday bar interval. Common: '1min' | '5min' | '15min' | '1h'."),
    "rank_by": (str, None,
                "Event ranking metric. Common: 'flow_usd' | 'zscore' | 'abs_flow'."),
    "median_threshold": (float, None,
                         "Minimum median flow threshold for event inclusion."),
    "sector": (str, None,
               "Sector filter. Use the 11 GICS L1 names (e.g. 'Technology')."),
    "index": (str, None,
              "Parent index scope. Common: 'SP500' | 'Nasdaq100' | 'Russell3000'."),
    # Short interest
    "flow_type": (str, None,
                  "Short-interest factor variant. Common: 'daily' | 'intraday_proxy'."),
    "lookback": (int, None, "Rolling lookback window (trading days)."),
    # Apollo / COT futures
    "product": (str, None,
                "Apollo product label. Accepts aliases (ES, SPY, GC, CL, ...) — see apollo_list_products for the full 133-value catalog."),
    "product_name": (str, None,
                     "Legacy COT product name (use when upstream rejects 'product')."),
    "report_type": (str, None,
                    "COT report variant. Common: 'disaggregated' | 'legacy' | 'tff'."),
    "category": (str, None,
                 "Trader category filter. Common: 'commercial' | 'managed_money' | 'non_reportable'."),
    "identifier": (str, None, "Internal numeric identifier (upstream-scoped)."),
    "signal": (str, None, "Signal sub-selection (upstream-scoped)."),
    # Inelasticity
    "ticker": (str, None, "Ticker symbol for inelasticity analysis."),
    "metric": (str, None,
               "Inelasticity metric. Common: 'r_squared' | 'alpha' | 'beta'."),
    "stage": (str, None,
              "Inelasticity stage. Common: 'stage_1' | 'stage_2' | 'stage_3'."),
    # Macro
    "view": (str, None,
             "Macro view selector. Common: 'actual' | 'predicted' | 'surprise'."),
    "start_datetime": (str, None, "Range start, ISO 8601."),
    "end_datetime": (str, None, "Range end, ISO 8601."),
    "show_actual": (bool, None, "Overlay actual release values on chart."),
    "consensus_view": (str, None, "Consensus slicing. Common: 'mean' | 'median'."),
    "overlays": (str, None, "Comma-separated overlay series names."),
    "show_residuals": (bool, None, "Overlay prediction residuals."),
    "show_abs_residuals": (bool, None, "Overlay absolute-value residuals."),
    "rounded": (bool, None, "Round numeric outputs to 2 decimals."),
    "show_all_buckets": (bool, None, "Include all histogram buckets."),
    "futures_symbols": (str, None, "Comma-separated futures symbols to overlay."),
    "frequency": (str, None, "Release frequency. Common: 'daily' | 'weekly' | 'monthly' | 'quarterly'."),
    # P4 — output format control. Dashboard endpoints upstream return a full
    # Plotly figure spec (~12KB per call) with the actual values buried in
    # figure.data[N].y arrays. format='data' (default) extracts structured
    # rows; format='plotly' passes the raw figure through for callers that
    # need to render it directly. ~10x token savings on affected tools.
    "format": (str, "data",
               "Output format: 'data' returns structured rows (default, recommended). 'plotly' returns the raw Plotly figure JSON for direct rendering."),
}


def _make_typed_proxy(
    upstream_path: str,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    description: str,
) -> Callable:
    """Build a proxy handler with a signature that matches the upstream contract.

    Uses exec() to synthesize a function because FastAPI/pydantic introspect
    the actual signature + annotations to build openapi schemas. You can't
    just set __signature__ at runtime — the schema generator walks the real
    parameter names and defaults. Exec is fine here: the template is fixed
    and the spec is under our control.
    """
    # Validate spec — every named param must have a type entry.
    for p in (*required, *optional):
        if p not in _PARAM_TYPES:
            raise RuntimeError(
                f"router spec for /{upstream_path}: param '{p}' has no _PARAM_TYPES entry"
            )

    # Build signature string. Required params come first (no default), then
    # optional (each with its documented default which may be None).
    sig_parts: list[str] = []
    for p in required:
        py_type, _default, _desc = _PARAM_TYPES[p]
        sig_parts.append(f"{p}: {py_type.__name__}")
    for p in optional:
        py_type, default, _desc = _PARAM_TYPES[p]
        if default is None:
            sig_parts.append(f"{p}: Optional[{py_type.__name__}] = None")
        elif isinstance(default, str):
            sig_parts.append(f"{p}: {py_type.__name__} = {default!r}")
        else:
            sig_parts.append(f"{p}: {py_type.__name__} = {default!r}")
    sig = ", ".join(sig_parts)

    # Per-param docstring block
    arg_docs = []
    for p in (*required, *optional):
        _pyt, _def, pdesc = _PARAM_TYPES[p]
        arg_docs.append(f"        {p}: {pdesc}")
    arg_doc_block = "\n".join(arg_docs) if arg_docs else "        (none)"

    doc = (
        f'"""{description}\n\n'
        f'    Proxies GET {_DEFAULT_UPSTREAM}/{upstream_path}.\n'
        f'    Upstream errors surface at extra.error with status/body/hint.\n\n'
        f'    Args:\n{arg_doc_block}\n'
        f'    """'
    )

    # Pre-build the input schema once. Closure captures it so _execute_proxy
    # can echo it on validation-class errors without rebuilding per-call.
    schema = _build_schema(required, optional)

    locals_list = ", ".join(f'"{p}": {p}' for p in (*required, *optional))
    src = textwrap.dedent(f"""
    def _handler({sig}) -> OBBject[list[Data]]:
        {doc}
        _params = {{{locals_list}}}
        return _execute_proxy({upstream_path!r}, _params, _schema)
    """)

    ns: dict[str, Any] = {
        "OBBject": OBBject,
        "Data": Data,
        "Optional": Optional,
        "_execute_proxy": _execute_proxy,
        "_schema": schema,
    }
    exec(src, ns)  # noqa: S102 — trusted template, no user input
    fn = ns["_handler"]
    fn.__name__ = f"sharkquant_flow_{upstream_path.replace('/', '_').lstrip('_')}"
    # Stash the schema on the function for later introspection (e.g. by the
    # P2 catalog meta-tools) without having to re-derive from __annotations__.
    fn.__sq_input_schema__ = schema
    return fn


# =============================================================================
# P5 — per-call stats. Every _execute_proxy call stamps this in-memory table
# so `sharkquant_flow_health` can summarize the top offenders without external
# monitoring infra. Thread-safe because openbb-api serves handlers via thread
# pool; defaultdict access under GIL is fine for the counters here but we
# guard the occasional dict-resize with a lock.
# =============================================================================
_HEALTH_LOCK = threading.Lock()
_CALL_STATS: dict[str, dict] = defaultdict(lambda: {
    "total": 0,
    "success": 0,
    "errors_by_code": defaultdict(int),
    "last_latency_ms": None,
    "last_error_code": None,
    "last_success_at": None,
    "last_error_at": None,
})


def _record_call(upstream_path: str, ok: bool, error_code: Optional[str], latency_ms: int) -> None:
    """Record one call's outcome. Called from _execute_proxy's finally path."""
    with _HEALTH_LOCK:
        row = _CALL_STATS[upstream_path]
        row["total"] += 1
        row["last_latency_ms"] = latency_ms
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if ok:
            row["success"] += 1
            row["last_success_at"] = now_iso
        else:
            row["errors_by_code"][error_code or "unknown"] += 1
            row["last_error_code"] = error_code
            row["last_error_at"] = now_iso


def _execute_proxy(
    upstream_path: str,
    params: dict,
    schema: Optional[dict] = None,
) -> OBBject[list[Data]]:
    """Shared runtime body: apollo-alias resolution, upstream call, error wrap.

    When `schema` is supplied, validation-class errors (upstream 422,
    unknown-apollo-product, upstream application errors) include the input
    schema in their error response so the caller can self-correct without
    a second round-trip to /openapi.json.
    """
    # Apollo positioning requires a canonical upstream product label; resolve
    # common aliases (ES/SPY/GC/...) before forwarding. Unknown inputs still
    # pass through so exotic markets remain reachable.
    if upstream_path in ("apollo_positioning_dashboard",
                         "apollo_positioning_extremes"):
        product = params.get("product")
        if product:
            resolved = _resolve_apollo_product(product)
            if resolved is None:
                return _error_obbject(
                    UpstreamError(
                        code="unknown_apollo_product",
                        message=f"'{product}' is not a recognized apollo product.",
                        hint=(
                            "Call apollo_list_products to see the 133-value catalog. "
                            "Accepted aliases include: ES, SPY, SPX, NQ, QQQ, YM, "
                            "RTY, CL, NG, GC, SI, HG, ZC, ZS, ZW, 10Y, 2Y, 5Y, 30Y, "
                            "EUR, GBP, JPY, BTC, ETH."
                        ),
                    ),
                    schema=schema,
                )
            params["product"] = resolved

    # P4: pop the `format` param off before forwarding — upstream doesn't
    # know it; it controls how we shape the response on the way back.
    fmt = params.pop("format", "data") if "format" in params else "data"

    # P5: time the whole operation (including apollo resolution done above)
    # and stamp the call stats.
    t0 = time.monotonic()
    err_code: Optional[str] = None
    try:
        try:
            payload = _call_upstream(upstream_path, params)
        except UpstreamError as err:
            err_code = err.code
            return _error_obbject(err, schema=schema)

        if fmt == "plotly":
            return OBBject(results=_to_data(payload))
        if _looks_like_plotly(payload):
            return OBBject(results=_plotly_to_data(payload))
        return OBBject(results=_to_data(payload))
    finally:
        latency_ms = int((time.monotonic() - t0) * 1000)
        _record_call(upstream_path, ok=err_code is None, error_code=err_code,
                     latency_ms=latency_ms)


for route_suffix, upstream_path, required, optional, description in _ALL_ENDPOINTS:
    handler = _make_typed_proxy(upstream_path, required, optional, description)
    handler.__name__ = route_suffix  # route path = function name under @router.command
    # Build a realistic example — if upstream requires symbol+asof_date we
    # supply both so the openapi example actually validates.
    example_params = {}
    if "symbol" in required:
        example_params["symbol"] = "SPY"
    if "asof_date" in required:
        example_params["asof_date"] = "2026-04-23"
    if "ticker" in required:
        example_params["ticker"] = "AAPL"
    if "product" in required:
        example_params["product"] = "ES"
    if not example_params:
        example_params["symbol"] = "AAPL"
    router.command(
        methods=["GET"],
        examples=[APIEx(parameters=example_params)],
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


# =============================================================================
# P2 — Progressive discovery catalog (sharkquant_catalog_list/schema/call).
#
# Three meta-tools that let callers discover every sharkquant_flow_* route
# without blowing the MCP tool-list budget. The token economics win:
# listing names + short descriptions is ~20x cheaper than loading every
# tool's full schema upfront.
#
# Scope limitation: this catalog covers routes registered on THIS extension's
# router. Full cross-extension catalog (spanning every OpenBB router) requires
# changes to the MCP server's tool-registration filtering — flagged as
# follow-up; the surface here establishes the pattern and proves it with the
# ~50 sharkquant_flow tools.
# =============================================================================


def _iter_catalog_entries():
    """Yield (name, endpoint_fn, description) for every registered route."""
    for route in router.api_router.routes:
        fn = getattr(route, "endpoint", None)
        if fn is None:
            continue
        name = getattr(fn, "__name__", None) or str(getattr(route, "path", "?"))
        # Strip any inherited "sharkquant_flow_" prefix added by the factory
        # so catalog-list names match the tool names callers actually use.
        pretty = name.replace("sharkquant_flow_api_", "").replace("sharkquant_flow_", "")
        # Exclude the catalog tools themselves and other meta-routes so the
        # caller can't spider into /list from /call.
        if pretty.startswith("catalog_") or pretty.startswith("_"):
            continue
        desc = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        yield pretty, fn, desc


def _lookup_catalog_fn(tool_name: str):
    """Return the endpoint callable for `tool_name` or None."""
    for pretty, fn, _desc in _iter_catalog_entries():
        if pretty == tool_name:
            return fn
    return None


def _schema_for_fn(fn: Callable) -> dict:
    """Return a JSON Schema for fn's parameters.

    Prefers the pre-built schema stashed on fn.__sq_input_schema__ by
    _make_typed_proxy (zero cost). Falls back to deriving from signature
    for tools that weren't generated via the typed factory (options_gex_dex,
    apollo_list_products, the catalog tools themselves).
    """
    prebuilt = getattr(fn, "__sq_input_schema__", None)
    if isinstance(prebuilt, dict):
        return prebuilt
    # Fallback: derive from signature.
    sig = inspect.signature(fn)
    props: dict = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        annot = param.annotation
        origin = getattr(annot, "__origin__", None)
        # Unwrap Optional[X] → X, nullable=True.
        nullable = False
        if origin is not None and annot.__class__.__name__ in ("_UnionGenericAlias", "UnionType"):
            args = [a for a in getattr(annot, "__args__", ()) if a is not type(None)]
            if args and type(None) in getattr(annot, "__args__", ()):
                nullable = True
            if len(args) == 1:
                annot = args[0]
        type_name = {
            str: "string", int: "integer", float: "number", bool: "boolean",
        }.get(annot, "string")
        prop: dict = {"type": type_name}
        if nullable:
            prop["nullable"] = True
        if param.default is not inspect.Parameter.empty and param.default is not None:
            prop["default"] = param.default
        props[pname] = prop
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    schema: dict = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def sharkquant_catalog_list() -> OBBject[list[Data]]:
    """List every sharkquant_flow tool (names + one-line descriptions only).

    **This returns names and descriptions, NOT full parameter schemas.** You
    MUST call sharkquant_catalog_schema(tool_name) to retrieve the input
    schema before calling sharkquant_catalog_call. Calling catalog_call
    without first fetching the schema will fail because you won't know the
    required parameters.

    Workflow: catalog_list → catalog_schema(name) → catalog_call(name, args).

    Note: the first-class sharkquant_flow_* tools (options_*, equity_flow_*,
    apollo_positioning, market_inelasticity_*, macro_*) are still directly
    callable by name — this catalog is the fallback discovery surface for
    callers that want to browse the full extension without loading every
    tool's schema into context.
    """
    entries = [
        Data(name=name, description=desc)
        for name, _fn, desc in sorted(_iter_catalog_entries(), key=lambda x: x[0])
    ]
    obb: OBBject[list[Data]] = OBBject(results=entries)
    try:
        obb.extra["total"] = len(entries)
        obb.extra["workflow_hint"] = (
            "call sharkquant_catalog_schema(tool_name) to get the input schema, "
            "then sharkquant_catalog_call(tool_name, arguments) to execute"
        )
    except Exception:  # noqa: BLE001
        pass
    return obb


def sharkquant_catalog_schema(tool_name: str) -> OBBject[list[Data]]:
    """Get the full input-parameter JSON Schema for a catalog tool.

    Args:
      tool_name: The exact tool name returned by sharkquant_catalog_list.

    Returns a single Data row with the JSON Schema describing types,
    defaults, required fields, and per-param descriptions.
    """
    fn = _lookup_catalog_fn(tool_name)
    if fn is None:
        return _error_obbject(UpstreamError(
            code="tool_not_found",
            message=f"'{tool_name}' is not a sharkquant_flow catalog tool.",
            hint="Call sharkquant_catalog_list to see available tools.",
        ))
    schema = _schema_for_fn(fn)
    desc = (fn.__doc__ or "").strip()
    return OBBject(results=[Data(
        tool_name=tool_name,
        description=desc,
        input_schema=schema,
    )])


def sharkquant_catalog_call(tool_name: str, arguments: Optional[str] = None) -> OBBject[list[Data]]:
    """Dispatch a call to any sharkquant_flow catalog tool.

    Args:
      tool_name: The exact tool name from sharkquant_catalog_list.
      arguments: JSON-encoded object of arguments matching the schema from
                 sharkquant_catalog_schema(tool_name). Example:
                 '{"symbol": "SPY", "asof_date": "2026-04-23"}'.

    On validation failure the error response includes the full input schema
    (via the P3 schema-echo path) so you can self-correct without a separate
    schema fetch.

    IMPORTANT: You should call sharkquant_catalog_schema(tool_name) first
    to retrieve the parameter contract before calling this tool. Catalog
    call accepts JSON-encoded arguments as a single string param because
    the MCP protocol pattern requires dispatch tools to have a single
    generic `arguments` slot.
    """
    import json as _json
    fn = _lookup_catalog_fn(tool_name)
    if fn is None:
        return _error_obbject(UpstreamError(
            code="tool_not_found",
            message=f"'{tool_name}' is not a sharkquant_flow catalog tool.",
            hint="Call sharkquant_catalog_list to see available tools.",
        ))
    # Parse arguments JSON. Empty / missing is treated as {} so tools with
    # all-optional params work without the caller having to pass '{}'.
    if arguments is None or arguments == "":
        parsed_args: dict = {}
    else:
        try:
            parsed_args = _json.loads(arguments)
        except _json.JSONDecodeError as e:
            return _error_obbject(
                UpstreamError(
                    code="invalid_arguments_json",
                    message=f"`arguments` must be a JSON-encoded object: {e}",
                    hint='Example: \'{"symbol": "SPY", "asof_date": "2026-04-23"}\'',
                ),
                schema=_schema_for_fn(fn),
            )
        if not isinstance(parsed_args, dict):
            return _error_obbject(
                UpstreamError(
                    code="invalid_arguments_json",
                    message="`arguments` must decode to a JSON object, not an array/scalar.",
                ),
                schema=_schema_for_fn(fn),
            )
    # Validate required params are present up front so the error names the
    # missing field instead of surfacing a raw TypeError from fn(**kwargs).
    schema = _schema_for_fn(fn)
    missing = [p for p in schema.get("required", []) if p not in parsed_args]
    if missing:
        return _error_obbject(
            UpstreamError(
                code="missing_required_arguments",
                message=f"Missing required arguments: {missing}",
            ),
            schema=schema,
        )
    # Dispatch.
    try:
        return fn(**parsed_args)
    except TypeError as e:
        # Unknown kwarg — surface as validation error with schema.
        return _error_obbject(
            UpstreamError(
                code="invalid_arguments",
                message=str(e),
            ),
            schema=schema,
        )


router.command(methods=["GET"])(sharkquant_catalog_list)
router.command(methods=["GET"])(sharkquant_catalog_schema)
router.command(methods=["GET"])(sharkquant_catalog_call)


# =============================================================================
# P5 — health snapshot. Returns per-tool call counts, success rate, last
# latency, and error-code histogram since the process started. Lightweight
# enough for an external cron to hit every 60s without impact.
# =============================================================================


def sharkquant_flow_health(
    min_calls: int = 0,
    errors_only: bool = False,
) -> OBBject[list[Data]]:
    """Per-tool health snapshot since process start.

    Args:
      min_calls: Filter to tools that have been called at least this many
                 times. Default 0 (include all observed tools).
      errors_only: When True, only return tools with at least one error.

    Returns one row per observed upstream_path with total calls, success
    count, success rate, latest error code, last-latency_ms, last-success
    and last-error timestamps, and an errors-by-code sub-dict.

    Intended for ops: an external cron pings this endpoint every N seconds
    and alerts on rows where success_rate drops below threshold or where
    the last-error-at is newer than last-success-at for extended windows.
    """
    rows: list[Data] = []
    with _HEALTH_LOCK:
        for path, stats in sorted(_CALL_STATS.items()):
            total = stats.get("total", 0)
            if total < min_calls:
                continue
            errs = dict(stats.get("errors_by_code") or {})
            if errors_only and not errs:
                continue
            success = stats.get("success", 0)
            rows.append(Data(
                tool=path,
                total=total,
                success=success,
                error=total - success,
                success_rate=round(success / total, 4) if total else None,
                last_latency_ms=stats.get("last_latency_ms"),
                last_error_code=stats.get("last_error_code"),
                last_success_at=stats.get("last_success_at"),
                last_error_at=stats.get("last_error_at"),
                errors_by_code=errs,
            ))
    obb: OBBject[list[Data]] = OBBject(results=rows)
    try:
        obb.extra["observed_tool_count"] = len(_CALL_STATS)
        obb.extra["note"] = (
            "Stats are in-memory and reset on process restart. For persistent "
            "monitoring run this on an external cron and pipe into your TSDB."
        )
    except Exception:  # noqa: BLE001
        pass
    return obb


router.command(methods=["GET"])(sharkquant_flow_health)
