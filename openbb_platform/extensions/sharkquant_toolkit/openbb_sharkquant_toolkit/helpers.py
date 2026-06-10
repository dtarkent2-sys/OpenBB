"""Shared helpers for the finance_toolkit extension.

A Toolkit is built per request, but the underlying Alpha Vantage datasets
are served from an in-process TTL cache (see `av_datasets`), so one
symbol's statements/prices are fetched once — not once per endpoint.
Callers pass symbols + date window; we return DataFrames as list[Data]
for OpenBB's serialization.

FinanceToolkit is fed through its external/custom dataset interface
(`Toolkit(balance=..., income=..., cash=..., historical=...)`) — no FMP
key is required anymore.
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Optional

from openbb_core.provider.abstract.data import Data

from openbb_sharkquant_toolkit.av_datasets import (
    TREASURY_NAME,
    get_av_datasets,
)


def default_start_date() -> str:
    """~18 months back. Enough to compute TTM ratios + 1y performance."""
    return (_dt.date.today() - _dt.timedelta(days=540)).isoformat()


def build_toolkit(
    symbols: list[str] | str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    quarterly: bool = True,
):
    """Construct a FinanceToolkit Toolkit fed by Alpha Vantage datasets.

    The returned Toolkit carries a `_sq_av_meta` dict describing any
    degradations (tickers without fundamentals, missing risk-free data),
    which the router uses to produce actionable error messages.
    """
    # pylint: disable=import-outside-toplevel,protected-access
    from financetoolkit import Toolkit

    if isinstance(symbols, str):
        tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        tickers = [s.strip().upper() for s in symbols if s and s.strip()]
    if not tickers:
        raise ValueError("No symbols provided")

    start = start_date or default_start_date()
    datasets = get_av_datasets(
        tickers,
        quarterly=quarterly,
        start_date=start,
        end_date=end_date,
    )

    has_benchmark = "Benchmark" in datasets["historical"].columns.get_level_values(1)

    toolkit = Toolkit(
        tickers=tickers,
        start_date=start,
        end_date=end_date,
        quarterly=quarterly,
        historical=datasets["historical"],
        balance=datasets["balance"],
        income=datasets["income"],
        cash=datasets["cash"],
        # Custom datasets are already in USD-consistent shape; skip currency
        # conversion and the FMP plan probe (sleep_timer=None would ping FMP).
        convert_currency=False,
        sleep_timer=False,
        reverse_dates=False,
        benchmark_ticker="SPY" if has_benchmark else None,
        progress_bar=False,
    )

    # Inject the Alpha Vantage 10y treasury data so FinanceToolkit never
    # reaches out to FMP/Yahoo for the risk-free rate. The weekly/monthly/
    # quarterly/yearly variants are derived from the daily frame by FT.
    if datasets["treasury"] is not None:
        toolkit._daily_treasury_data = datasets["treasury"]
        toolkit._daily_risk_free_rate = datasets["treasury"].xs(
            TREASURY_NAME, level=1, axis=1
        )

    toolkit._sq_av_meta = {
        "fundamentals_missing": datasets["fundamentals_missing"],
        "notes": datasets["notes"],
    }
    return toolkit


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
