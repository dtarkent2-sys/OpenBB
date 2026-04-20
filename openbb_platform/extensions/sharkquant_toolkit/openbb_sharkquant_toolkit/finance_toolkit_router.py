"""SharkQuant FinanceToolkit Router.

Exposes [JerBouma/FinanceToolkit](https://github.com/JerBouma/FinanceToolkit)
as OpenBB Platform API endpoints. Rather than hand-writing one command
per FT method, we generate the full surface (~140 endpoints) from a
catalog at import time: every `get_*` method across ratios/models/
performance/risk/technicals becomes an endpoint under
`/api/v1/finance_toolkit/{family}_{method_suffix}`.

Every endpoint shares the signature (symbol, start_date, end_date),
builds a stateless `Toolkit` backed by `FMP_API_KEY`, and returns a
`list[Data]` normalized from whatever shape FT emits.
"""
# pylint: disable=import-outside-toplevel,unused-argument

from typing import Callable, Optional

from openbb_core.app.model.example import APIEx
from openbb_core.app.model.obbject import OBBject
from openbb_core.app.router import Router
from openbb_core.provider.abstract.data import Data

from openbb_sharkquant_toolkit.helpers import (
    build_toolkit,
    df_to_data,
    series_to_data,
)

router = Router(
    prefix="",
    description="FinanceToolkit ratios, models, performance, risk, technicals — full surface.",
)


# =============================================================================
# Method catalog — enumerated once from FinanceToolkit 2.x
# (cross-checked via `docker exec ... python -c 'dir(Toolkit(...).ratios)'`).
# Adding or removing a method here is the only edit needed to keep pace with
# upstream FinanceToolkit releases.
# =============================================================================

_RATIOS = [
    "get_EBIT_to_revenue",
    "get_EBT_to_EBIT",
    "get_accounts_payables_turnover_ratio",
    "get_asset_turnover_ratio",
    "get_book_value_per_share",
    "get_capex_coverage_ratio",
    "get_capex_dividend_coverage_ratio",
    "get_capex_per_share",
    "get_cash_conversion_cycle",
    "get_cash_conversion_efficiency",
    "get_cash_flow_coverage_ratio",
    "get_cash_ratio",
    "get_current_ratio",
    "get_days_of_accounts_payable_outstanding",
    "get_days_of_inventory_outstanding",
    "get_days_of_sales_outstanding",
    "get_debt_service_coverage_ratio",
    "get_debt_to_assets_ratio",
    "get_debt_to_equity_ratio",
    "get_dividend_payout_ratio",
    "get_dividend_yield",
    "get_earnings_per_share",
    "get_earnings_yield",
    "get_effective_tax_rate",
    "get_enterprise_value",
    "get_equity_multiplier",
    "get_ev_to_ebit",
    "get_ev_to_ebitda_ratio",
    "get_ev_to_operating_cashflow_ratio",
    "get_ev_to_sales_ratio",
    "get_fixed_asset_turnover",
    "get_free_cash_flow_operating_cash_flow_ratio",
    "get_free_cash_flow_yield",
    "get_gross_margin",
    "get_income_before_tax_profit_margin",
    "get_income_quality_ratio",
    "get_interest_burden_ratio",
    "get_interest_coverage_ratio",
    "get_interest_debt_per_share",
    "get_inventory_turnover_ratio",
    "get_market_cap",
    "get_net_current_asset_value",
    "get_net_debt_to_ebitda_ratio",
    "get_net_income_per_ebt",
    "get_net_profit_margin",
    "get_operating_cash_flow_ratio",
    "get_operating_cash_flow_sales_ratio",
    "get_operating_cycle",
    "get_operating_margin",
    "get_operating_ratio",
    "get_price_to_book_ratio",
    "get_price_to_cash_flow_ratio",
    "get_price_to_earnings_growth_ratio",
    "get_price_to_earnings_ratio",
    "get_price_to_free_cash_flow_ratio",
    "get_quick_ratio",
    "get_receivables_turnover",
    "get_reinvestment_rate",
    "get_return_on_assets",
    "get_return_on_capital_employed",
    "get_return_on_equity",
    "get_return_on_invested_capital",
    "get_return_on_tangible_assets",
    "get_revenue_per_share",
    "get_sga_to_revenue_ratio",
    "get_short_term_coverage_ratio",
    "get_tangible_asset_value",
    "get_tax_burden_ratio",
    "get_weighted_dividend_yield",
    "get_working_capital",
]
_RATIOS_COLLECTORS = [
    "collect_profitability_ratios",
    "collect_solvency_ratios",
    "collect_liquidity_ratios",
    "collect_efficiency_ratios",
    "collect_valuation_ratios",
    "collect_all_ratios",
]

_MODELS = [
    "get_altman_z_score",
    "get_dupont_analysis",
    "get_enterprise_value_breakdown",
    "get_extended_dupont_analysis",
    "get_gorden_growth_model",
    "get_piotroski_score",
    "get_present_value_of_growth_opportunities",
    "get_weighted_average_cost_of_capital",
    # intrinsic_valuation requires user-supplied growth/WACC; handled separately below.
]

_PERFORMANCE = [
    "get_alpha",
    "get_beta",
    "get_capital_asset_pricing_model",
    "get_compound_growth_rate",
    "get_factor_asset_correlations",
    "get_factor_correlations",
    "get_fama_and_french_model",
    "get_information_ratio",
    "get_jensens_alpha",
    "get_m2_ratio",
    "get_sharpe_ratio",
    "get_sortino_ratio",
    "get_tracking_error",
    "get_treynor_ratio",
    "get_ulcer_performance_index",
]

_RISK = [
    "get_conditional_value_at_risk",
    "get_entropic_value_at_risk",
    "get_garch",
    "get_garch_forecast",
    "get_kurtosis",
    "get_maximum_drawdown",
    "get_skewness",
    "get_ulcer_index",
    "get_value_at_risk",
]

_TECHNICALS = [
    "get_accumulation_distribution_line",
    "get_advancers_decliners",
    "get_aroon_indicator",
    "get_average_directional_index",
    "get_average_true_range",
    "get_balance_of_power",
    "get_bollinger_bands",
    "get_chaikin_oscillator",
    "get_chande_momentum_oscillator",
    "get_commodity_channel_index",
    "get_detrended_price_oscillator",
    "get_double_exponential_moving_average",
    "get_exponential_moving_average",
    "get_force_index",
    "get_ichimoku_cloud",
    "get_keltner_channels",
    "get_mcclellan_oscillator",
    "get_money_flow_index",
    "get_moving_average",
    "get_moving_average_convergence_divergence",
    "get_on_balance_volume",
    "get_percentage_price_oscillator",
    "get_relative_strength_index",
    "get_relative_vigor_index",
    "get_stochastic_oscillator",
    "get_support_resistance_levels",
    "get_triangular_moving_average",
    "get_trix",
    "get_true_range",
    "get_ultimate_oscillator",
    "get_williams_percent_r",
]
_TECHNICALS_COLLECTORS = [
    "collect_all_indicators",
    "collect_breadth_indicators",
    "collect_momentum_indicators",
    "collect_overlap_indicators",
    "collect_volatility_indicators",
]


# =============================================================================
# Endpoint factory
# =============================================================================

def _route_name(family: str, method_name: str) -> str:
    """Produce `family_method_suffix`; e.g. (ratios, get_gross_margin) → ratios_gross_margin."""
    suffix = method_name
    if suffix.startswith("get_"):
        suffix = suffix[4:]
    elif suffix.startswith("collect_"):
        suffix = "collect_" + suffix[len("collect_"):]
    # lowercase + slug-safe
    return f"{family}_{suffix}".lower()


def _make_endpoint(
    family: str,
    attr_name: str,
    method_name: str,
    *,
    quarterly: bool,
) -> Callable:
    """Build a route handler closing over (family, method) for router.command."""

    def endpoint(
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> OBBject[list[Data]]:
        t = build_toolkit(
            symbol,
            start_date=start_date,
            end_date=end_date,
            quarterly=quarterly,
        )
        sub = getattr(t, attr_name)
        fn = getattr(sub, method_name)
        try:
            result = fn()
        except Exception as exc:  # pragma: no cover
            # FT occasionally raises KeyError/ValueError when the symbol has
            # incomplete statements. Surface a typed empty result so the
            # endpoint stays a 200, not a 500.
            return OBBject(results=[], warnings=[{"message": str(exc)}])

        # Performance/risk commonly return scalars or per-symbol Series;
        # ratios/models/technicals return DataFrames. Try series-shape
        # interpretation first for those families, else treat as DataFrame.
        import pandas as pd

        if isinstance(result, (int, float)):
            return OBBject(results=series_to_data(result, metric=_route_name(family, method_name)))
        if isinstance(result, pd.Series):
            return OBBject(results=series_to_data(result, metric=_route_name(family, method_name)))
        return OBBject(results=df_to_data(result))

    endpoint.__name__ = _route_name(family, method_name)
    endpoint.__doc__ = (
        f"FinanceToolkit `{attr_name}.{method_name}()` — called with default parameters. "
        f"See https://www.jeroenbouma.com/projects/financetoolkit for method-specific inputs."
    )
    return endpoint


def _register_family(
    family_label: str,
    attr_name: str,
    method_names: list[str],
    *,
    quarterly: bool,
) -> None:
    for method_name in method_names:
        fn = _make_endpoint(family_label, attr_name, method_name, quarterly=quarterly)
        router.command(
            methods=["GET"],
            examples=[APIEx(parameters={"symbol": "AAPL"})],
        )(fn)


# =============================================================================
# Bulk registration
# =============================================================================

_register_family("ratios", "ratios", _RATIOS, quarterly=True)
_register_family("ratios", "ratios", _RATIOS_COLLECTORS, quarterly=True)
_register_family("models", "models", _MODELS, quarterly=True)
_register_family("performance", "performance", _PERFORMANCE, quarterly=False)
_register_family("risk", "risk", _RISK, quarterly=False)
_register_family("technicals", "technicals", _TECHNICALS, quarterly=False)
_register_family("technicals", "technicals", _TECHNICALS_COLLECTORS, quarterly=False)


# =============================================================================
# Specially-handled endpoints (need extra params)
# =============================================================================

@router.command(
    methods=["GET"],
    examples=[APIEx(parameters={"symbol": "AAPL"})],
)
def models_intrinsic_valuation(
    symbol: str,
    growth_rate: float = 0.05,
    perpetual_growth_rate: float = 0.025,
    weighted_average_cost_of_capital: float = 0.08,
    periods: int = 5,
    cash_flow_type: str = "Free Cash Flow",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> OBBject[list[Data]]:
    """Intrinsic (DCF) valuation.

    FinanceToolkit requires growth + discount assumptions for DCF. Sensible
    defaults are applied (5% near-term, 2.5% perpetual, 8% WACC, 5-year
    projection from FCF). Override via query params for symbol-specific runs.
    """
    t = build_toolkit(symbol, start_date=start_date, end_date=end_date)
    try:
        df = t.models.get_intrinsic_valuation(
            growth_rate=growth_rate,
            perpetual_growth_rate=perpetual_growth_rate,
            weighted_average_cost_of_capital=weighted_average_cost_of_capital,
            periods=periods,
            cash_flow_type=cash_flow_type,
        )
    except Exception as exc:
        return OBBject(results=[], warnings=[{"message": str(exc)}])
    return OBBject(results=df_to_data(df))
