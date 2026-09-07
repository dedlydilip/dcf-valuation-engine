"""Audit regression tests: accounting identities and independent forward checks."""

import math

import numpy as np
import pandas as pd
import pytest

from src.dcf.accounting import cash_tax_schedule
from src.dcf.engine import DCFEngine
from src.dcf.monte_carlo import run_monte_carlo
from src.dcf.reverse_dcf import solve_reverse_dcf
from src.dcf.sensitivity import wacc_vs_exit_multiple, wacc_vs_growth
from src.models.assumptions import DCFAssumptions, MonteCarloAssumptions, SBCAssumptions
from src.models.financials import total_cash_position
from tests.conftest import make_financials


@pytest.mark.parametrize("method", ["gordon", "value_driver", "exit_multiple"])
@pytest.mark.parametrize("sbc", ["expense", "dilute"])
@pytest.mark.parametrize("midyear", [False, True])
def test_zero_uncertainty_is_base(tech_financials, method, sbc, midyear):
    a = DCFAssumptions.model_validate(
        {
            "projection": {"mid_year_convention": midyear, "starting_nol": 100},
            "terminal": {"method": method, "exit_multiple_mode": "static"},
            "sbc": {"method": sbc, "buyback_offset_pct": 0.3, "option_overhang_shares": 4},
            "monte_carlo": {
                "iterations": 100,
                "wacc_std": 0,
                "terminal_growth_std": 0,
                "ebit_margin_std": 0,
            },
        }
    )
    base = DCFEngine(tech_financials, a).run()
    sim = run_monte_carlo(base)
    assert sim.values == pytest.approx(np.full(100, base.value_per_share), rel=1e-12)
    assert sim.stats["n_rejected"] == 0


@pytest.mark.parametrize("method", ["gordon", "value_driver", "exit_multiple"])
@pytest.mark.parametrize("sbc", ["expense", "dilute"])
def test_sampled_draws_equal_full_engine(tech_financials, method, sbc):
    a = DCFAssumptions.model_validate(
        {
            "projection": {"ebit_margin": 0.02, "starting_nol": 50},
            "terminal": {"method": method},
            "sbc": {"method": sbc, "sbc_pct_revenue": 0.01, "buyback_offset_pct": 0.25},
            "monte_carlo": {"iterations": 100, "ebit_margin_std": 0.06},
        }
    )
    base = DCFEngine(tech_financials, a).run()
    sim = run_monte_carlo(base)
    for draw, expected in list(zip(sim.draws, sim.values, strict=True))[::11]:
        w, g, m = draw
        patch = a.model_dump()
        patch["projection"]["ebit_margin"] = 0.02 + m
        patch["wacc"]["discount_rate_override"] = w
        patch["terminal"]["perpetuity_growth"] = g
        actual = DCFEngine(tech_financials, DCFAssumptions.model_validate(patch)).run()
        assert actual.value_per_share == pytest.approx(expected, rel=1e-11)
    assert len(sim.values) + sim.stats["n_rejected"] == 100


@pytest.mark.parametrize("method", ["gordon", "value_driver"])
@pytest.mark.parametrize("sbc", ["expense", "dilute"])
def test_reverse_round_trip(tech_financials, method, sbc):
    a = DCFAssumptions.model_validate(
        {
            "projection": {"ebit_margin": 0.2, "revenue_growth": 0.05},
            "terminal": {"method": method},
            "sbc": {"method": sbc, "buyback_offset_pct": 0.4},
        }
    )
    base = DCFEngine(tech_financials, a).run()
    reverse = solve_reverse_dcf(tech_financials, a, None, "TECH", target_price=base.value_per_share)
    assert reverse.implied_perpetuity_growth == pytest.approx(0.025, abs=1e-7)
    assert reverse.implied_revenue_growth_cagr == pytest.approx(0.05, abs=1e-7)
    assert reverse.implied_ebit_margin == pytest.approx(0.2, abs=1e-7)


@pytest.mark.parametrize("sbc", ["expense", "dilute"])
def test_sensitivity_direct_wacc_preserves_other_assumptions(tech_financials, sbc):
    a = DCFAssumptions.model_validate(
        {
            "wacc": {"capital_structure": "target", "target_debt_weight": 0.35},
            "sbc": {"method": sbc},
            "sensitivity": {
                "wacc_deltas": [-0.01, 0, 0.01],
                "growth_deltas": [0],
                "multiple_deltas": [0],
            },
        }
    )
    base = DCFEngine(tech_financials, a).run()
    for exit_mode in (False, True):
        table = (wacc_vs_exit_multiple if exit_mode else wacc_vs_growth)(tech_financials, a, base)
        for i, delta in enumerate([-0.01, 0, 0.01]):
            patch = a.model_dump()
            patch["wacc"]["discount_rate_override"] = base.wacc.wacc + delta
            if exit_mode:
                patch["terminal"].update(
                    method="exit_multiple",
                    exit_multiple_mode="static",
                    static_exit_multiple=base.terminal_all["exit_multiple"].multiple_used,
                )
            actual = DCFEngine(tech_financials, DCFAssumptions.model_validate(patch)).run()
            assert table.frame.iloc[i, 0] == pytest.approx(actual.value_per_share, rel=1e-12)


def test_cash_taxes_preserve_losses_without_negative_tax():
    taxes, opening, used, closing = cash_tax_schedule([-100, 40, 100], 0.25, 10)
    assert taxes == pytest.approx([0, 0, 7.5])
    assert opening == pytest.approx([10, 110, 70])
    assert used == pytest.approx([0, 40, 70])
    assert closing == pytest.approx([110, 70, 0])


def test_opening_nwc_is_historical(golden_financials, golden_assumptions):
    golden_assumptions.projection.revenue_growth = 0
    golden_assumptions.projection.nwc_pct_revenue = 0.1
    result = DCFEngine(golden_financials, golden_assumptions).run()
    assert result.projection.table.loc["nwc_investment", 1] == pytest.approx(100)


def test_cash_fallback_is_per_period():
    fin = make_financials(
        cash=[10, math.nan],
        short_term_investments=[2, math.nan],
        cash_and_sti_combined=[12, 30],
        current_assets=100,
        current_liabilities=40,
        current_debt=0,
    )
    assert fin.net_working_capital().tolist() == pytest.approx([48, 30])
    assert total_cash_position(fin) == pytest.approx(30)


def test_restricted_cash_source_contract():
    fin = make_financials(cash=100, restricted_cash=30)
    assert total_cash_position(fin) == 100
    assert total_cash_position(fin, includes_restricted=True) == 70


def test_metadata_debt_has_one_basis(golden_financials, golden_assumptions):
    fin = golden_financials
    fin.statements.loc["total_debt"] = math.nan
    fin.info["totalDebt"] = 500
    base = DCFEngine(fin, golden_assumptions).run()
    assert base.bridge.total_debt == base.wacc.total_debt == 500


def test_rejected_assignment_is_atomic():
    a = DCFAssumptions()
    with pytest.raises(ValueError):
        a.projection.revenue_growth = -2
    assert a.projection.revenue_growth == 0.05
    s = SBCAssumptions()
    s.method = "dilute"
    assert s.grow_share_count and not s.deduct_from_fcf
    with pytest.raises(ValueError):
        s.deduct_from_fcf = True
    assert s.grow_share_count and not s.deduct_from_fcf


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_nonfinite_assumptions_refused(value):
    with pytest.raises(ValueError):
        DCFAssumptions.model_validate({"wacc": {"beta_override": value}})
    with pytest.raises(ValueError):
        DCFAssumptions.model_validate({"projection": {"ebit_margin": [value] * 5}})


def test_impossible_correlation_refused():
    with pytest.raises(ValueError, match="positive definite"):
        MonteCarloAssumptions(corr_wacc_growth=0.9, corr_wacc_margin=0.9, corr_growth_margin=-0.9)


def test_book_debt_yield_uses_matching_interest_period():
    from src.dcf.wacc import WACCCalculator

    fin = make_financials(
        periods=[2023, 2024, 2025],
        interest_expense=[10, math.nan, math.nan],
        total_debt=[100, 200, 300],
    )
    assert WACCCalculator(fin).cost_of_debt == pytest.approx(0.1)


def test_before_sbc_default_preserves_reported_margin(tech_financials):
    a = DCFAssumptions.model_validate(
        {"projection": {"margin_basis": "before_sbc", "revenue_growth": 0}}
    )
    base = DCFEngine(tech_financials, a).run()
    assert base.projection.table.loc["ebit", 1] == pytest.approx(200)


def test_alias_fallback_fills_latest_not_stale():
    from src.fetcher.normalizer import FinancialNormalizer

    raw = pd.DataFrame(
        [[100, math.nan], [101, 200]], index=["Operating Income", "EBIT"], columns=[2024, 2025]
    )
    out = FinancialNormalizer.canonicalize({"income": raw}, {"ebit": ["Operating Income", "EBIT"]})
    assert out.loc["ebit"].tolist() == [100, 200]
    assert out.attrs["source_aliases"]["ebit"] == {"2024": "Operating Income", "2025": "EBIT"}


def test_manifest_changes_with_data_and_assumptions(golden_financials, golden_assumptions):
    one = DCFEngine(golden_financials, golden_assumptions).run()
    two = DCFEngine(golden_financials, golden_assumptions).run()
    assert one.manifest["data_sha256"] == two.manifest["data_sha256"]
    assert one.manifest["code_sha256"] == two.manifest["code_sha256"]
    golden_assumptions.projection.tax_rate = 0.2
    three = DCFEngine(golden_financials, golden_assumptions).run()
    assert three.manifest["assumptions_sha256"] != one.manifest["assumptions_sha256"]
    assert any("undated" in w for w in three.warnings)
