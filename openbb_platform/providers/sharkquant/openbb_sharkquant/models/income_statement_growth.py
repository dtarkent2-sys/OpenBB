"""SharkQuant Income Statement Growth Model — reads from fmp_bulk where
bulk_name='income-statement-growth-bulk'."""

# pylint: disable=unused-argument

from typing import Any, Literal

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.income_statement_growth import (
    IncomeStatementGrowthQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from openbb_fmp.models.income_statement_growth import FMPIncomeStatementGrowthData
from pydantic import Field

from openbb_sharkquant.utils.db import fetch_bulk_rows


class SharkQuantIncomeStatementGrowthQueryParams(IncomeStatementGrowthQueryParams):
    """SharkQuant Income Statement Growth Query."""

    __json_schema_extra__ = {"symbol": {"multiple_items_allowed": True}}

    period: Literal["annual", "quarter"] = Field(
        default="annual", description="Reporting period (annual or quarter)."
    )


class SharkQuantIncomeStatementGrowthFetcher(
    Fetcher[
        SharkQuantIncomeStatementGrowthQueryParams,
        list[FMPIncomeStatementGrowthData],
    ]
):
    """Fetches income-statement-growth rows from Postgres snapshots."""

    @staticmethod
    def transform_query(
        params: dict[str, Any],
    ) -> SharkQuantIncomeStatementGrowthQueryParams:
        return SharkQuantIncomeStatementGrowthQueryParams(**params)

    @staticmethod
    async def aextract_data(
        query: SharkQuantIncomeStatementGrowthQueryParams,
        credentials: dict[str, str] | None,
        **kwargs: Any,
    ) -> list[dict]:
        symbols = [s.strip().upper() for s in (query.symbol or "").split(",") if s.strip()]
        if not symbols:
            raise EmptyDataError("No symbols provided")
        rows = fetch_bulk_rows(
            credentials,
            bulk_name="income-statement-growth-bulk",
            symbols=symbols,
            latest_only=False,
        )
        payloads: list[dict] = []
        for row in rows:
            data = row.get("data") or {}
            period = (data.get("period") or "").upper()
            if query.period == "annual" and period != "FY":
                continue
            if query.period == "quarter" and period not in {"Q1", "Q2", "Q3", "Q4"}:
                continue
            payloads.append(data)
        payloads.sort(key=lambda d: d.get("date") or "", reverse=True)
        if query.limit:
            payloads = payloads[: query.limit]
        if not payloads:
            raise EmptyDataError(f"No income-statement-growth-bulk rows for {symbols}")
        return payloads

    @staticmethod
    def transform_data(
        query: SharkQuantIncomeStatementGrowthQueryParams,
        data: list[dict],
        **kwargs: Any,
    ) -> list[FMPIncomeStatementGrowthData]:
        return [FMPIncomeStatementGrowthData.model_validate(d) for d in data]
