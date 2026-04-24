"""Spot price fetch with provider fallbacks.

Tries sources in order:
  1. Loopback openbb-api /equity/price/quote?provider=fmp
     (fast, cached, shares OpenBB's entitled FMP surface when available)
  2. Alpha Vantage GLOBAL_QUOTE
     (direct to AV, works from any host with an API key — does not
     require a sibling openbb-api container)
  3. Alpha Vantage REALTIME_BULK_QUOTES for a 1-symbol batch
     (fallback endpoint in case GLOBAL_QUOTE is rate-limited)

Returns (price, source) so callers can stamp provenance on the response.
Previous single-source behavior caused every gex_dex call to return
``spot_price_unavailable`` when the loopback was unreachable in
deployments that don't run openbb-api alongside the MCP.
"""

import json
import os
import urllib.parse
import urllib.request
from typing import Optional, Tuple


def _try_openbb_loopback(symbol: str) -> Optional[float]:
    port = os.environ.get("PORT", "6900")
    url = (
        f"http://localhost:{port}/api/v1/equity/price/quote"
        f"?symbol={urllib.parse.quote(symbol)}&provider=fmp"
    )
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    results = payload.get("results") if isinstance(payload, dict) else None
    if not (results and isinstance(results, list) and isinstance(results[0], dict)):
        return None
    row = results[0]
    price = row.get("last_price") or row.get("price")
    try:
        return float(price) if price is not None else None
    except (TypeError, ValueError):
        return None


def _try_alpha_vantage_global(symbol: str) -> Optional[float]:
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY") or os.environ.get("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        return None
    url = (
        "https://www.alphavantage.co/query"
        f"?function=GLOBAL_QUOTE&symbol={urllib.parse.quote(symbol)}&apikey={api_key}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    gq = payload.get("Global Quote") or {}
    raw = gq.get("05. price") or gq.get("price")
    if raw in (None, "", "0", "0.0000"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _try_alpha_vantage_bulk(symbol: str) -> Optional[float]:
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY") or os.environ.get("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        return None
    url = (
        "https://www.alphavantage.co/query"
        f"?function=REALTIME_BULK_QUOTES&symbol={urllib.parse.quote(symbol)}&apikey={api_key}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not (data and isinstance(data, list) and isinstance(data[0], dict)):
        return None
    row = data[0]
    raw = row.get("close") or row.get("last_price") or row.get("price")
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def get_spot_with_source(symbol: str) -> Tuple[Optional[float], Optional[str]]:
    """Return (price, source_name) or (None, None) if every source failed.

    Source values: ``openbb_fmp`` | ``alpha_vantage_global`` | ``alpha_vantage_bulk``.
    """
    price = _try_openbb_loopback(symbol)
    if price is not None:
        return price, "openbb_fmp"
    price = _try_alpha_vantage_global(symbol)
    if price is not None:
        return price, "alpha_vantage_global"
    price = _try_alpha_vantage_bulk(symbol)
    if price is not None:
        return price, "alpha_vantage_bulk"
    return None, None


def get_spot(symbol: str) -> Optional[float]:
    """Back-compat wrapper returning just the price. Prefer get_spot_with_source()."""
    price, _ = get_spot_with_source(symbol)
    return price
