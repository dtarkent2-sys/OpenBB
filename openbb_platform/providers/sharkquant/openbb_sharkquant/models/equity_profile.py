"""SharkQuant Equity Profile Model — reads from fmp_bulk where bulk_name='profile-bulk'."""

# pylint: disable=unused-argument

from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.equity_info import (
    EquityInfoData,
    EquityInfoQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from pydantic import ConfigDict

from openbb_sharkquant.utils.db import fetch_bulk_rows


class SharkQuantEquityProfileQueryParams(EquityInfoQueryParams):
    """SharkQuant Equity Profile Query.

    Reads from the most recent profile-bulk snapshot in Postgres."""

    __json_schema_extra__ = {"symbol": {"multiple_items_allowed": True}}


class SharkQuantEquityProfileData(EquityInfoData):
    """SharkQuant Equity Profile Data — shape matches FMP's profile-bulk CSV."""

    # Field name translations: standard model snake_case → FMP camelCase
    __alias_dict__ = {
        "name": "companyName",
        "stock_exchange": "exchange",
        "company_url": "website",
        "hq_address1": "address",
        "hq_address_city": "city",
        "hq_address_postal_code": "zip",
        "hq_state": "state",
        "hq_country": "country",
        "business_phone_no": "phone",
        "industry_category": "industry",
        "employees": "fullTimeEmployees",
        "long_description": "description",
        "first_stock_price_date": "ipoDate",
        "last_price": "price",
        "annualized_dividend_amount": "lastDividend",
    }
    model_config = ConfigDict(extra="ignore")


class SharkQuantEquityProfileFetcher(
    Fetcher[
        SharkQuantEquityProfileQueryParams,
        list[SharkQuantEquityProfileData],
    ]
):
    """Fetches equity profile(s) from fmp_bulk snapshots of profile-bulk."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> SharkQuantEquityProfileQueryParams:
        return SharkQuantEquityProfileQueryParams(**params)

    @staticmethod
    async def aextract_data(
        query: SharkQuantEquityProfileQueryParams,
        credentials: dict[str, str] | None,
        **kwargs: Any,
    ) -> list[dict]:
        symbols = [s.strip().upper() for s in (query.symbol or "").split(",") if s.strip()]
        if not symbols:
            raise EmptyDataError("No symbols provided")

        rows = fetch_bulk_rows(credentials, bulk_name="profile-bulk", symbols=symbols)
        payloads: list[dict] = []
        for row in rows:
            data = row.get("data") or {}
            # Normalize empty strings to None so pydantic optionals don't fail
            normalized = {k: (None if v == "" else v) for k, v in data.items()}
            payloads.append(normalized)
        if not payloads:
            raise EmptyDataError(f"No profile-bulk rows for {symbols}")
        return payloads

    @staticmethod
    def transform_data(
        query: SharkQuantEquityProfileQueryParams,
        data: list[dict],
        **kwargs: Any,
    ) -> list[SharkQuantEquityProfileData]:
        return [SharkQuantEquityProfileData.model_validate(d) for d in data]
