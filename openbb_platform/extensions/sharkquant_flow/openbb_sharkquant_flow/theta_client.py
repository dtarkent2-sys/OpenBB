"""Thin stdlib-only Theta Terminal v3 client for locally-computed endpoints.

Theta Terminal runs on the host (Windows), reached from inside the openbb-api
container via http://host.docker.internal:25503 (wired by compose with
extra_hosts: host.docker.internal:host-gateway). Override with env
THETADATA_URL for dev.

Standard v3 response shape for list/snapshot/history endpoints:
    {"header": {...}, "response": [ ... rows ... ]}
472 = "no data found" — not an error, empty result.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_DEFAULT_URL = "http://host.docker.internal:25503"
_TIMEOUT_SEC = 30.0


def _base_url() -> str:
    return os.environ.get("THETADATA_URL", _DEFAULT_URL).rstrip("/")


def theta_get(path: str, params: dict) -> Any:
    """GET a Theta v3 endpoint and return parsed JSON (or an error dict)."""
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    clean.setdefault("format", "json")
    qs = urllib.parse.urlencode(clean, doseq=True)
    url = f"{_base_url()}/{path.lstrip('/')}" + (f"?{qs}" if qs else "")
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SEC) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 472:
            return {"response": []}
        return {"theta_error": f"HTTP {exc.code}", "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"theta_error": str(exc), "url": url}


def theta_rows(path: str, params: dict) -> list[dict]:
    """Unwrap the standard {"response": [...]} envelope; return [] on error."""
    payload = theta_get(path, params)
    if isinstance(payload, dict) and "theta_error" not in payload:
        resp = payload.get("response")
        if isinstance(resp, list):
            return resp
    return []
