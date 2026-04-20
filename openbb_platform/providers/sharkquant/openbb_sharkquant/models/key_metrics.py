"""SharkQuant Key Metrics Model — reads from fmp_bulk where
bulk_name='key-metrics-ttm-bulk'."""

# pylint: disable=unused-argument

from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.key_metrics import KeyMetricsQueryParams
from openbb_core.provider.utils.errors import EmptyDataError
from openbb_fmp.models.key_metrics import FMPKeyMetricsData

from openbb_sharkquant.utils.db import fetch_bulk_rows


class SharkQuantKeyMetricsQueryParams(KeyMetricsQueryParams):
    """SharkQuant Key Metrics Query — TTM snapshot per symbol."""

    __json_schema_extra__ = {"symbol": {"multiple_items_allowed": True}}


class SharkQuantKeyMetricsFetcher(
    Fetcher[
        SharkQuantKeyMetricsQueryParams,
        list[FMPKeyMetricsData],
    ]
):
    """Fetches TTM key metrics from fmp_bulk snapshots of key-metrics-ttm-bulk."""

    @staticmethod
    def transform_query(
        params: dict[str, Any],
    ) -> SharkQuantKeyMetricsQueryParams:
        return SharkQuantKeyMetricsQueryParams(**params)

    @staticmethod
    async def aextract_data(
        query: SharkQuantKeyMetricsQueryParams,
        credentials: dict[str, str] | None,
        **kwargs: Any,
    ) -> list[dict]:
        symbols = [s.strip().upper() for s in (query.symbol or "").split(",") if s.strip()]
        if not symbols:
            raise EmptyDataError("No symbols provided")
        rows = fetch_bulk_rows(credentials, bulk_name="key-metrics-ttm-bulk", symbols=symbols)
        payloads = [row.get("data") or {} for row in rows]
        payloads = [p for p in payloads if p]
        if not payloads:
            raise EmptyDataError(f"No key-metrics-ttm-bulk rows for {symbols}")
        return payloads

    @staticmethod
    def transform_data(
        query: SharkQuantKeyMetricsQueryParams,
        data: list[dict],
        **kwargs: Any,
    ) -> list[FMPKeyMetricsData]:
        return [FMPKeyMetricsData.model_validate(d) for d in data]
