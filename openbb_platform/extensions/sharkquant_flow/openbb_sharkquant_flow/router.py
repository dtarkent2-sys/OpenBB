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

_ALL_ENDPOINTS = (
    _EQUITY_FLOW + _SHORT_INTEREST + _COT_FUTURES + _MACRO + _PRICE +
    _INELASTICITY + _REFERENCE
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
        product: Optional[str] = None,
        product_name: Optional[str] = None,
        sector: Optional[str] = None,
        index: Optional[str] = None,
        interval: Optional[str] = None,
        flow_method: Optional[str] = None,
        detrend_window: Optional[int] = None,
        detrend_method: Optional[str] = None,
        include_extended_hours: Optional[bool] = None,
        report_type: Optional[str] = None,
        lookback: Optional[int] = None,
        category: Optional[str] = None,
        identifier: Optional[str] = None,
        signal: Optional[str] = None,
    ) -> OBBject[list[Data]]:
        """Auto-generated SuperQuant proxy."""
        params = {
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
            "product": product,
            "product_name": product_name,
            "sector": sector,
            "index": index,
            "interval": interval,
            "flow_method": flow_method,
            "detrend_window": detrend_window,
            "detrend_method": detrend_method,
            "include_extended_hours": include_extended_hours,
            "report_type": report_type,
            "lookback": lookback,
            "category": category,
            "identifier": identifier,
            "signal": signal,
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
