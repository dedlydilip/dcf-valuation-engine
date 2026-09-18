"""Which Yahoo row becomes `ebit`, and why the as-filed one has to win.

Yahoo publishes two operating-profit rows that disagree with each other: an adjusted
"Operating Income" and the filed "Total Operating Income As Reported". They differ in
75 of 152 fixture ticker-periods. Reconciling against `us-gaap:OperatingIncomeLoss` --
the figure the company actually filed with the SEC -- matched "As Reported" in 28 of 28
cases checked and the adjusted row in none.

`FIELD_MAP` preferred the adjusted row, so 18 of the 57 fixtures were forecasting from a
base-year operating profit the company never reported. Boeing is the case that shows
what that costs: the model read a 5,416m loss where the filing says a 4,281m profit.

The whole defect was invisible from the headline numbers because Apple -- the ticker the
README, the pinned tests and the entire Excel cross-check are built on -- is one of the
companies where the two rows agree.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pandas as pd
import pytest

from src.dcf.engine import DCFEngine
from src.fetcher.normalizer import FinancialNormalizer
from src.fetcher.yfinance_client import YFinanceClient
from src.models.assumptions import DCFAssumptions
from src.models.financials import FIELD_MAP, Financials, field_value
from src.paths import resolve_path

# Base-year operating profit exactly as filed with the SEC, verified one by one against
# `us-gaap:OperatingIncomeLoss` on EDGAR. These are deliberately the companies where
# Yahoo's adjusted row diverges most, so a regression cannot hide behind a small gap.
AS_FILED_BASE_EBIT = {
    "BA": 4_281_000_000.0,  # adjusted row says -5,416,000,000: a sign flip
    "INTC": -2_214_000_000.0,  # adjusted row says -23,000,000
    "ABBV": 15_075_000_000.0,  # adjusted row says 20,091,000,000
    "KO": 13_762_000_000.0,  # adjusted row says 14,911,000,000
    "TSLA": 4_355_000_000.0,  # adjusted row says 4,849,000,000
    "ORCL": 20_606_000_000.0,  # adjusted row says 22,444,000,000
    "CRM": 8_331_000_000.0,  # adjusted row says 8,917,000,000
}


class TestFieldMapPrefersTheFiledRow:
    def test_as_reported_is_the_first_candidate(self):
        """The ordering IS the fix, so it is asserted directly rather than inferred."""
        assert FIELD_MAP["ebit"][0] == "Total Operating Income As Reported"
        assert FIELD_MAP["operating_income"][0] == "Total Operating Income As Reported"

    def test_yahoos_own_ebit_row_stays_last(self):
        """Unchanged from the earlier finding: that row is pretax plus interest."""
        assert FIELD_MAP["ebit"][-1] == "EBIT"

    def test_the_filed_row_wins_when_both_are_present(self):
        """Directly: two rows, different values, the filed one is the one chosen."""
        raw = pd.DataFrame(
            [[9_992.0], [14_022.0]],
            index=["Total Operating Income As Reported", "Operating Income"],
            columns=[pd.Timestamp("2024-12-31")],
        )
        out = FinancialNormalizer.canonicalize({"income": raw})
        assert out.loc["ebit", pd.Timestamp("2024-12-31")] == pytest.approx(9_992.0)

    def test_the_adjusted_row_is_still_used_when_nothing_was_filed(self):
        """A fallback chain, not a replacement -- dropping the adjusted row would lose
        every period where Yahoo carries only that one."""
        raw = pd.DataFrame(
            [[14_022.0]],
            index=["Operating Income"],
            columns=[pd.Timestamp("2024-12-31")],
        )
        out = FinancialNormalizer.canonicalize({"income": raw})
        assert out.loc["ebit", pd.Timestamp("2024-12-31")] == pytest.approx(14_022.0)


class TestBaseYearMatchesTheFiling:
    @pytest.mark.parametrize("ticker,expected", sorted(AS_FILED_BASE_EBIT.items()))
    def test_base_year_ebit_is_the_filed_figure(self, ticker, expected):
        warnings.simplefilter("ignore")
        financials = YFinanceClient(ticker, offline_mode=True).get_financials()
        assert field_value(financials, "ebit") == pytest.approx(expected), (
            f"{ticker} base-year EBIT is not the figure filed with the SEC; "
            f"FIELD_MAP is reading Yahoo's adjusted operating-income row again"
        )

    def test_boeing_reads_a_profit_not_a_loss(self):
        """The finding in one assertion. Boeing filed +4,281m; the model read -5,416m."""
        warnings.simplefilter("ignore")
        financials = YFinanceClient("BA", offline_mode=True).get_financials()
        assert field_value(financials, "ebit") > 0


class TestTheAliasWarningPointsAtTheRightRow:
    """The warning in `DCFEngine.run` used to treat the adjusted row as the safe case.

    Reordering FIELD_MAP without reversing this check would have left the warning silent
    for the one input that needs checking and noisy about the one that does not.
    """

    @staticmethod
    def _run_with_ebit_row(label: str):
        periods = [pd.Timestamp("2024-12-31"), pd.Timestamp("2025-12-31")]
        raw = pd.DataFrame(
            [
                [1000.0, 1100.0],
                [200.0, 220.0],
                [50.0, 55.0],
                [50.0, 55.0],
                [100.0, 100.0],
                [0.0, 0.0],
                [100.0, 100.0],
            ],
            index=[
                "Total Revenue",
                label,
                "Depreciation And Amortization",
                "Capital Expenditure",
                "Current Assets",
                "Current Liabilities",
                "Diluted Average Shares",
            ],
            columns=periods,
        )
        canonical = FinancialNormalizer.canonicalize({"income": raw})
        financials = Financials(
            ticker="X",
            statements=canonical,
            info={"marketCap": 2000.0, "currentPrice": 20.0, "sharesOutstanding": 100},
            provenance={"source_aliases": canonical.attrs.get("source_aliases", {})},
        )
        assumptions = DCFAssumptions.model_validate({"wacc": {"beta_override": 1.0}})
        result = DCFEngine(financials, assumptions, ticker="X").run()
        return [w for w in result.warnings if "provider alias" in w]

    def test_warns_when_ebit_comes_from_the_adjusted_row(self):
        warnings.simplefilter("ignore")
        hits = self._run_with_ebit_row("Operating Income")
        assert hits, "falling back to Yahoo's adjusted operating income must be surfaced"
        assert "Operating Income" in hits[0]

    def test_silent_when_ebit_comes_from_the_filed_row(self):
        warnings.simplefilter("ignore")
        assert self._run_with_ebit_row("Total Operating Income As Reported") == []


class TestTheTwoRowsGenuinelyDisagree:
    """Guards the premise. If Yahoo ever reconciled its own rows these tests would pass
    for the wrong reason, and the ordering above would be untested rather than correct.
    """

    def test_the_fixtures_still_contain_divergent_rows(self):
        divergent = 0
        compared = 0
        for directory in sorted(resolve_path("data/offline_sample").iterdir()):
            path = directory / "income_stmt.json"
            if not path.exists():
                continue
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            for rows in payload.values():
                filed = rows.get("Total Operating Income As Reported")
                adjusted = rows.get("Operating Income")
                if filed is None or adjusted is None:
                    continue
                compared += 1
                if abs(filed - adjusted) > 1e6:
                    divergent += 1

        assert compared > 50, "too few ticker-periods carry both rows to prove anything"
        assert divergent > 20, (
            f"only {divergent} of {compared} ticker-periods disagree; if Yahoo has "
            f"reconciled its two operating-income rows, the ordering in FIELD_MAP is no "
            f"longer doing any work and this whole file is vacuous"
        )
