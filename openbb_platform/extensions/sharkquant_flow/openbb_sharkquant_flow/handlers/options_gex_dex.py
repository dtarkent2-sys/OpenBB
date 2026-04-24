"""options_gex_dex — per-strike dealer GEX + DEX, computed from Theta v3 greeks.

Replaces upstream analytics.openbb.superquant.com/options_gex_dex. Pulls real
greeks + open interest from Theta Terminal (OPRA Pro) instead of relying on
Black-Scholes approximations.

Dealer convention used here (dealers long calls / short puts to the street):
    call_GEX = +gamma × OI × 100 × spot²
    put_GEX  = −gamma × OI × 100 × spot²
    call_DEX = +delta × OI × 100 × spot
    put_DEX  = −delta × OI × 100 × spot

Flip the sign at the caller if you want the street-side (retail-long) view.
"""

# NOTE: no `from __future__ import annotations` — pydantic's OpenAPI generator
# can't resolve `OBBject[list[Data]]` as a ForwardRef, which crashes openbb-api
# on startup. Keep annotations as real types on any handler registered via
# @router.command.

from typing import Optional

from openbb_core.app.model.obbject import OBBject
from openbb_core.provider.abstract.data import Data

from openbb_sharkquant_flow.spot_price import get_spot_with_source
from openbb_sharkquant_flow.theta_client import theta_rows


def _latest(data_list: Optional[list]) -> dict:
    if not data_list:
        return {}
    last = data_list[-1]
    return last if isinstance(last, dict) else {}


def _signed_gex(gamma: float, oi: float, spot: float, right: str) -> float:
    sign = 1.0 if right == "CALL" else -1.0
    return sign * gamma * oi * 100.0 * spot * spot


def _signed_dex(delta: float, oi: float, spot: float, right: str) -> float:
    sign = 1.0 if right == "CALL" else -1.0
    return sign * delta * oi * 100.0 * spot


def options_gex_dex(
    symbol: str,
    expiration: str,
) -> OBBject[list[Data]]:
    """Per-strike dealer gamma + delta exposure for one expiration.

    Args:
        symbol: Underlying ticker (e.g. "SPY", "AAPL").
        expiration: Option expiration as YYYYMMDD (e.g. "20260516").

    Returns one Data per contract plus a final TOTAL row aggregating both
    sides. Fields: symbol, expiration, strike, right, gamma, delta,
    open_interest, implied_vol, gex, dex, spot.
    """
    greeks_rows = theta_rows(
        "v3/option/snapshot/greeks/all",
        {"symbol": symbol, "expiration": expiration},
    )
    oi_rows = theta_rows(
        "v3/option/snapshot/open_interest",
        {"symbol": symbol, "expiration": expiration},
    )

    oi_by_key: dict = {}
    for row in oi_rows:
        contract = row.get("contract") or {}
        latest = _latest(row.get("data"))
        strike = contract.get("strike")
        right = (contract.get("right") or "").upper()
        oi_by_key[(strike, right)] = (
            latest.get("open_interest") or latest.get("oi") or 0
        )

    spot, spot_source = get_spot_with_source(symbol)
    if spot is None:
        obb: OBBject[list[Data]] = OBBject(results=[])
        try:
            obb.extra["error"] = {
                "code": "spot_price_unavailable",
                "message": (
                    f"Could not resolve a spot price for '{symbol}' from any "
                    "source (openbb_fmp loopback, alpha_vantage_global, "
                    "alpha_vantage_bulk). GEX/DEX requires a live underlying "
                    "price and cannot be computed without one."
                ),
                "hint": (
                    "Verify ALPHAVANTAGE_API_KEY is set in the MCP server's "
                    "environment, or that an openbb-api sibling with a valid "
                    "FMP_API_KEY is reachable at http://localhost:$PORT."
                ),
            }
        except Exception:  # noqa: BLE001
            pass
        return obb

    out: list[Data] = []
    total_call_gex = 0.0
    total_put_gex = 0.0
    total_call_dex = 0.0
    total_put_dex = 0.0

    for row in greeks_rows:
        contract = row.get("contract") or {}
        latest = _latest(row.get("data"))
        strike = contract.get("strike")
        right = (contract.get("right") or "").upper()
        gamma = latest.get("gamma")
        delta = latest.get("delta")
        if strike is None or gamma is None or delta is None:
            continue
        oi = oi_by_key.get((strike, right), 0) or 0
        try:
            gamma_f = float(gamma)
            delta_f = float(delta)
            oi_f = float(oi)
        except (TypeError, ValueError):
            continue

        gex = _signed_gex(gamma_f, oi_f, spot, right)
        dex = _signed_dex(delta_f, oi_f, spot, right)
        if right == "CALL":
            total_call_gex += gex
            total_call_dex += dex
        else:
            total_put_gex += gex
            total_put_dex += dex

        out.append(
            Data(
                symbol=symbol,
                expiration=expiration,
                strike=float(strike),
                right=right,
                gamma=gamma_f,
                delta=delta_f,
                open_interest=int(oi_f),
                implied_vol=latest.get("implied_vol"),
                gex=gex,
                dex=dex,
                spot=spot,
                spot_source=spot_source,
            )
        )

    out.append(
        Data(
            symbol=symbol,
            expiration=expiration,
            strike=None,
            right="TOTAL",
            gex=total_call_gex + total_put_gex,
            dex=total_call_dex + total_put_dex,
            call_gex=total_call_gex,
            put_gex=total_put_gex,
            call_dex=total_call_dex,
            put_dex=total_put_dex,
            spot=spot,
            spot_source=spot_source,
        )
    )
    return OBBject(results=out)
