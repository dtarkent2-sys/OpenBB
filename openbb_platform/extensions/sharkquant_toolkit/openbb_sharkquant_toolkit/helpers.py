"""Shared helpers for the finance_toolkit extension.

A single Toolkit is built per request (no cross-request caching) so
requests stay independent and the OpenBB API stays stateless. Callers
pass symbols + date window; we return DataFrames as list[Data] for
OpenBB's serialization.
"""
from __future__ import annotations

import datetime as _dt
import math
import os
from typing import Any, Optional

from openbb_core.provider.abstract.data import Data


def get_fmp_api_key() -> str:
    """Return the FMP key from env. Raise if missing — FinanceToolkit
    can't do anything without it."""
    key = os.environ.get("FMP_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "FinanceToolkit endpoints require FMP_API_KEY in the environment."
        )
    return key


def default_start_date() -> str:
    """~18 months back. Enough to compute TTM ratios + 1y performance."""
    return (_dt.date.today() - _dt.timedelta(days=540)).isoformat()


def build_toolkit(
    symbols: list[str] | str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    quarterly: bool = True,
):
    """Construct a FinanceToolkit Toolkit backed by FMP."""
    # pylint: disable=import-outside-toplevel
    from financetoolkit import Toolkit

    if isinstance(symbols, str):
        tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        tickers = [s.strip().upper() for s in symbols if s and s.strip()]
    if not tickers:
        raise ValueError("No symbols provided")

    return Toolkit(
        tickers=tickers,
        api_key=get_fmp_api_key(),
        start_date=start_date or default_start_date(),
        end_date=end_date,
        quarterly=quarterly,
        enforce_source="FinancialModelingPrep",
        progress_bar=False,
    )


def _safe(v: Any) -> Any:
    """Normalize values so pydantic Data() serializes cleanly.

    Drops NaN/inf (OpenBB's JSON serializer chokes on them) and coerces
    numpy/pandas scalars to native Python."""
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    # numpy scalars
    if hasattr(v, "item"):
        try:
            x = v.item()
            if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
                return None
            return x
        except (ValueError, TypeError):
            return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def df_to_data(df, *, value_column: str = "value") -> list[Data]:
    """Convert a FinanceToolkit DataFrame result to list[Data].

    FT returns three common shapes:
      1. Scalar Series (one symbol → one column per date)
      2. DataFrame indexed by metric, columns = dates (one symbol)
      3. DataFrame indexed by (symbol, metric), columns = dates (multi)

    We normalize to records [{symbol, metric, date, value}] so Workspace
    can render any of them as a table/chart.
    """
    # pylint: disable=import-outside-toplevel
    import pandas as pd

    if df is None:
        return []

    # Series → 1-col DataFrame
    if isinstance(df, pd.Series):
        df = df.to_frame(name=value_column)

    records: list[dict] = []

    if isinstance(df.index, pd.MultiIndex):
        for idx, row in df.iterrows():
            sym = str(idx[0]) if len(idx) > 1 else None
            metric = str(idx[-1])
            for col, val in row.items():
                records.append(
                    {
                        "symbol": sym,
                        "metric": metric,
                        "date": str(col),
                        "value": _safe(val),
                    }
                )
    else:
        # Index is either metric names or dates
        for idx, row in df.iterrows():
            if hasattr(row, "items"):
                for col, val in row.items():
                    records.append(
                        {
                            "metric": str(idx),
                            "date": str(col),
                            "value": _safe(val),
                        }
                    )
            else:
                records.append(
                    {
                        "metric": str(idx),
                        "value": _safe(row),
                    }
                )

    return [Data(**r) for r in records]


def series_to_data(result, *, metric: str) -> list[Data]:
    """Convert a FinanceToolkit performance/risk result (typically a
    per-symbol Series or scalar) to list[Data]."""
    # pylint: disable=import-outside-toplevel
    import pandas as pd

    if result is None:
        return []

    if isinstance(result, (int, float)):
        return [Data(metric=metric, value=_safe(result))]

    if isinstance(result, pd.Series):
        return [
            Data(symbol=str(k), metric=metric, value=_safe(v))
            for k, v in result.items()
        ]

    if isinstance(result, pd.DataFrame):
        # Collapse: if single row, it's one per-symbol vector; if index is
        # dates, emit timeseries.
        records: list[dict] = []
        for idx, row in result.iterrows():
            for col, val in row.items():
                records.append(
                    {
                        "symbol": str(col),
                        "metric": metric,
                        "date": str(idx),
                        "value": _safe(val),
                    }
                )
        return [Data(**r) for r in records]

    return [Data(metric=metric, value=_safe(result))]
