import pandas as pd
import pytest

from src.comps.comps_engine import compute_multiples
from src.dcf.engine import DCFEngine
from src.dcf.moat import _resolve_invested_capital
from src.dcf.monte_carlo import run_monte_carlo
from src.dcf.wacc import WACCCalculator
from src.models.assumptions import DCFAssumptions, WACCAssumptions
from src.models.financials import Financials


def test_hamada_beta_relevering():
    """Verify that beta is properly unlevered and re-levered under target capital structure."""
    # Company with current D/E: Debt = 200, Equity (Market Cap) = 800 -> D/E = 0.25
    # Raw beta = 1.20, Tax rate = 0.20
    # Hamada unlevered beta = 1.20 / (1 + (1 - 0.20) * 0.25) = 1.20 / 1.20 = 1.00
    financials = pd.DataFrame(
        {
            "total_debt": [200.0],
            "interest_expense": [10.0],
            "tax_provision": [20.0],
            "pretax_income": [100.0],
        }
    )
    fin_obj = Financials(
        ticker="TEST",
        statements=financials.T,
        info={"marketCap": 800.0, "beta": 1.20},
    )

    # 1. Under current capital structure: beta is 1.20
    calc_current = WACCCalculator(fin_obj, beta=1.20, tax_rate=0.20)
    assert pytest.approx(calc_current.beta, rel=1e-4) == 1.20

    # 2. Under target capital structure: Target debt weight = 0.50 -> Target D/E = 1.00
    # Expected re-levered beta = 1.00 * (1 + (1 - 0.20) * 1.00) = 1.00 * 1.80 = 1.80
    wacc_target_assump = WACCAssumptions(
        capital_structure="target",
        target_debt_weight=0.50,
        beta_override=1.20,
    )
    calc_target = WACCCalculator(fin_obj, assumptions=wacc_target_assump, tax_rate=0.20)
    assert pytest.approx(calc_target.beta, rel=1e-4) == 1.80
    assert calc_target.raw_beta == 1.20


def test_cost_of_equity_floor():
    """Verify that cost of equity is floored at the risk-free rate when floor_cost_of_equity is True."""
    financials = pd.DataFrame({"ebit": [100.0], "total_debt": [0.0]})
    # Negative beta: Rf = 4.2%, ERP = 5.5%, Beta = -1.0 -> Unfloored CAPM = -1.3%
    wacc_unfloored = WACCAssumptions(risk_free_rate=0.042, equity_risk_premium=0.055, beta_override=-1.0)
    calc_unfloored = WACCCalculator(financials, assumptions=wacc_unfloored)
    assert calc_unfloored.cost_of_equity < 0.0

    wacc_floored = WACCAssumptions(
        risk_free_rate=0.042, equity_risk_premium=0.055, beta_override=-1.0, floor_cost_of_equity=True
    )
    calc_floored = WACCCalculator(financials, assumptions=wacc_floored)
    assert calc_floored.cost_of_equity == pytest.approx(0.042)


def test_operating_nwc_fallback_clean_from_financing():
    """Verify that operating NWC fallback strips cash, sti, and current debt."""
    from src.dcf.projector import Projector

    financials_df = pd.DataFrame(
        {
            "revenue": [1000.0],
            "ebit": [100.0],
            "ebitda": [120.0],
            "current_assets": [500.0],
            "current_liabilities": [300.0],
            "cash": [200.0],
            "short_term_investments": [50.0],
            "current_debt": [100.0],
            "total_debt": [100.0],
            "diluted_shares": [10.0],
        }
    )
    p = Projector(financials_df, DCFAssumptions())
    base_nwc_pct = p._base_nwc_pct()
    assert pytest.approx(base_nwc_pct, rel=1e-3) == 0.05


def test_correlated_monte_carlo():
    """Verify that Monte Carlo produces valid, finite results with Cholesky correlated shocks."""
    financials = pd.DataFrame(
        {
            "revenue": [1000.0],
            "ebit": [150.0],
            "ebitda": [200.0],
            "cfo": [180.0],
            "capex": [50.0],
            "total_debt": [0.0],
            "cash": [100.0],
            "diluted_shares": [100.0],
        }
    )
    assumptions = DCFAssumptions()
    res = DCFEngine(financials, assumptions).run()
    mc = run_monte_carlo(res, assumptions)
    assert mc.stats["p10"] > 0
    assert mc.stats["p10"] <= mc.stats["p50"] <= mc.stats["p90"]
    assert 0.0 <= mc.probability_above(res.bridge.current_price or 100.0) <= 1.0


def test_comps_peer_ev_includes_preferred_and_minority():
    """Verify that peer EV includes preferred equity and minority interest."""
    df = pd.DataFrame(
        {
            "total_debt": [100.0, 100.0],
            "cash": [50.0, 50.0],
            "short_term_investments": [0.0, 0.0],
            "preferred_equity": [25.0, 25.0],
            "minority_interest": [15.0, 15.0],
            "revenue": [1000.0, 1100.0],
            "ebitda": [200.0, 220.0],
            "ebit": [150.0, 165.0],
            "net_income": [100.0, 110.0],
        }
    )
    fin = Financials(ticker="PEER", statements=df.T, info={"marketCap": 800.0})
    multiples = compute_multiples(fin)
    # EV = 800 (market cap) + 100 (debt) - 50 (cash) + 25 (preferred) + 15 (minority) = 890.0
    assert pytest.approx(multiples["enterprise_value"]) == 890.0


def test_invested_capital_deducts_long_term_investments():
    """Verify that long-term non-operating investments are excluded from invested capital."""
    df = pd.DataFrame(
        {
            "total_debt": [500.0],
            "stockholders_equity": [1000.0],
            "cash": [200.0],
            "long_term_investments": [300.0],
            "revenue": [2000.0],
        }
    )
    fin = Financials(ticker="TEST", statements=df.T)
    # Financing IC = Debt (500) + Equity (1000) - Cash (200) - LT Investments (300) = 1000.0
    ic = _resolve_invested_capital(fin)
    assert pytest.approx(ic) == 1000.0
