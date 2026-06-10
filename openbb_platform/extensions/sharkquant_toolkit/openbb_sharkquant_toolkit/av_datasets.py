"""Alpha Vantage → FinanceToolkit external-dataset adapter.

FinanceToolkit (JerBouma/FinanceToolkit) accepts custom datasets via
``Toolkit(balance=..., income=..., cash=..., historical=...)``. The expected
shapes (derived from `financetoolkit.normalization_model` and
`financetoolkit.historical_model` in v2.0.x) are:

Statements (balance / income / cash):
    * rows: MultiIndex (ticker, line_item) where line_item names match the
      FMP keys in `financetoolkit/normalization/{balance,income,cash}.csv`
      (they are renamed to the "Generic" names by
      ``convert_financial_statements``; unmatched rows are dropped).
    * columns: date strings (``YYYY-MM-DD``); ``convert_date_label`` truncates
      to [start_date, end_date] and converts to a PeriodIndex (freq Q or Y).
    * values: float64 (NaN allowed).

Historical prices:
    * index: ``pd.PeriodIndex`` (freq "D").
    * columns: MultiIndex (metric, ticker) with metrics Open/High/Low/Close/
      Adj Close/Volume/Dividends/Return/Volatility/Excess Return/
      Excess Volatility/Cumulative Return — mirroring
      ``historical_model.get_historical_data`` + ``helpers.enrich_historical_data``.
    * the benchmark must be present under the literal ticker "Benchmark"
      (the rename FT normally performs is skipped for custom data).

Treasury (injected post-construction as ``Toolkit._daily_treasury_data``):
    * same shape as historical, ticker level named "10 Year" with yields/100,
      so ``get_treasury_data`` skips its FMP/Yahoo fetch and the
      weekly/monthly/quarterly/yearly risk-free conversions keep working.

This module fetches everything from Alpha Vantage REST
(INCOME_STATEMENT, BALANCE_SHEET, CASH_FLOW, TIME_SERIES_DAILY_ADJUSTED,
TREASURY_YIELD) and maps fields conservatively: line items Alpha Vantage
provides are populated, everything else stays NaN.

Known approximations / gaps (documented intentionally):
    * Weighted Average Shares (and Diluted) are approximated with the
      period-end ``commonStockSharesOutstanding`` from the balance sheet —
      Alpha Vantage does not report weighted averages. EPS is derived as
      netIncome / shares.
    * "Accounts Receivable" is filled with ``currentNetReceivables``
      (AV has no gross receivables split).
    * Interest income/expense are zero-filled when AV reports "None"
      (mirroring FMP's zero-fill; Altman-Z's EBIT needs a number).
    * Permanently-NaN rows (no AV source): prepaids, tax assets/payables,
      accrued expenses, minority interest, additional paid-in capital,
      accumulated OCI, preferred stock, deferred income tax, stock based
      compensation, change-in-working-capital breakdowns, acquisitions,
      purchases/sales of investments, debt repayment, other investing/
      financing activities, cash at beginning/end of period, income taxes
      paid, interest paid. Metrics that depend solely on those rows return
      NaN values (the toolkit tolerates missing data per-metric).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Optional

import httpx

AV_BASE_URL = "https://www.alphavantage.co/query"
CACHE_TTL_SECONDS = 6 * 60 * 60  # ~6h: statements/prices barely move intraday
BENCHMARK_SYMBOL = "SPY"
TREASURY_NAME = "10 Year"  # FT's default risk_free_rate="10y"

_HISTORICAL_METRICS = [
    "Open",
    "High",
    "Low",
    "Close",
    "Adj Close",
    "Volume",
    "Dividends",
    "Return",
    "Volatility",
    "Excess Return",
    "Excess Volatility",
    "Cumulative Return",
]


class AlphaVantageError(RuntimeError):
    """Alpha Vantage could not supply the requested data.

    Raised for quota/rate-limit responses, invalid API keys, unknown symbols
    and empty payloads. The message is meant to be surfaced to API users.
    """


# =============================================================================
# In-process TTL cache (raw AV JSON per (function, symbol) + built bundles)
# =============================================================================

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: tuple) -> Any:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if time.monotonic() > expires_at:
            del _CACHE[key]
            return None
        return value


def _cache_set(key: tuple, value: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, value)


# =============================================================================
# Alpha Vantage REST
# =============================================================================


def get_av_api_key() -> str:
    """Return the Alpha Vantage key from env, raising a clear error if unset."""
    key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
    if not key:
        raise AlphaVantageError(
            "FinanceToolkit endpoints are backed by Alpha Vantage; set "
            "ALPHAVANTAGE_API_KEY in the environment."
        )
    return key


def _av_request(params: dict) -> dict:
    """GET an Alpha Vantage endpoint with TTL caching and error normalization."""
    cache_key = ("av_json",) + tuple(sorted(params.items()))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    response = httpx.get(
        AV_BASE_URL,
        params={**params, "apikey": get_av_api_key()},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        raise AlphaVantageError(
            f"Unexpected Alpha Vantage response for {params.get('function')}: "
            f"{type(payload).__name__}"
        )

    if "Error Message" in payload:
        raise AlphaVantageError(
            f"Alpha Vantage rejected the request ({params.get('function')} "
            f"{params.get('symbol', '')}): {payload['Error Message']}"
        )

    # Throttle / premium notices come back as the *only* key in the payload.
    notice = payload.get("Note") or payload.get("Information")
    data_keys = [k for k in payload if k not in ("Note", "Information")]
    if notice and not data_keys:
        raise AlphaVantageError(
            "Alpha Vantage quota/availability limit hit "
            f"({params.get('function')} {params.get('symbol', '')}): {notice}"
        )

    _cache_set(cache_key, payload)
    return payload


# =============================================================================
# Value helpers
# =============================================================================

_NAN = float("nan")


def _num(report: dict, *fields: str) -> float:
    """First parseable numeric value among `fields`, else NaN."""
    for field in fields:
        value = report.get(field)
        if value in (None, "", "None", "none", "-", "."):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return _NAN


def _isnan(value: float) -> bool:
    return value != value  # noqa: PLR0124 — NaN check without math import cost


def _nsum(*values: float) -> float:
    """NaN-aware sum: NaN inputs are skipped; all-NaN → NaN."""
    present = [v for v in values if not _isnan(v)]
    return sum(present) if present else _NAN


def _nsub(a: float, b: float) -> float:
    if _isnan(a) or _isnan(b):
        return _NAN
    return a - b


def _nneg(value: float) -> float:
    """FMP outflow convention: report payments as negative numbers."""
    if _isnan(value):
        return _NAN
    return -abs(value)


# =============================================================================
# Statement row sets — exact FMP keys from financetoolkit/normalization/*.csv
# =============================================================================

BALANCE_KEYS = [
    "cashAndCashEquivalents", "shortTermInvestments", "cashAndShortTermInvestments",
    "accountsReceivables", "otherReceivables", "netReceivables", "inventory",
    "prepaids", "otherCurrentAssets", "totalCurrentAssets",
    "propertyPlantEquipmentNet", "goodwill", "intangibleAssets",
    "goodwillAndIntangibleAssets", "longTermInvestments", "taxAssets",
    "otherNonCurrentAssets", "totalNonCurrentAssets", "otherAssets", "totalAssets",
    "accountPayables", "otherPayables", "totalPayables", "accruedExpenses",
    "shortTermDebt", "capitalLeaseObligationsCurrent", "taxPayables",
    "deferredRevenue", "otherCurrentLiabilities", "totalCurrentLiabilities",
    "capitalLeaseObligationsNonCurrent", "longTermDebt", "deferredRevenueNonCurrent",
    "deferredTaxLiabilitiesNonCurrent", "otherNonCurrentLiabilities",
    "totalNonCurrentLiabilities", "otherLiabilities", "capitalLeaseObligations",
    "totalDebt", "netDebt", "totalInvestments", "totalLiabilities", "treasuryStock",
    "preferredStock", "commonStock", "retainedEarnings", "additionalPaidInCapital",
    "accumulatedOtherComprehensiveIncomeLoss", "othertotalStockholdersEquity",
    "totalStockholdersEquity", "totalEquity", "minorityInterest",
    "totalLiabilitiesAndStockholdersEquity", "totalLiabilitiesAndTotalEquity",
]

INCOME_KEYS = [
    "revenue", "costOfRevenue", "grossProfit", "researchAndDevelopmentExpenses",
    "generalAndAdministrativeExpenses", "sellingAndMarketingExpenses",
    "sellingGeneralAndAdministrativeExpenses", "otherExpenses", "operatingExpenses",
    "costAndExpenses", "interestIncome", "interestExpense", "netInterestIncome",
    "depreciationAndAmortization", "ebitda", "ebit",
    "nonOperatingIncomeExcludingInterest", "operatingIncome",
    "totalOtherIncomeExpensesNet", "incomeBeforeTax", "incomeTaxExpense",
    "netIncomeFromContinuingOperations", "netIncomeFromDiscontinuedOperations",
    "otherAdjustmentsToNetIncome", "netIncome", "netIncomeDeductions",
    "bottomLineNetIncome", "eps", "epsdiluted", "weightedAverageShsOut",
    "weightedAverageShsOutDil",
]

CASH_KEYS = [
    "netIncome", "depreciationAndAmortization", "deferredIncomeTax",
    "stockBasedCompensation", "changeInWorkingCapital", "accountsReceivables",
    "inventory", "accountsPayables", "otherWorkingCapital", "otherNonCashItems",
    "netCashProvidedByOperatingActivities", "investmentsInPropertyPlantAndEquipment",
    "acquisitionsNet", "purchasesOfInvestments", "salesMaturitiesOfInvestments",
    "otherInvestingActivities", "netCashProvidedByInvestingActivities",
    "debtRepayment", "netDebtIssuance", "longTermNetDebtIssuance",
    "shortTermNetDebtIssuance", "netStockIssuance", "netCommonStockIssuance",
    "commonStockIssuance", "commonStockRepurchased", "netPreferredStockIssuance",
    "commonDividendsPaid", "preferredDividendsPaid", "netDividendsPaid",
    "otherFinancingActivities", "netCashProvidedByFinancingActivities",
    "effectOfForexChangesOnCash", "netChangeInCash", "cashAtEndOfPeriod",
    "cashAtBeginningOfPeriod", "operatingCashFlow", "capitalExpenditure",
    "freeCashFlow", "incomeTaxesPaid", "interestPaid",
]


# =============================================================================
# AV report → FMP-keyed row dicts
# =============================================================================


def _map_balance(report: dict) -> dict:
    out = dict.fromkeys(BALANCE_KEYS, _NAN)

    cash = _num(report, "cashAndCashEquivalentsAtCarryingValue")
    sti = _num(report, "shortTermInvestments")
    lti = _num(report, "longTermInvestments")
    receivables = _num(report, "currentNetReceivables")
    short_debt = _num(report, "shortTermDebt", "currentDebt")
    long_debt = _num(report, "longTermDebtNoncurrent", "longTermDebt")
    total_debt = _num(report, "shortLongTermDebtTotal")
    if _isnan(total_debt):
        total_debt = _nsum(short_debt, long_debt)
    total_liabilities = _num(report, "totalLiabilities")
    equity = _num(report, "totalShareholderEquity")

    out.update(
        {
            "cashAndCashEquivalents": cash,
            "shortTermInvestments": sti,
            "cashAndShortTermInvestments": _num(
                report, "cashAndShortTermInvestments"
            ),
            # AV has no gross receivables; net receivables is the closest proxy.
            "accountsReceivables": receivables,
            "netReceivables": receivables,
            "inventory": _num(report, "inventory"),
            "otherCurrentAssets": _num(report, "otherCurrentAssets"),
            "totalCurrentAssets": _num(report, "totalCurrentAssets"),
            "propertyPlantEquipmentNet": _num(report, "propertyPlantEquipment"),
            "goodwill": _num(report, "goodwill"),
            "intangibleAssets": _num(report, "intangibleAssetsExcludingGoodwill"),
            "goodwillAndIntangibleAssets": _num(report, "intangibleAssets"),
            "longTermInvestments": lti,
            "otherNonCurrentAssets": _num(report, "otherNonCurrentAssets"),
            "totalNonCurrentAssets": _num(report, "totalNonCurrentAssets"),
            "totalAssets": _num(report, "totalAssets"),
            "accountPayables": _num(report, "currentAccountsPayable"),
            "shortTermDebt": short_debt,
            "deferredRevenue": _num(report, "deferredRevenue"),
            "otherCurrentLiabilities": _num(report, "otherCurrentLiabilities"),
            "totalCurrentLiabilities": _num(report, "totalCurrentLiabilities"),
            "longTermDebt": long_debt,
            "otherNonCurrentLiabilities": _num(report, "otherNonCurrentLiabilities"),
            "totalNonCurrentLiabilities": _num(report, "totalNonCurrentLiabilities"),
            "capitalLeaseObligations": _num(report, "capitalLeaseObligations"),
            "totalDebt": total_debt,
            "netDebt": _nsub(total_debt, cash),
            "totalInvestments": _nsum(sti, lti),
            "totalLiabilities": total_liabilities,
            "treasuryStock": _num(report, "treasuryStock"),
            "commonStock": _num(report, "commonStock"),
            "retainedEarnings": _num(report, "retainedEarnings"),
            "totalStockholdersEquity": equity,
            "totalEquity": equity,
            "totalLiabilitiesAndStockholdersEquity": _nsum(
                total_liabilities, equity
            ),
            "totalLiabilitiesAndTotalEquity": _nsum(total_liabilities, equity),
        }
    )
    return out


def _map_income(report: dict, shares: float) -> dict:
    out = dict.fromkeys(INCOME_KEYS, _NAN)

    revenue = _num(report, "totalRevenue")
    gross_profit = _num(report, "grossProfit")
    # Keep COGS consistent with grossProfit (AV's costOfRevenue occasionally
    # includes non-COGS items); fall back to the explicit fields.
    cogs = _nsub(revenue, gross_profit)
    if _isnan(cogs):
        cogs = _num(report, "costofGoodsAndServicesSold", "costOfRevenue")
    operating_income = _num(report, "operatingIncome")
    income_before_tax = _num(report, "incomeBeforeTax")
    net_income = _num(report, "netIncome")
    # AV reports "None" when a company stopped breaking out interest
    # income/expense (e.g. AAPL since FY2023). FMP zero-fills the same
    # rows, and models like Altman-Z (EBIT = NI + tax + interest) need a
    # number — mirror the FMP zero-fill convention here.
    interest_expense = _num(report, "interestExpense", "interestAndDebtExpense")
    if _isnan(interest_expense):
        interest_expense = 0.0
    interest_income = _num(report, "interestIncome", "investmentIncomeNet")
    if _isnan(interest_income):
        interest_income = 0.0

    out.update(
        {
            "revenue": revenue,
            "costOfRevenue": cogs,
            "grossProfit": gross_profit,
            "researchAndDevelopmentExpenses": _num(report, "researchAndDevelopment"),
            "sellingGeneralAndAdministrativeExpenses": _num(
                report, "sellingGeneralAndAdministrative"
            ),
            "operatingExpenses": _num(report, "operatingExpenses"),
            "costAndExpenses": _nsub(revenue, operating_income),
            "interestIncome": interest_income,
            "interestExpense": interest_expense,
            "netInterestIncome": _num(report, "netInterestIncome"),
            "depreciationAndAmortization": _num(
                report, "depreciationAndAmortization", "depreciation"
            ),
            "ebitda": _num(report, "ebitda"),
            "ebit": _num(report, "ebit"),
            "nonOperatingIncomeExcludingInterest": _num(
                report, "otherNonOperatingIncome"
            ),
            "operatingIncome": operating_income,
            "totalOtherIncomeExpensesNet": _nsub(income_before_tax, operating_income),
            "incomeBeforeTax": income_before_tax,
            "incomeTaxExpense": _num(report, "incomeTaxExpense"),
            "netIncomeFromContinuingOperations": _num(
                report, "netIncomeFromContinuingOperations"
            ),
            # FT maps `netIncome` → "Net Income before Deductions" and
            # `bottomLineNetIncome` → "Net Income" (what the ratios use).
            "netIncome": net_income,
            "bottomLineNetIncome": net_income,
        }
    )

    # Approximation: AV income statements lack weighted-average share counts;
    # use period-end shares outstanding from the balance sheet.
    if not _isnan(shares) and shares:
        out["weightedAverageShsOut"] = shares
        out["weightedAverageShsOutDil"] = shares
        if not _isnan(net_income):
            out["eps"] = net_income / shares
            out["epsdiluted"] = net_income / shares
    return out


def _map_cash(report: dict) -> dict:
    out = dict.fromkeys(CASH_KEYS, _NAN)

    operating = _num(report, "operatingCashflow")
    capex = _num(report, "capitalExpenditures")
    issuance_common = _num(report, "proceedsFromIssuanceOfCommonStock")
    repurchase_common = _num(
        report, "paymentsForRepurchaseOfCommonStock", "paymentsForRepurchaseOfEquity"
    )
    issuance_pref = _num(report, "proceedsFromIssuanceOfPreferredStock")
    repurchase_pref = _num(report, "paymentsForRepurchaseOfPreferredStock")
    short_debt_net = _num(report, "proceedsFromRepaymentsOfShortTermDebt")
    long_debt_net = _num(
        report, "proceedsFromIssuanceOfLongTermDebtAndCapitalSecuritiesNet"
    )
    net_common_issuance = _nsum(issuance_common, _nneg(repurchase_common))

    out.update(
        {
            "netIncome": _num(report, "netIncome", "profitLoss"),
            "depreciationAndAmortization": _num(
                report, "depreciationDepletionAndAmortization"
            ),
            "netCashProvidedByOperatingActivities": operating,
            "operatingCashFlow": operating,
            # FMP convention: capital expenditure reported as an outflow (<0).
            "capitalExpenditure": _nneg(capex),
            "investmentsInPropertyPlantAndEquipment": _nneg(capex),
            "freeCashFlow": (
                operating - abs(capex)
                if not (_isnan(operating) or _isnan(capex))
                else _NAN
            ),
            "netCashProvidedByInvestingActivities": _num(
                report, "cashflowFromInvestment"
            ),
            "netCashProvidedByFinancingActivities": _num(
                report, "cashflowFromFinancing"
            ),
            "commonStockIssuance": issuance_common,
            "commonStockRepurchased": _nneg(repurchase_common),
            "netCommonStockIssuance": net_common_issuance,
            "netStockIssuance": _nsum(
                net_common_issuance, issuance_pref, _nneg(repurchase_pref)
            ),
            "netPreferredStockIssuance": _nsum(issuance_pref, _nneg(repurchase_pref)),
            # FMP convention: dividends paid are negative.
            "commonDividendsPaid": _nneg(_num(report, "dividendPayoutCommonStock")),
            "preferredDividendsPaid": _nneg(
                _num(report, "dividendPayoutPreferredStock")
            ),
            "netDividendsPaid": _nneg(_num(report, "dividendPayout")),
            "shortTermNetDebtIssuance": short_debt_net,
            "longTermNetDebtIssuance": long_debt_net,
            "netDebtIssuance": _nsum(short_debt_net, long_debt_net),
            "netChangeInCash": _num(report, "changeInCashAndCashEquivalents"),
            "effectOfForexChangesOnCash": _num(report, "changeInExchangeRate"),
        }
    )
    return out


# =============================================================================
# DataFrame builders
# =============================================================================


def _statement_reports(payload: dict, quarterly: bool) -> list[dict]:
    reports = payload.get("quarterlyReports" if quarterly else "annualReports") or []
    return [r for r in reports if r.get("fiscalDateEnding")]


def _build_statement_frame(
    reports: list[dict],
    keys: list[str],
    mapper: Callable[[dict], dict],
):
    """One ticker's statement: rows = FMP keys, columns = ISO dates ascending."""
    # pylint: disable=import-outside-toplevel
    import pandas as pd

    columns = {}
    for report in reports:
        columns[report["fiscalDateEnding"]] = mapper(report)
    frame = pd.DataFrame(columns, dtype="float64").reindex(keys)
    return frame.reindex(sorted(frame.columns), axis=1)


def _fetch_statements(ticker: str, quarterly: bool) -> Optional[dict]:
    """Build {balance, income, cash} frames for one ticker, or None when AV
    has no fundamentals for it (ETFs, funds, most non-US listings)."""
    income_payload = _av_request({"function": "INCOME_STATEMENT", "symbol": ticker})
    balance_payload = _av_request({"function": "BALANCE_SHEET", "symbol": ticker})
    cash_payload = _av_request({"function": "CASH_FLOW", "symbol": ticker})

    income_reports = _statement_reports(income_payload, quarterly)
    balance_reports = _statement_reports(balance_payload, quarterly)
    cash_reports = _statement_reports(cash_payload, quarterly)

    if not (income_reports or balance_reports or cash_reports):
        return None

    shares_by_date = {
        r["fiscalDateEnding"]: _num(r, "commonStockSharesOutstanding")
        for r in balance_reports
    }

    return {
        "balance": _build_statement_frame(balance_reports, BALANCE_KEYS, _map_balance),
        "income": _build_statement_frame(
            income_reports,
            INCOME_KEYS,
            lambda r: _map_income(r, shares_by_date.get(r["fiscalDateEnding"], _NAN)),
        ),
        "cash": _build_statement_frame(cash_reports, CASH_KEYS, _map_cash),
    }


def _build_price_frame(symbol: str, start_date: str, end_date: Optional[str], rf=None):
    """Daily OHLCV+derived metrics frame for one symbol (PeriodIndex 'D')."""
    # pylint: disable=import-outside-toplevel
    import pandas as pd

    payload = _av_request(
        {
            "function": "TIME_SERIES_DAILY_ADJUSTED",
            "symbol": symbol,
            "outputsize": "full",
        }
    )
    series = payload.get("Time Series (Daily)")
    if not series:
        notice = payload.get("Note") or payload.get("Information") or ""
        raise AlphaVantageError(
            f"Alpha Vantage returned no daily price history for {symbol}."
            + (f" Detail: {notice}" if notice else "")
        )

    rows = {
        date: {
            "Open": _num(bar, "1. open"),
            "High": _num(bar, "2. high"),
            "Low": _num(bar, "3. low"),
            "Close": _num(bar, "4. close"),
            "Adj Close": _num(bar, "5. adjusted close"),
            "Volume": _num(bar, "6. volume"),
            "Dividends": _num(bar, "7. dividend amount"),
        }
        for date, bar in series.items()
    }
    frame = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    frame.index = pd.PeriodIndex(pd.to_datetime(frame.index), freq="D")
    frame = frame.loc[start_date:end_date] if end_date else frame.loc[start_date:]
    if frame.empty:
        raise AlphaVantageError(
            f"No Alpha Vantage price data for {symbol} within the requested window."
        )

    # Mirror financetoolkit.helpers.enrich_historical_data
    frame["Return"] = frame["Adj Close"].ffill().pct_change()
    frame["Volatility"] = frame["Return"].std()
    if rf is not None:
        aligned_rf = rf.reindex(frame.index).ffill()
        frame["Excess Return"] = frame["Return"].sub(aligned_rf)
        frame["Excess Volatility"] = frame["Excess Return"].std()
    else:
        # No risk-free rate: keep the full FT column contract anyway.
        # Omitting these columns makes FinanceToolkit fall back to its own
        # internal FMP/Yahoo fetch paths to fill the gap; NaN columns keep
        # everything in-process (excess-return metrics degrade to NaN).
        frame["Excess Return"] = _NAN
        frame["Excess Volatility"] = _NAN
    adjusted_return = frame["Return"].copy()
    if len(adjusted_return):
        adjusted_return.iloc[0] = 0.0
    frame["Cumulative Return"] = (1.0 + adjusted_return).cumprod()
    return frame


def _build_treasury_frame(start_date: str, end_date: Optional[str]):
    """Daily 10y treasury (yield/100) in FT historical-data shape."""
    # pylint: disable=import-outside-toplevel
    import pandas as pd

    payload = _av_request(
        {"function": "TREASURY_YIELD", "interval": "daily", "maturity": "10year"}
    )
    data = payload.get("data")
    if not data:
        raise AlphaVantageError("Alpha Vantage returned no treasury yield data.")

    values = {
        row["date"]: row["value"]
        for row in data
        if row.get("value") not in (None, "", ".")
    }
    yields = pd.Series(values, dtype="float64").sort_index() / 100.0
    yields.index = pd.PeriodIndex(pd.to_datetime(yields.index), freq="D")
    yields = yields.loc[start_date:end_date] if end_date else yields.loc[start_date:]
    if yields.empty:
        raise AlphaVantageError(
            "No Alpha Vantage treasury yields within the requested window."
        )

    frame = pd.DataFrame(
        {
            "Open": yields,
            "High": yields,
            "Low": yields,
            "Close": yields,
            "Adj Close": yields,
            "Volume": 0.0,
            "Dividends": 0.0,
        }
    )
    frame["Return"] = frame["Adj Close"].ffill().pct_change()
    frame["Volatility"] = frame["Return"].std()
    # Excess return over itself is meaningless for the risk-free series, but
    # the columns must exist to honor the "same shape as historical" contract
    # (see module docstring) so FinanceToolkit never re-fetches externally.
    frame["Excess Return"] = _NAN
    frame["Excess Volatility"] = _NAN
    adjusted_return = frame["Return"].copy()
    if len(adjusted_return):
        adjusted_return.iloc[0] = 0.0
    frame["Cumulative Return"] = (1.0 + adjusted_return).cumprod()

    treasury = pd.concat({TREASURY_NAME: frame}).unstack(level=0)
    return treasury


# =============================================================================
# Public entry point
# =============================================================================


def _copy_bundle(bundle: dict) -> dict:
    """Per-caller view of a (cached) bundle.

    DataFrames *and* lists (`notes`, `fundamentals_missing`) are copied so
    one Toolkit's mutations can never leak into the shared cache entry or
    into other Toolkits built from the same cache hit.
    """
    # pylint: disable=import-outside-toplevel
    import pandas as pd

    return {
        name: (
            value.copy()
            if isinstance(value, pd.DataFrame)
            else list(value) if isinstance(value, list) else value
        )
        for name, value in bundle.items()
    }


def get_av_datasets(
    tickers: list[str],
    quarterly: bool,
    start_date: str,
    end_date: Optional[str] = None,
    include_benchmark: bool = True,
) -> dict:
    """Fetch and shape every dataset FinanceToolkit needs, from Alpha Vantage.

    Returns a dict with keys:
        balance / income / cash: statement DataFrames (may be empty when AV
            has no fundamentals for any ticker — e.g. ETFs);
        historical: combined daily price DataFrame (always populated; raises
            AlphaVantageError when prices are unavailable);
        treasury: 10y treasury frame for risk-free injection, or None;
        fundamentals_missing: tickers with no AV statements;
        notes: non-fatal degradation messages.
    """
    # pylint: disable=import-outside-toplevel
    import pandas as pd

    key = ("bundle", tuple(tickers), quarterly, start_date, end_date, include_benchmark)
    cached = _cache_get(key)
    if cached is not None:
        return _copy_bundle(cached)

    notes: list[str] = []

    # --- treasury / risk-free (optional, degrades gracefully) ---------------
    treasury = None
    rf = None
    try:
        treasury = _build_treasury_frame(start_date, end_date)
        rf = treasury[("Adj Close", TREASURY_NAME)]
    except (AlphaVantageError, httpx.HTTPError) as exc:
        notes.append(
            f"Risk-free rate unavailable ({exc}); excess-return based metrics "
            "(e.g. Sharpe ratio) will be degraded."
        )
        notes.append(
            "'Excess Return' and 'Excess Volatility' columns are NaN-filled "
            "(risk-free rate unavailable) so FinanceToolkit does not fall "
            "back to external FMP/Yahoo fetches."
        )

    # --- prices (mandatory for the requested tickers) ------------------------
    price_frames: dict[str, Any] = {}
    for ticker in tickers:
        price_frames[ticker] = _build_price_frame(ticker, start_date, end_date, rf=rf)

    column_order = list(tickers)
    if include_benchmark:
        try:
            price_frames["Benchmark"] = _build_price_frame(
                BENCHMARK_SYMBOL, start_date, end_date, rf=rf
            )
            column_order.append("Benchmark")
        except (AlphaVantageError, httpx.HTTPError) as exc:
            notes.append(
                f"Benchmark ({BENCHMARK_SYMBOL}) prices unavailable ({exc}); "
                "benchmark-relative metrics (alpha, beta, CAPM) will fail."
            )

    historical = pd.concat(price_frames).unstack(level=0)
    historical = historical.reindex(column_order, level=1, axis=1)
    if "Dividends" in historical.columns:
        historical["Dividends"] = historical["Dividends"].fillna(0)
    historical = historical.interpolate(limit_area="inside")

    # --- fundamentals ---------------------------------------------------------
    balance_frames: dict[str, Any] = {}
    income_frames: dict[str, Any] = {}
    cash_frames: dict[str, Any] = {}
    fundamentals_missing: list[str] = []

    for ticker in tickers:
        statements = _fetch_statements(ticker, quarterly)
        if statements is None:
            fundamentals_missing.append(ticker)
            continue
        balance_frames[ticker] = statements["balance"]
        income_frames[ticker] = statements["income"]
        cash_frames[ticker] = statements["cash"]

    def _combine(frames: dict) -> Any:
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames)

    bundle = {
        "balance": _combine(balance_frames),
        "income": _combine(income_frames),
        "cash": _combine(cash_frames),
        "historical": historical,
        "treasury": treasury,
        "fundamentals_missing": fundamentals_missing,
        "notes": notes,
    }
    _cache_set(key, bundle)
    return _copy_bundle(bundle)
