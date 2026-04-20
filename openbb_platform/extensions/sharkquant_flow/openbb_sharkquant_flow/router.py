"""SharkQuant Flow Router — proxies the SuperQuant analytics backend.

Goal: give Billy, Arena, the Workspace, and any other client a single
SharkQuant backend to talk to instead of each maintaining its own
connection to SuperQuant / Exponential. This extension registers
programmatic proxy endpoints under /api/v1/sharkquant_flow/* that
forward GET requests upstream and return the raw JSON payload.

Default upstream: analytics.openbb.superquant.com (cleaner single-
method schema than the XTech mirror). Override with the
SHARKQUANT_FLOW_UPSTREAM env var for the XTech variant or a local
Postgres cache.
"""
# pylint: disable=import-outside-toplevel

import os
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from openbb_core.app.router import Router

router = Router(prefix="/sharkquant_flow", description="Proxied flow, COT, and macro analytics.")

_DEFAULT_UPSTREAM = "https://analytics.openbb.superquant.com"


def _upstream() -> str:
    """Resolve the SuperQuant/XTech upstream base URL.

    Env override so we can point at the XTech mirror or a local cache
    without rebuilding. Trailing slash stripped to keep URL joins
    predictable."""
    return os.environ.get("SHARKQUANT_FLOW_UPSTREAM", _DEFAULT_UPSTREAM).rstrip("/")


async def _proxy(path: str, request: Request) -> Any:
    """Forward a GET to the upstream and return its JSON body.

    Pass-through: we don't validate, transform, or repackage — whatever
    the upstream returns (list of rows, single dict, plot spec) goes
    back to the caller with the same Content-Type. Timeouts map to 504
    so Workspace widgets show a clear error instead of hanging.
    """
    import httpx

    url = f"{_upstream()}/{path.lstrip('/')}"
    params = dict(request.query_params)

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail=f"Upstream timeout: {exc}")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream unreachable: {exc}")

    if resp.status_code >= 400:
        # Pass upstream errors through with their status code + body.
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except ValueError:
            return JSONResponse(status_code=resp.status_code, content={"upstream_error": resp.text[:500]})

    # Upstream typically returns JSON; if not, fall back to text wrapping.
    try:
        return resp.json()
    except ValueError:
        return JSONResponse(content={"raw": resp.text})


# ------------------------------------------------------------------
# Catch-all proxy — Billy and internal clients use this freely.
# ------------------------------------------------------------------


@router.api_router.get("/proxy/{endpoint_path:path}", include_in_schema=True)
async def proxy(endpoint_path: str, request: Request):
    """Generic proxy — forwards `GET /sharkquant_flow/proxy/<any-path>?<qs>`
    to the upstream analytics backend. Used for ad-hoc access when you
    don't want a typed wrapper.

    Example:
        GET /sharkquant_flow/proxy/equity_flow_table_daily?symbol=AAPL
    """
    return await _proxy(endpoint_path, request)


# ------------------------------------------------------------------
# Typed wrappers for the highest-value endpoints. These are the paths
# Billy / Arena / Workspace widgets will actually call often, so we
# expose them as first-class routes (cleaner for logs, rate-limiting,
# and future caching). The generic /proxy/ endpoint still covers
# everything else.
# ------------------------------------------------------------------

_PRIMARY_ENDPOINTS = [
    # (route_suffix, upstream_path, description)
    ("equity_flow_daily", "equity_flow_table_daily", "Daily institutional + retail equity flow."),
    ("equity_flow_1min", "equity_flow_table_1min", "Minute-level institutional + retail equity flow."),
    ("flow_chart", "flow_chart", "Equity flow chart time series."),
    ("flow_chart_intraday", "flow_chart_intraday", "Intraday equity flow chart."),
    ("sector_flow_chart", "sector_flow_chart", "Aggregated equity flow by sector."),
    ("etf_flow_chart", "etf_flow_chart", "ETF flow chart time series."),
    ("flow_accuracy", "flow_accuracy", "Flow prediction accuracy table."),
    ("short_interest_factor", "short_interest_factor_chart", "Daily short-interest factor chart."),
    ("short_interest_factor_intraday", "short_interest_factor_intraday", "Intraday short-interest factor."),
    ("apollo_positioning", "apollo_positioning_dashboard", "Apollo-vs-COT futures positioning dashboard."),
    ("apollo_positioning_extremes", "apollo_positioning_extremes", "Apollo positioning extremes."),
    ("cot_summary_disag", "cot_summary_disag_dashboard", "CFTC COT disaggregated summary."),
    ("cot_detail_disag", "cot_detail_disag_dashboard", "CFTC COT disaggregated detail."),
    ("macro_surprises", "macro_surprises_dashboard", "Macro surprise index dashboard."),
    ("macro_predictions", "macro_predictions_dashboard", "Macro predictions dashboard."),
    ("macro_consensus", "macro_consensus_dashboard", "Macro consensus dashboard."),
    ("macro_revisions", "macro_revisions_dashboard", "Macro forecast revisions dashboard."),
    ("security_master", "security_master", "Equity universe security master."),
    ("top_flow_daily", "top_flow_daily_events", "Top flow daily events."),
    ("top_flow_cumulative", "top_flow_cumulative", "Top cumulative flow."),
    ("price_chart", "price_chart", "Daily price chart."),
    ("price_chart_intraday", "price_chart_intraday", "Intraday price chart."),
    ("market_inelasticity_summary", "market_inelasticity_summary", "Market inelasticity summary."),
    ("market_inelasticity_timeseries", "market_inelasticity_timeseries", "Market inelasticity time series."),
    ("market_inelasticity_scatter", "market_inelasticity_scatter", "Market inelasticity scatter."),
    ("market_inelasticity_cross_sectional", "market_inelasticity_cross_sectional", "Market inelasticity cross-sectional."),
]


def _make_typed_proxy(upstream_path: str, description: str):
    async def handler(request: Request):
        return await _proxy(upstream_path, request)

    handler.__name__ = f"sharkquant_flow_{upstream_path}"
    handler.__doc__ = description + (
        f"\n\nProxies GET {_DEFAULT_UPSTREAM}/{upstream_path} — pass upstream "
        "query params as-is."
    )
    return handler


for route_suffix, upstream_path, description in _PRIMARY_ENDPOINTS:
    handler = _make_typed_proxy(upstream_path, description)
    router.api_router.add_api_route(
        f"/{route_suffix}",
        handler,
        methods=["GET"],
        name=f"sharkquant_flow_{route_suffix}",
        summary=description,
        include_in_schema=True,
    )


# ------------------------------------------------------------------
# Widget catalog passthrough — lets a client discover what upstream
# widgets are available without leaving our domain. Workspace can
# consume this if we later wire widgets.json merging.
# ------------------------------------------------------------------


@router.api_router.get("/upstream_widgets", include_in_schema=True)
async def upstream_widgets(request: Request):
    """Returns the upstream's widgets.json so callers can enumerate what's
    available. URLs in the response still point at SuperQuant; we'll
    rewrite them if/when we add Workspace-native widget merging."""
    return await _proxy("widgets.json", request)
