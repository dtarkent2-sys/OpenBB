"""Spot price fetch via OpenBB's own equity.price.quote endpoint.

We call the loopback openbb-api so OpenBB's FMP provider handles the
quote internally. Direct FMP `/api/v3/quote*` calls 403 on our key tier
but OpenBB's FMP provider uses a different entitled surface that works
with the same key.

This also aligns us with how the stock-screener already reads quotes,
picks up OpenBB's caching, and keeps the extension dependency-free
(stdlib urllib only).
"""

import json
import os
import urllib.parse
import urllib.request
from typing import Optional


def get_spot(symbol: str) -> Optional[float]:
    """Return last_price for `symbol` via the local openbb-api, or None."""
    port = os.environ.get("PORT", "6900")
    url = (
        f"http://localhost:{port}/api/v1/equity/price/quote"
        f"?symbol={urllib.parse.quote(symbol)}&provider=fmp"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    results = payload.get("results") if isinstance(payload, dict) else None
    if results and isinstance(results, list) and isinstance(results[0], dict):
        row = results[0]
        price = row.get("last_price") or row.get("price")
        if price is not None:
            try:
                return float(price)
            except (TypeError, ValueError):
                return None
    return None
