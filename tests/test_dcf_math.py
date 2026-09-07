"""The arithmetic, checked against a valuation worked out by hand.

The golden case is built so every intermediate lands on a round number:

    Revenue      1,000 growing 10%   ->  1,100.00  1,210.00  1,331.00
    EBIT at 20%                      ->    220.00    242.00    266.20
    Less tax at 25%                  ->    165.00    181.50    199.65
    D&A 5% and capex 5% cancel       ->    165.00    181.50    199.65
    Discounted at 10%                ->    150.00    150.00    150.00
    Sum of explicit period                                      450.00

    Terminal value  199.65 x 1.02 / (0.10 - 0.02)            2,545.5375
    Discounted at 1.1^3                                      1,912.50
    Enterprise value = equity value (no debt, no cash)        2,362.50
    Over 100 shares                                              23.625

If a refactor moves the valuation, this test fails before anyone ships it.
"""

from __future__ import annotations

import pytest

from src.dcf.engine import DCFEngine, discount_factors, terminal_discount_factor
from src.dcf.projector import Projector
from src.dcf.terminal_value import TerminalValue, implied_perpetuity_growth
from src.dcf.wacc import WACCCalculator


class TestGoldenValuation:
    def test_wacc_is_exactly_ten_percent(self, golden_financials, golden_assumptions):
        wacc = WACCCalculator(golden_financials, golden_assumptions)
        assert wacc.cost_of_equity == pytest.approx(0.10)
        assert wacc.debt_weight == pytest.approx(0.0)
        assert wacc.wacc == pytest.approx(0.10)

    def test_projection_line_items(self, golden_financials, golden_assumptions):
        table = Projector(golden_financials, golden_assumptions).table
        assert list(table.loc["revenue"]) == pytest.approx([1100.0, 1210.0, 1331.0])
        assert list(table.loc["ebit"]) == pytest.approx([220.0, 242.0, 266.2])
        assert list(table.loc["nopat"]) == pytest.approx([165.0, 181.5, 199.65])
        # D&A and capex are both 5% of revenue, so they cancel exactly.
        assert list(table.loc["adjusted_fcf"]) == pytest.approx([165.0, 181.5, 199.65])
        assert list(table.loc["nwc_investment"]) == pytest.approx([0.0, 0.0, 0.0])

    def test_each_discounted_flow_is_exactly_150(self, golden_financials, golden_assumptions):
        result = DCFEngine(golden_financials, golden_assumptions).run()
        assert result.pv_explicit == pytest.approx([150.0, 150.0, 150.0])
        assert sum(result.pv_explicit) == pytest.approx(450.0)

    def test_terminal_value(self, golden_financials, golden_assumptions):
        result = DCFEngine(golden_financials, golden_assumptions).run()
        assert result.terminal.value == pytest.approx(2545.5375)
        assert result.pv_terminal == pytest.approx(1912.50)

    def test_enterprise_and_per_share_value(self, golden_financials, golden_assumptions):
        result = DCFEngine(golden_financials, golden_assumptions).run()
        assert result.enterprise_value == pytest.approx(2362.50)
        assert result.bridge.equity_value == pytest.approx(2362.50)
        assert result.value_per_share == pytest.approx(23.625)

    def test_terminal_value_share_of_ev(self, golden_financials, golden_assumptions):
        result = DCFEngine(golden_financials, golden_assumptions).run()
        assert result.terminal_value_share == pytest.approx(1912.50 / 2362.50)


class TestDiscounting:
    def test_year_end_factors(self):
        factors = discount_factors(0.10, 3, mid_year=False)
        assert factors == pytest.approx([1 / 1.1, 1 / 1.21, 1 / 1.331])

    def test_mid_year_factors_are_higher(self):
        year_end = discount_factors(0.10, 3, mid_year=False)
        mid_year = discount_factors(0.10, 3, mid_year=True)
        assert all(m > y for m, y in zip(mid_year, year_end, strict=True))
        assert mid_year[0] == pytest.approx(1 / (1.1**0.5))

    def test_terminal_factor_matches_final_year_convention(self):
        assert terminal_discount_factor(0.10, 3, mid_year=False) == pytest.approx(1 / 1.331)
        assert terminal_discount_factor(0.10, 3, mid_year=True) == pytest.approx(1 / 1.1**2.5)

    def test_mid_year_convention_raises_the_valuation(self, golden_financials, golden_assumptions):
        year_end = DCFEngine(golden_financials, golden_assumptions).run().value_per_share
        mid = golden_assumptions.model_copy(deep=True)
        mid.projection.mid_year_convention = True
        mid_year = DCFEngine(golden_financials, mid).run().value_per_share
        assert mid_year > year_end
        # Half a year of discounting at 10% is worth about 4.9%.
        assert mid_year / year_end == pytest.approx(1.1**0.5, rel=1e-6)


class TestGordonAndImpliedGrowth:
    def test_gordon_formula(self):
        tv = TerminalValue()
        result = tv.gordon_value(terminal_fcf=100.0, wacc=0.10, growth=0.02)
        assert result.value == pytest.approx(100.0 * 1.02 / 0.08)

    def test_gordon_diverges_when_growth_exceeds_wacc(self):
        result = TerminalValue().gordon_value(terminal_fcf=100.0, wacc=0.03, growth=0.05)
        assert not result.ok
        assert "does not exceed" in result.warnings[0]

    def test_implied_growth_inverts_gordon(self):
        """Back-solving g from a terminal value must return the g that produced it."""
        fcf, wacc, growth = 100.0, 0.10, 0.025
        terminal = fcf * (1 + growth) / (wacc - growth)
        assert implied_perpetuity_growth(terminal, fcf, wacc) == pytest.approx(growth)
