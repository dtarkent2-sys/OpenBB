"""SharkQuant Provider — serves OpenBB standard models from the SharkQuant
Postgres archive (fmp_bulk + fmp_feeds tables)."""

from openbb_core.provider.abstract.provider import Provider
from openbb_sharkquant.models.balance_sheet import (
    SharkQuantBalanceSheetFetcher,
)
from openbb_sharkquant.models.cash_flow import (
    SharkQuantCashFlowStatementFetcher,
)
from openbb_sharkquant.models.equity_profile import (
    SharkQuantEquityProfileFetcher,
)
from openbb_sharkquant.models.financial_ratios import (
    SharkQuantFinancialRatiosFetcher,
)
from openbb_sharkquant.models.income_statement import (
    SharkQuantIncomeStatementFetcher,
)

# No declared credentials: the provider reads DATABASE_URL from the
# process environment (set by docker-compose). Declaring it here would
# make OpenBB's credential framework reject requests before our code
# runs when the user hasn't POSTed a per-session credential.
sharkquant_provider = Provider(
    name="sharkquant",
    description=(
        "SharkQuant Postgres-backed mirror of FMP bulk data. Serves OpenBB "
        "standard models without burning external API credits. "
        "Reads Postgres via the DATABASE_URL environment variable."
    ),
    website="https://sharkquant.ai",
    credentials=None,
    fetcher_dict={
        "BalanceSheet": SharkQuantBalanceSheetFetcher,
        "CashFlowStatement": SharkQuantCashFlowStatementFetcher,
        "EquityInfo": SharkQuantEquityProfileFetcher,
        "FinancialRatios": SharkQuantFinancialRatiosFetcher,
        "IncomeStatement": SharkQuantIncomeStatementFetcher,
    },
)
