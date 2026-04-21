"""Spot price fetch via FMP — used by locally-computed options endpoints.

FMP_API_KEY is already wired into the openbb-api container by compose
(Discord-bot PR #379). One request per endpoint call, no caching — fine
for POC volumes.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Optional

_FMP_BASE = "https://financialmodelingprep.com/api/v3"


def get_spot(symbol: str) -> Optional[float]:
    """Return the latest FMP spot price for `symbol`, or None on failure."""
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return None
    url = (
        f"{_FMP_BASE}/quote-short/{urllib.parse.quote(symbol)}"
        f"?apikey={urllib.parse.quote(key)}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        price = payload[0].get("price")
        if price is not None:
            try:
                return float(price)
            except (TypeError, ValueError):
                return None
    return None
