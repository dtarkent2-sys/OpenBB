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


def _call_upstream(path: str, params: dict) -> Any:
    """Blocking GET against the upstream, returning parsed JSON.

    Uses stdlib urllib so the extension stays dependency-free at runtime.
    Called from sync handlers — openbb-api serves many requests concurrently
    via thread pool, so a blocking HTTP is fine here.
    """
    import json
    import urllib.parse
    import urllib.request

    clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
    qs = urllib.parse.urlencode(clean_params, doseq=True)
    url = f"{_upstream()}/{path.lstrip('/')}" + (f"?{qs}" if qs else "")

    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SEC) as r:
            body = r.read().decode("utf-8")
    except Exception as exc:  # pragma: no cover
        return {"upstream_error": str(exc), "url": url}

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body[:500]}


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
        payload = _call_upstream(upstream_path, params)
        return OBBject(results=_to_data(payload))

    handler.__name__ = f"sharkquant_flow_{upstream_path}"
    handler.__doc__ = (
        f"{description}\n\nProxies GET {_DEFAULT_UPSTREAM}/{upstream_path}. "
        "Pass upstream query params as-is — unused ones are dropped."
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
