"""SharkQuant Financial Ratios Model — reads from fmp_bulk where
bulk_name='ratios-ttm-bulk'.

We reuse FMP's data shape (FMPFinancialRatiosData) because our Postgres
rows are literal passthroughs of the FMP bulk payloads — same camelCase
keys, same TTM suffixes, same aliases. This keeps us in lockstep with
OpenBB Workspace's widget expectations without duplicating ~400 lines
of field definitions.
"""

# pylint: disable=unused-argument

from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.financial_ratios import (
    FinancialRatiosQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from openbb_fmp.models.financial_ratios import FMPFinancialRatiosData

from openbb_sharkquant.utils.db import fetch_bulk_rows


class SharkQuantFinancialRatiosQueryParams(FinancialRatiosQueryParams):
    """SharkQuant Financial Ratios Query.

    Reads the latest TTM snapshot from ratios-ttm-bulk. The `limit`
    parameter from the standard model is accepted but ignored — Postgres
    only retains the most recent TTM row per symbol."""

    __json_schema_extra__ = {"symbol": {"multiple_items_allowed": True}}


class SharkQuantFinancialRatiosFetcher(
    Fetcher[
        SharkQuantFinancialRatiosQueryParams,
        list[FMPFinancialRatiosData],
    ]
):
    """Fetches TTM ratios from fmp_bulk snapshots of ratios-ttm-bulk."""

    @staticmethod
    def transform_query(
        params: dict[str, Any],
    ) -> SharkQuantFinancialRatiosQueryParams:
        return SharkQuantFinancialRatiosQueryParams(**params)

    @staticmethod
    async def aextract_data(
        query: SharkQuantFinancialRatiosQueryParams,
        credentials: dict[str, str] | None,
        **kwargs: Any,
    ) -> list[dict]:
        symbols = [s.strip().upper() for s in (query.symbol or "").split(",") if s.strip()]
        if not symbols:
            raise EmptyDataError("No symbols provided")

        rows = fetch_bulk_rows(credentials, bulk_name="ratios-ttm-bulk", symbols=symbols)
        payloads = [row.get("data") or {} for row in rows]
        payloads = [p for p in payloads if p]
        if not payloads:
            raise EmptyDataError(f"No ratios-ttm-bulk rows for {symbols}")
        return payloads

    @staticmethod
    def transform_data(
        query: SharkQuantFinancialRatiosQueryParams,
        data: list[dict],
        **kwargs: Any,
    ) -> list[FMPFinancialRatiosData]:
        return [FMPFinancialRatiosData.model_validate(d) for d in data]
