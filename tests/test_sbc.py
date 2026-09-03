"""Stock-based compensation: the guard, the solver, and the equivalence.

The claim this model makes about SBC is that expensing it and diluting for it are two
routes to the same answer, so doing both double-counts. A claim like that is worth
proving rather than asserting, which is what `test_methods_converge` does.
"""

from __future__ import annotations

import pytest

from src.dcf.bridge import base_share_count, build_bridge
from src.dcf.dilution import DilutionTracker, expense_method_share_count
from src.dcf.engine import DCFEngine, closed_form_dilution_price
from src.dcf.projector import Projector
from src.models.assumptions import DCFAssumptions, SBCAssumptions
from src.models.errors import ConvergenceError


class TestDoubleCountGuard:
    def test_cannot_expense_and_dilute_together(self):
        """The whole point of the enum. Both switches on is the error to prevent."""
        with pytest.raises(ValueError, match="double-count"):
            SBCAssumptions(deduct_from_fcf=True, grow_share_count=True)

    def test_cannot_ignore_sbc_entirely(self):
        with pytest.raises(ValueError, match="ignored"):
            SBCAssumptions(deduct_from_fcf=False, grow_share_count=False)

    def test_expense_method_derives_switches(self):
        sbc = SBCAssumptions(method="expense")
        assert sbc.deduct_from_fcf is True
        assert sbc.grow_share_count is False

    def test_dilute_method_derives_switches(self):
        sbc = SBCAssumptions(method="dilute")
        assert sbc.deduct_from_fcf is False
        assert sbc.grow_share_count is True

    def test_explicit_forecast_requires_values(self):
        with pytest.raises(ValueError, match="explicit"):
            SBCAssumptions(forecast_method="explicit", explicit=None)

    def test_explicit_length_must_match_horizon(self):
        with pytest.raises(ValueError, match="projection.years"):
            DCFAssumptions.model_validate(
                {
                    "projection": {"years": 5},
                    "sbc": {"forecast_method": "explicit", "explicit": [1.0, 2.0]},
                }
            )


class TestFCFBridge:
    def test_three_definitions_differ_by_after_tax_sbc(self, tech_financials):
        """SBC-neutral FCF exceeds adjusted FCF by exactly the after-tax add-back."""
        assumptions = DCFAssumptions.model_validate(
            {
                "projection": {"years": 3, "revenue_growth": 0.0, "tax_rate": 0.25},
                "sbc": {"method": "expense", "sbc_pct_revenue": 0.10},
            }
        )
        projector = Projector(tech_financials, assumptions)
        table = projector.table

        for year in table.columns:
            gap = table.loc["sbc_neutral_fcf", year] - table.loc["adjusted_fcf", year]
            assert gap == pytest.approx(table.loc["sbc", year] * 0.75)

    def test_expense_method_discounts_the_expensed_series(self, tech_financials):
        assumptions = DCFAssumptions.model_validate({"sbc": {"method": "expense"}})
        projector = Projector(tech_financials, assumptions)
        assert list(projector.unlevered_fcf) == pytest.approx(list(projector.adjusted_fcf))

    def test_dilute_method_discounts_the_added_back_series(self, tech_financials):
        assumptions = DCFAssumptions.model_validate({"sbc": {"method": "dilute"}})
        projector = Projector(tech_financials, assumptions)
        assert list(projector.unlevered_fcf) == pytest.approx(list(projector.sbc_neutral_fcf))


class TestDilutionSolver:
    def test_share_count_grows_with_sbc(self):
        tracker = DilutionTracker(100.0, SBCAssumptions(method="dilute"))
        path = tracker.forecast([100.0, 100.0], base_price=10.0, price_growth=0.0)
        # 100 dollars of stock at 10 dollars a share is 10 new shares each year.
        assert path.shares == pytest.approx([110.0, 120.0])
        assert path.ending_shares == pytest.approx(120.0)

    def test_issue_price_grows_at_the_supplied_rate(self):
        tracker = DilutionTracker(100.0, SBCAssumptions(method="dilute"))
        path = tracker.forecast([100.0], base_price=10.0, price_growth=0.10)
        assert path.issue_prices[0] == pytest.approx(11.0)
        assert path.new_shares[0] == pytest.approx(100.0 / 11.0)

    def test_buyback_offset_reduces_issuance(self):
        tracker = DilutionTracker(100.0, SBCAssumptions(method="dilute", buyback_offset_pct=0.5))
        path = tracker.forecast([100.0], base_price=10.0, price_growth=0.0)
        assert path.new_shares[0] == pytest.approx(5.0)

    def test_zero_price_is_rejected(self):
        tracker = DilutionTracker(100.0, SBCAssumptions(method="dilute"))
        with pytest.raises(ValueError, match="base_price"):
            tracker.forecast([100.0], base_price=0.0)

    def test_option_overhang_added_under_expense_method(self):
        sbc = SBCAssumptions(method="expense", option_overhang_shares=5.0)
        assert expense_method_share_count(100.0, sbc) == pytest.approx(105.0)

    def test_solver_matches_the_closed_form(self, tech_financials):
        """The iterative fixed point must land where the algebra says it should.

        Solving P = (E - K)/S0 analytically and by iteration are independent routes to
        the same number, so agreement is real evidence the loop is correct.
        """
        assumptions = DCFAssumptions.model_validate(
            {"sbc": {"method": "dilute", "sbc_pct_revenue": 0.08}}
        )
        engine = DCFEngine(tech_financials, assumptions, ticker="TECH")
        result = engine.run()

        core = engine._compute_core()
        base_shares = base_share_count(tech_financials)
        undiluted_equity = build_bridge(
            core.enterprise_value, tech_financials, assumptions, base_shares
        ).equity_value
        sbc_dollars = [float(v) for v in core.projection.table.loc["sbc"]]

        expected = closed_form_dilution_price(
            undiluted_equity, base_shares, sbc_dollars, core.wacc_calc.cost_of_equity
        )
        assert result.value_per_share == pytest.approx(expected, rel=1e-3)

    def test_solver_converges_quickly(self, tech_financials):
        assumptions = DCFAssumptions.model_validate({"sbc": {"method": "dilute"}})
        result = DCFEngine(tech_financials, assumptions, ticker="TECH").run()
        assert 1 <= result.iterations <= 10

    def test_diverges_when_sbc_swamps_equity_value(self, tech_financials):
        """When the PV of future stock grants exceeds equity value there is no solution.

        Each pass issues more stock, which lowers the price, which issues more stock.
        The model says so instead of returning the last iterate.
        """
        assumptions = DCFAssumptions.model_validate(
            {
                "projection": {"years": 5, "revenue_growth": 0.0},
                "sbc": {"method": "dilute", "sbc_pct_revenue": 3.0},
            }
        )
        with pytest.raises(ConvergenceError):
            DCFEngine(tech_financials, assumptions, ticker="TECH").run()


class TestMethodEquivalence:
    def test_methods_converge(self, tech_financials):
        """Expensing and diluting must produce a similar per-share value.

        They are not identical, and the residual is explainable rather than noise:

          expensing charges the present value of SBC after tax, discounted at WACC
          diluting charges the present value of SBC before tax, discounted at the
          cost of equity

        So the gap is the tax shield on stock compensation plus the spread between
        the two discount rates. A few percent is expected. A large divergence would
        mean one of the two paths is wrong.

        The threshold is 5% because 5% binds and 15% did not. On this fixture the
        real gap is 1.4%; removing dilution entirely -- adding SBC back to cash flow
        without growing the share count, the double-count this model exists to
        prevent -- widens it to 6.8%. The old 15% bound passed in that state, so it
        was testing nothing. The README quotes 5%; this is the test it means.
        """
        results = {}
        for method in ("expense", "dilute"):
            assumptions = DCFAssumptions.model_validate(
                {"sbc": {"method": method, "sbc_pct_revenue": 0.05}}
            )
            results[method] = DCFEngine(
                tech_financials, assumptions, ticker="TECH"
            ).run().value_per_share

        gap = abs(results["dilute"] / results["expense"] - 1.0)
        assert gap < 0.05, f"methods diverged by {gap:.1%}: {results}"

    def test_the_five_percent_threshold_binds(self, tech_financials):
        """Prove the bound above can fail, since its predecessor could not.

        Reproduces the defect it guards -- SBC added back to cash flow with the share
        count held flat -- and checks the resulting gap clears 5% but not 15%.
        """
        expense = DCFEngine(
            tech_financials,
            DCFAssumptions.model_validate(
                {"sbc": {"method": "expense", "sbc_pct_revenue": 0.05}}
            ),
            ticker="TECH",
        ).run()
        dilute = DCFEngine(
            tech_financials,
            DCFAssumptions.model_validate(
                {"sbc": {"method": "dilute", "sbc_pct_revenue": 0.05}}
            ),
            ticker="TECH",
        ).run()

        undiluted = dilute.bridge.equity_value / expense.bridge.shares
        gap = abs(undiluted / expense.value_per_share - 1.0)
        assert gap > 0.05, "the 5% bound would not catch dilution being dropped"
        assert gap < 0.15, "which is exactly why the old 15% bound passed regardless"

    @pytest.mark.parametrize(
        "ticker,expected_gap", [("AAPL", 0.005), ("MSFT", 0.007), ("TSLA", 0.039)]
    )
    def test_the_readme_convergence_figures_are_current(self, ticker, expected_gap):
        """The README quotes 0.5% / 0.8% / 3.9%. Pinned so they cannot go stale.

        An earlier README claimed the two treatments agreed "within 0.5-0.8%",
        generalising from two companies while its own results table listed three.
        """
        import warnings

        from src.fetcher.yfinance_client import YFinanceClient

        warnings.simplefilter("ignore")
        financials = YFinanceClient(ticker, offline_mode=True).get_financials()
        values = {}
        for method in ("expense", "dilute"):
            assumptions = DCFAssumptions.from_yaml(overrides={"sbc": {"method": method}})
            values[method] = DCFEngine(
                financials, assumptions, ticker=ticker
            ).run().value_per_share

        gap = abs(values["dilute"] / values["expense"] - 1.0)
        assert gap == pytest.approx(expected_gap, abs=0.001), (
            f"{ticker} convergence gap is now {gap:.1%}; the README says {expected_gap:.1%}"
        )

    def test_enterprise_value_is_higher_under_dilute(self, tech_financials):
        """Adding SBC back raises cash flow; the charge lands on the share count."""
        evs, shares = {}, {}
        for method in ("expense", "dilute"):
            assumptions = DCFAssumptions.model_validate(
                {"sbc": {"method": method, "sbc_pct_revenue": 0.05}}
            )
            result = DCFEngine(tech_financials, assumptions, ticker="TECH").run()
            evs[method] = result.enterprise_value
            shares[method] = result.bridge.shares

        assert evs["dilute"] > evs["expense"]
        assert shares["dilute"] > shares["expense"]

    def test_zero_sbc_makes_the_methods_identical(self, golden_financials):
        """With no stock compensation there is nothing to expense or dilute."""
        values = []
        for method in ("expense", "dilute"):
            assumptions = DCFAssumptions.model_validate(
                {
                    "projection": {
                        "years": 3,
                        "revenue_growth": 0.10,
                        "ebit_margin": 0.20,
                        "tax_rate": 0.25,
                        "da_pct_revenue": 0.05,
                        "capex_pct_revenue": 0.05,
                        "nwc_pct_revenue": 0.0,
                        "mid_year_convention": False,
                    },
                    "wacc": {
                        "risk_free_rate": 0.04,
                        "equity_risk_premium": 0.05,
                        "beta_override": 1.2,
                    },
                    "terminal": {"method": "gordon", "perpetuity_growth": 0.02},
                    "sbc": {"method": method, "sbc_pct_revenue": 0.0},
                }
            )
            values.append(DCFEngine(golden_financials, assumptions).run().value_per_share)

        assert values[0] == pytest.approx(values[1])
        assert values[0] == pytest.approx(23.625)
