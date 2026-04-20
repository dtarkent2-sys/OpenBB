"""SharkQuant Cash Flow Growth Model — reads from fmp_bulk where
bulk_name='cash-flow-statement-growth-bulk'."""

# pylint: disable=unused-argument

from typing import Any, Literal

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.cash_flow_growth import (
    CashFlowStatementGrowthQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from openbb_fmp.models.cash_flow_growth import FMPCashFlowStatementGrowthData
from pydantic import Field

from openbb_sharkquant.utils.db import fetch_bulk_rows


class SharkQuantCashFlowStatementGrowthQueryParams(CashFlowStatementGrowthQueryParams):
    """SharkQuant Cash Flow Statement Growth Query."""

    __json_schema_extra__ = {"symbol": {"multiple_items_allowed": True}}

    period: Literal["annual", "quarter"] = Field(
        default="annual", description="Reporting period (annual or quarter)."
    )


class SharkQuantCashFlowStatementGrowthFetcher(
    Fetcher[
        SharkQuantCashFlowStatementGrowthQueryParams,
        list[FMPCashFlowStatementGrowthData],
    ]
):
    """Fetches cash-flow-growth rows from Postgres snapshots."""

    @staticmethod
    def transform_query(
        params: dict[str, Any],
    ) -> SharkQuantCashFlowStatementGrowthQueryParams:
        return SharkQuantCashFlowStatementGrowthQueryParams(**params)

    @staticmethod
    async def aextract_data(
        query: SharkQuantCashFlowStatementGrowthQueryParams,
        credentials: dict[str, str] | None,
        **kwargs: Any,
    ) -> list[dict]:
        symbols = [s.strip().upper() for s in (query.symbol or "").split(",") if s.strip()]
        if not symbols:
            raise EmptyDataError("No symbols provided")
        rows = fetch_bulk_rows(
            credentials,
            bulk_name="cash-flow-statement-growth-bulk",
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
            raise EmptyDataError(f"No cash-flow-statement-growth-bulk rows for {symbols}")
        return payloads

    @staticmethod
    def transform_data(
        query: SharkQuantCashFlowStatementGrowthQueryParams,
        data: list[dict],
        **kwargs: Any,
    ) -> list[FMPCashFlowStatementGrowthData]:
        return [FMPCashFlowStatementGrowthData.model_validate(d) for d in data]
