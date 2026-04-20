"""SharkQuant Price Target Consensus Model — reads from fmp_bulk where
bulk_name='price-target-summary-bulk'."""

# pylint: disable=unused-argument

from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.price_target_consensus import (
    PriceTargetConsensusQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from openbb_fmp.models.price_target_consensus import FMPPriceTargetConsensusData

from openbb_sharkquant.utils.db import fetch_bulk_rows


class SharkQuantPriceTargetConsensusQueryParams(PriceTargetConsensusQueryParams):
    """SharkQuant Price Target Consensus Query."""

    __json_schema_extra__ = {"symbol": {"multiple_items_allowed": True}}


class SharkQuantPriceTargetConsensusFetcher(
    Fetcher[
        SharkQuantPriceTargetConsensusQueryParams,
        list[FMPPriceTargetConsensusData],
    ]
):
    """Fetches analyst consensus price targets from Postgres."""

    @staticmethod
    def transform_query(
        params: dict[str, Any],
    ) -> SharkQuantPriceTargetConsensusQueryParams:
        return SharkQuantPriceTargetConsensusQueryParams(**params)

    @staticmethod
    async def aextract_data(
        query: SharkQuantPriceTargetConsensusQueryParams,
        credentials: dict[str, str] | None,
        **kwargs: Any,
    ) -> list[dict]:
        symbols = [s.strip().upper() for s in (query.symbol or "").split(",") if s.strip()]
        if not symbols:
            raise EmptyDataError("No symbols provided")
        rows = fetch_bulk_rows(
            credentials, bulk_name="price-target-summary-bulk", symbols=symbols
        )
        payloads = [row.get("data") or {} for row in rows]
        payloads = [p for p in payloads if p]
        if not payloads:
            raise EmptyDataError(f"No price-target-summary-bulk rows for {symbols}")
        return payloads

    @staticmethod
    def transform_data(
        query: SharkQuantPriceTargetConsensusQueryParams,
        data: list[dict],
        **kwargs: Any,
    ) -> list[FMPPriceTargetConsensusData]:
        return [FMPPriceTargetConsensusData.model_validate(d) for d in data]
