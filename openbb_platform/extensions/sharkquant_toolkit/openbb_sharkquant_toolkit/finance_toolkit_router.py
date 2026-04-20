"""SharkQuant FinanceToolkit Router.

Exposes JerBouma/FinanceToolkit (https://github.com/JerBouma/FinanceToolkit)
as OpenBB Platform API endpoints. Every endpoint builds a fresh Toolkit
against FMP, runs one FT method, returns list[Data]. Stateless on
purpose — OpenBB Workspace picks these up automatically via widgets.json.
"""
# pylint: disable=import-outside-toplevel,unused-argument

from typing import Optional

from openbb_core.app.model.example import APIEx
from openbb_core.app.model.obbject import OBBject
from openbb_core.app.router import Router
from openbb_core.provider.abstract.data import Data

from openbb_sharkquant_toolkit.helpers import (
    build_toolkit,
    df_to_data,
    series_to_data,
)

router = Router(prefix="", description="FinanceToolkit ratios, models, performance, risk, technicals.")


# =============================================================================
# Ratios
# =============================================================================

def _collect_ratios(method_name: str, symbol: str, start_date, end_date) -> list[Data]:
    t = build_toolkit(symbol, start_date=start_date, end_date=end_date)
    fn = getattr(t.ratios, method_name)
    df = fn()
    return df_to_data(df)


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def ratios_profitability(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Profitability ratios (gross/operating/net margin, ROE, ROA, ROIC, etc.)."""
    return OBBject(results=_collect_ratios(
        "collect_profitability_ratios", symbol, start_date, end_date
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def ratios_solvency(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Solvency ratios (debt/equity, debt/assets, interest coverage, etc.)."""
    return OBBject(results=_collect_ratios(
        "collect_solvency_ratios", symbol, start_date, end_date
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def ratios_liquidity(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Liquidity ratios (current, quick, cash, operating cash flow)."""
    return OBBject(results=_collect_ratios(
        "collect_liquidity_ratios", symbol, start_date, end_date
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def ratios_efficiency(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Efficiency ratios (asset/inventory/receivables turnover, cash conversion)."""
    return OBBject(results=_collect_ratios(
        "collect_efficiency_ratios", symbol, start_date, end_date
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def ratios_valuation(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Valuation ratios (P/E, P/B, P/S, EV/EBITDA, dividend yield, etc.)."""
    return OBBject(results=_collect_ratios(
        "collect_valuation_ratios", symbol, start_date, end_date
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def ratios_all(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """All ~150 FinanceToolkit ratios across every family, collected at once."""
    return OBBject(results=_collect_ratios(
        "collect_all_ratios", symbol, start_date, end_date
    ))


# =============================================================================
# Models (fundamental valuation / scoring)
# =============================================================================

def _run_model(method_name: str, symbol: str, start_date, end_date) -> list[Data]:
    t = build_toolkit(symbol, start_date=start_date, end_date=end_date)
    fn = getattr(t.models, method_name)
    result = fn()
    return df_to_data(result)


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def models_dupont(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Classic 3-step Dupont analysis (Net Margin × Asset Turnover × Equity Multiplier)."""
    return OBBject(results=_run_model(
        "get_dupont_analysis", symbol, start_date, end_date
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def models_extended_dupont(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """5-step Extended Dupont decomposition of Return on Equity."""
    return OBBject(results=_run_model(
        "get_extended_dupont_analysis", symbol, start_date, end_date
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def models_weighted_average_cost_of_capital(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Weighted Average Cost of Capital (WACC) with full component breakdown."""
    return OBBject(results=_run_model(
        "get_weighted_average_cost_of_capital", symbol, start_date, end_date
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def models_enterprise_value_breakdown(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Enterprise value components (market cap, debt, cash, minority interest, preferred)."""
    return OBBject(results=_run_model(
        "get_enterprise_value_breakdown", symbol, start_date, end_date
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def models_intrinsic_valuation(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Intrinsic (Gordon growth / DCF) valuation using FT's default assumptions."""
    t = build_toolkit(symbol, start_date=start_date, end_date=end_date)
    # FT's intrinsic_valuation signature varies by version; use the
    # collector path which returns a DataFrame.
    fn = getattr(t.models, "get_intrinsic_valuation", None) or getattr(
        t.models, "get_gordon_growth_model"
    )
    return OBBject(results=df_to_data(fn()))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def models_altman_z_score(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Altman Z-Score — bankruptcy risk indicator."""
    return OBBject(results=_run_model(
        "get_altman_z_score", symbol, start_date, end_date
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def models_piotroski_score(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Piotroski F-Score — fundamental strength scored 0-9."""
    return OBBject(results=_run_model(
        "get_piotroski_score", symbol, start_date, end_date
    ))


# =============================================================================
# Performance
# =============================================================================

def _run_performance(method_name: str, symbol: str, start_date, end_date, metric: str) -> list[Data]:
    t = build_toolkit(symbol, start_date=start_date, end_date=end_date, quarterly=False)
    fn = getattr(t.performance, method_name)
    return series_to_data(fn(), metric=metric)


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def performance_sharpe_ratio(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Sharpe ratio — excess return per unit of volatility."""
    return OBBject(results=_run_performance(
        "get_sharpe_ratio", symbol, start_date, end_date, "sharpe_ratio"
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def performance_sortino_ratio(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Sortino ratio — like Sharpe but penalizes only downside volatility."""
    return OBBject(results=_run_performance(
        "get_sortino_ratio", symbol, start_date, end_date, "sortino_ratio"
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def performance_treynor_ratio(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Treynor ratio — excess return per unit of systematic (beta) risk."""
    return OBBject(results=_run_performance(
        "get_treynor_ratio", symbol, start_date, end_date, "treynor_ratio"
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def performance_information_ratio(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Information ratio — active return over tracking error vs benchmark."""
    return OBBject(results=_run_performance(
        "get_information_ratio", symbol, start_date, end_date, "information_ratio"
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def performance_compound_annual_growth_rate(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Compound annual growth rate (CAGR) over the date window."""
    return OBBject(results=_run_performance(
        "get_compound_growth_rate", symbol, start_date, end_date, "cagr"
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def performance_jensens_alpha(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Jensen's Alpha — CAPM-adjusted excess return."""
    return OBBject(results=_run_performance(
        "get_jensens_alpha", symbol, start_date, end_date, "jensens_alpha"
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def performance_capital_asset_pricing_model(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """CAPM expected return using FT's default risk-free + market series."""
    return OBBject(results=_run_performance(
        "get_capital_asset_pricing_model", symbol, start_date, end_date, "capm"
    ))


# =============================================================================
# Risk
# =============================================================================

def _run_risk(method_name: str, symbol: str, start_date, end_date, metric: str) -> list[Data]:
    t = build_toolkit(symbol, start_date=start_date, end_date=end_date, quarterly=False)
    fn = getattr(t.risk, method_name)
    return series_to_data(fn(), metric=metric)


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def risk_value_at_risk(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Historical Value at Risk (VaR) at the default 95% confidence level."""
    return OBBject(results=_run_risk(
        "get_value_at_risk", symbol, start_date, end_date, "var"
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def risk_conditional_value_at_risk(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Conditional VaR (Expected Shortfall) at 95% confidence."""
    return OBBject(results=_run_risk(
        "get_conditional_value_at_risk", symbol, start_date, end_date, "cvar"
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def risk_max_drawdown(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Maximum historical drawdown over the window."""
    return OBBject(results=_run_risk(
        "get_maximum_drawdown", symbol, start_date, end_date, "max_drawdown"
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def risk_ulcer_index(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Ulcer Index — drawdown-depth severity measure."""
    return OBBject(results=_run_risk(
        "get_ulcer_index", symbol, start_date, end_date, "ulcer_index"
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def risk_skewness(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Return distribution skewness (asymmetry)."""
    return OBBject(results=_run_risk(
        "get_skewness", symbol, start_date, end_date, "skewness"
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def risk_kurtosis(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Return distribution kurtosis (fat-tail-ness)."""
    return OBBject(results=_run_risk(
        "get_kurtosis", symbol, start_date, end_date, "kurtosis"
    ))


@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def risk_semi_deviation(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Semi-deviation — downside volatility only."""
    return OBBject(results=_run_risk(
        "get_semi_deviation", symbol, start_date, end_date, "semi_deviation"
    ))


# =============================================================================
# Technicals
# =============================================================================

@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def technicals_all(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """All ~40 FinanceToolkit technical indicators (MACD, RSI, Bollinger, ADX, etc.)."""
    t = build_toolkit(symbol, start_date=start_date, end_date=end_date, quarterly=False)
    df = t.technicals.collect_all_indicators()
    return OBBject(results=df_to_data(df))
