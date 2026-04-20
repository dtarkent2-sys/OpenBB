"""SharkQuant Provider — serves OpenBB standard models from the SharkQuant
Postgres archive (fmp_bulk + fmp_feeds tables)."""

from openbb_core.provider.abstract.provider import Provider
from openbb_sharkquant.models.equity_profile import SharkQuantEquityProfileFetcher

sharkquant_provider = Provider(
    name="sharkquant",
    description=(
        "SharkQuant Postgres-backed mirror of FMP bulk data. Serves OpenBB "
        "standard models without burning external API credits."
    ),
    website="https://sharkquant.ai",
    credentials=["DATABASE_URL"],
    fetcher_dict={
        "EquityInfo": SharkQuantEquityProfileFetcher,
    },
)
