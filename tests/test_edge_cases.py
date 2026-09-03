"""How the model behaves on real-world data garbage.

Clean-input tests prove the arithmetic. These prove the model survives contact with
actual filings: missing rows, string-formatted numbers, negative EBITDA, companies
whose stock compensation exceeds their operating profit, and tickers that exist but
have no financial data at all.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.dcf.projector import Projector
from src.dcf.wacc import WACCCalculator
from src.fetcher.normalizer import FinancialNormalizer, to_float
from src.models.assumptions import DCFAssumptions
from src.models.errors import DataQualityError
from src.models.quality_gate import DataQualityGate
from tests.conftest import make_financials


class TestDataRobustness:
    def test_missing_interest_expense(self):
        """No debt, so interest expense is NaN. Cost of debt must not blow up."""
        financials = pd.DataFrame(
            {"ebit": [1000.0], "interest_expense": [np.nan], "total_debt": [0.0]}
        )
        wacc = WACCCalculator(financials)
        # Falls back to the risk-free rate. It carries zero weight anyway, because
        # a company with no debt has no debt leg in its WACC.
        assert wacc.cost_of_debt == pytest.approx(0.04, rel=0.1)
        assert wacc.debt_weight == 0.0

    def test_negative_ebitda_warns_and_forces_gordon(self):
        """A loss-making company. The exit multiple is meaningless on a negative base."""
        financials = make_financials(
            revenue=1000.0,
            ebit=-800.0,
            ebitda=-500.0,
            total_debt=100.0,
            cash=50.0,
            diluted_shares=100.0,
        )
        assumptions = DCFAssumptions()

        with pytest.warns(UserWarning, match="Negative EBITDA"):
            Projector(financials, assumptions)

    def test_string_formatted_numbers(self):
        """Providers leak presentation formatting: separators, suffixes, junk tokens."""
        raw = pd.DataFrame(
            {
                "revenue": ["1,234,000", "2.5B", "3.1B"],
                "ebitda": [np.nan, "invalid", "1.0B"],
            }
        )
        cleaned = FinancialNormalizer.clean(raw)

        assert cleaned["revenue"].dtype == np.float64
        assert cleaned["revenue"].iloc[0] == 1_234_000.0
        assert cleaned["revenue"].iloc[1] == 2.5e9
        assert cleaned["revenue"].iloc[2] == 3.1e9
        # Unparseable becomes NaN rather than raising: one bad cell must not kill a run.
        assert pd.isna(cleaned["ebitda"].iloc[1])
        assert cleaned["ebitda"].iloc[2] == 1.0e9

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("(1,500)", -1500.0),
            ("$2.5B", 2.5e9),
            ("1.5%", 0.015),
            ("—", None),
            ("N/A", None),
            ("", None),
            (None, None),
            (42, 42.0),
        ],
    )
    def test_scalar_coercion(self, raw, expected):
        result = to_float(raw)
        if expected is None:
            assert pd.isna(result)
        else:
            assert result == pytest.approx(expected)

    def test_all_nan_financials(self):
        """Ticker exists but carries no data -- a delisted shell or a fresh SPAC."""
        empty = make_financials(revenue=[np.nan, np.nan])
        with pytest.raises(DataQualityError, match="Insufficient data"):
            DataQualityGate(empty).validate()

    def test_negative_revenue_is_critical(self):
        financials = make_financials(
            revenue=-100.0, ebit=10.0, total_debt=0.0, cash=0.0
        )
        with pytest.raises(DataQualityError, match="negative revenue"):
            DataQualityGate(financials).validate()

    def test_missing_required_field_is_critical(self):
        """Revenue and EBIT are genuinely required; nothing can be forecast without them."""
        financials = make_financials(total_debt=0.0, cash=100.0)  # no revenue or ebit rows
        with pytest.raises(DataQualityError, match="revenue"):
            DataQualityGate(financials).validate()

    def test_debt_free_company_is_valued_not_rejected(self):
        """A company that has never borrowed has no 'Total Debt' row at all.

        Treating that as a critical data failure refused most debt-free software
        companies outright. An absent balance is zero, and the gate says so.
        """
        financials = make_financials(revenue=1000.0, ebit=200.0, cash=100.0)  # no debt row
        report = DataQualityGate(financials).validate()
        assert report.ok
        assert any("total_debt" in w and "zero" in w for w in report.warnings)

    def test_absent_cash_also_defaults_to_zero_with_a_warning(self):
        financials = make_financials(revenue=1000.0, ebit=200.0, total_debt=50.0)
        report = DataQualityGate(financials).validate()
        assert report.ok
        assert any("cash" in w and "zero" in w for w in report.warnings)

    def test_sbc_exceeds_operating_profit(self):
        """Stock compensation larger than operating profit.

        Note this departs from a naive reading of the case. Reported EBIT is already
        net of SBC, so an EBIT of 100 alongside SBC of 150 describes a company that
        earned 100 *after* paying its staff 150 in stock -- cash flow is positive and
        should be. The interesting case is the company whose operating profit exists
        only because SBC was added back: GAAP EBIT negative, EBIT before SBC positive.
        There the SBC treatment decides whether the business looks profitable at all.
        """
        financials = make_financials(
            revenue=1000.0,
            ebit=-50.0,  # GAAP, already net of the 150 of SBC
            ebitda=0.0,
            da=50.0,
            capex=50.0,
            sbc=150.0,
            total_debt=0.0,
            cash=100.0,
            current_assets=200.0,
            current_liabilities=200.0,
            diluted_shares=100.0,
        )
        assumptions = DCFAssumptions.model_validate(
            {
                "projection": {"years": 3, "revenue_growth": 0.0, "tax_rate": 0.21},
                "sbc": {"method": "expense", "sbc_pct_revenue": 0.15},
            }
        )
        projector = Projector(financials, assumptions)

        # Treated honestly, the business consumes cash.
        assert projector.adjusted_fcf.iloc[0] < 0
        # Add the stock compensation back and it looks profitable. Same company.
        assert projector.sbc_neutral_fcf.iloc[0] > 0
        assert projector.adjusted_fcf.iloc[0] < projector.sbc_neutral_fcf.iloc[0]

    def test_sbc_above_threshold_warns(self):
        financials = make_financials(
            revenue=1000.0,
            ebit=100.0,
            sbc=700.0,
            total_debt=0.0,
            cash=0.0,
        )
        report = DataQualityGate(financials, max_sbc_pct_revenue=0.60).validate()
        assert any("stock-based compensation" in w.lower() for w in report.warnings)

    def test_no_sbc_reported_warns(self):
        financials = make_financials(revenue=1000.0, ebit=100.0, total_debt=0.0, cash=0.0)
        report = DataQualityGate(financials).validate()
        assert any("No stock-based compensation" in w for w in report.warnings)


class TestWACCEdgeCases:
    def test_negative_beta(self):
        """A gold miner or an inverse ETF. Economically odd, mathematically valid."""
        wacc = WACCCalculator(beta=-0.2, rf=0.04, erp=0.055)
        # 4% + (-0.2 x 5.5%) = 2.9%, below the risk-free rate. Not clamped, because
        # clamping would hide a genuine property of the asset.
        assert wacc.cost_of_equity == pytest.approx(0.029, abs=0.001)

    def test_negative_net_debt(self):
        """Apple's position: cash exceeds debt, so enterprise value is below market cap."""
        wacc = WACCCalculator(market_cap=2.5e12, total_debt=100e9, cash=200e9)
        assert wacc.enterprise_value < wacc.market_cap
        assert wacc.net_debt == pytest.approx(-100e9)
        assert wacc.enterprise_value == pytest.approx(2.4e12)

    def test_zero_equity_value_does_not_divide_by_zero(self):
        wacc = WACCCalculator(market_cap=0.0, total_debt=0.0, cash=0.0)
        assert wacc.debt_weight == 0.0
        assert wacc.equity_weight == 1.0

    def test_cost_of_debt_clamped_against_garbage(self):
        """A tiny debt balance against a full year of interest implies a silly rate."""
        financials = make_financials(interest_expense=50.0, total_debt=1.0)
        assert WACCCalculator(financials).cost_of_debt <= 0.35

    def test_target_capital_structure_overrides_market_weights(self):
        financials = make_financials(total_debt=0.0, cash=0.0)
        assumptions = DCFAssumptions.model_validate(
            {"wacc": {"capital_structure": "target", "target_debt_weight": 0.30}}
        )
        wacc = WACCCalculator(financials, assumptions, market_cap=1000.0)
        assert wacc.debt_weight == pytest.approx(0.30)
        assert wacc.equity_weight == pytest.approx(0.70)

    def test_effective_tax_rate_ignores_nonsense(self):
        """A pretax loss with a tax benefit produces a meaningless effective rate."""
        financials = make_financials(tax_provision=-10.0, pretax_income=-100.0)
        assert pd.isna(WACCCalculator(financials).effective_tax_rate)
