# OpenBB SharkQuant FinanceToolkit Extension

Exposes [JerBouma/FinanceToolkit](https://github.com/JerBouma/FinanceToolkit)
as OpenBB Platform endpoints under `/api/v1/finance_toolkit/*`.

## Endpoints

- `/finance_toolkit/ratios/{profitability,solvency,liquidity,efficiency,valuation,all}`
- `/finance_toolkit/models/{dupont,extended_dupont,wacc,enterprise_value,intrinsic_valuation,altman_z,piotroski}`
- `/finance_toolkit/performance/{sharpe,sortino,treynor,information_ratio,cagr,jensens_alpha,capm}`
- `/finance_toolkit/risk/{value_at_risk,conditional_value_at_risk,max_drawdown,ulcer,skewness,kurtosis,semi_deviation}`
- `/finance_toolkit/technicals/all`

## Environment

Set `ALPHAVANTAGE_API_KEY`. FinanceToolkit is fed through its external
dataset interface from Alpha Vantage (INCOME_STATEMENT, BALANCE_SHEET,
CASH_FLOW, TIME_SERIES_DAILY_ADJUSTED, TREASURY_YIELD) — no FMP key is
required. Raw Alpha Vantage payloads are cached in-process for ~6 hours,
so one symbol's statements are fetched once, not once per endpoint.

### Known data approximations (Alpha Vantage vs FMP)

- Weighted Average Shares (basic + diluted) are approximated with
  period-end `commonStockSharesOutstanding`; EPS is derived from it.
- "Accounts Receivable" is filled with net receivables.
- Rows with no Alpha Vantage source stay NaN (prepaids, tax assets and
  payables, accrued expenses, minority interest, additional paid-in
  capital, stock-based compensation, working-capital change breakdowns,
  acquisitions, investment purchases/sales, debt repayment, cash at
  beginning/end of period, income taxes paid, interest paid). Metrics
  that depend solely on those rows return NaN values.
