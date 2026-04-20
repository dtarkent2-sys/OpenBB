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

Set `FMP_API_KEY` so FinanceToolkit can pull statements/prices from FMP.
Data is cached per-Toolkit-instance; each request builds a fresh
instance so requests are independent.
