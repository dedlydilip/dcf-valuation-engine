"""End-to-end runs against the committed fixtures.

Every test here works offline. That is the point of Patch 2: the model has to be
runnable and verifiable by someone who clones the repository on a day when Yahoo
Finance is down, rate-limiting, or has renamed half its rows. If these pass with the
network unplugged, the repository is doing its job.
"""

from __future__ import annotations

import warnings

import pandas as pd
import pytest

from src.comps.comps_engine import CompsEngine, compute_multiples
from src.dcf.engine import DCFEngine
from src.dcf.monte_carlo import run_monte_carlo
from src.dcf.sensitivity import football_field, wacc_vs_exit_multiple, wacc_vs_growth
from src.fetcher.yfinance_client import YFinanceClient
from src.models.assumptions import DCFAssumptions
from src.models.errors import OfflineDataMissingError

SAMPLES = ["AAPL", "MSFT", "TSLA"]


@pytest.fixture(params=SAMPLES)
def sample(request):
    return YFinanceClient(request.param, offline_mode=True).get_financials()


class TestOfflineFixtures:
    def test_every_sample_loads(self, sample):
        assert sample.source == "offline_sample"
        assert sample.n_years >= 2
        assert sample.latest("revenue") > 0

    def test_required_fields_present(self, sample):
        for name in ("revenue", "ebit", "total_debt", "cash", "diluted_shares"):
            assert pd.notna(sample.latest(name)), f"{sample.ticker} missing {name}"

    def test_periods_are_chronological(self, sample):
        periods = sample.periods
        assert periods == sorted(periods)

    def test_no_empty_stub_periods(self, sample):
        """The union of three statements leaves periods with a balance sheet only."""
        assert sample.series("revenue").notna().all()

    def test_info_carries_pricing_inputs(self, sample):
        assert sample.info.get("currentPrice")
        assert sample.info.get("sharesOutstanding")

    def test_missing_ticker_gives_an_actionable_error(self):
        client = YFinanceClient("NOTAREALTICKER", offline_mode=True)
        with pytest.raises(OfflineDataMissingError, match="snapshot"):
            client.get_financials()


class TestEndToEnd:
    @pytest.mark.parametrize("scenario", ["base", "bull", "bear"])
    def test_runs_for_every_scenario(self, sample, scenario):
        assumptions = DCFAssumptions.from_yaml(scenario=scenario)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = DCFEngine(sample, assumptions, ticker=sample.ticker).run()

        assert result.value_per_share > 0
        assert result.enterprise_value > 0
        assert 0 < result.terminal_value_share < 1
        assert result.wacc.wacc > 0

    @pytest.mark.parametrize("method", ["expense", "dilute"])
    def test_runs_under_both_sbc_methods(self, sample, method):
        assumptions = DCFAssumptions.from_yaml(overrides={"sbc": {"method": method}})
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = DCFEngine(sample, assumptions, ticker=sample.ticker).run()
        assert result.value_per_share > 0

    def test_bull_beats_base_beats_bear(self, sample):
        values = {}
        for scenario in ("bear", "base", "bull"):
            assumptions = DCFAssumptions.from_yaml(scenario=scenario)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                values[scenario] = DCFEngine(
                    sample, assumptions, ticker=sample.ticker
                ).run().value_per_share

        assert values["bear"] < values["base"] < values["bull"], values

    def test_summary_is_complete(self, sample):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = DCFEngine(sample, DCFAssumptions.from_yaml(), ticker=sample.ticker).run()
        summary = result.summary()
        for key in (
            "wacc",
            "enterprise_value",
            "equity_value",
            "value_per_share",
            "terminal_value_pct_ev",
            "sbc_method",
        ):
            assert key in summary and summary[key] is not None


class TestComps:
    def test_multiples_computed_for_each_sample(self, sample):
        multiples = compute_multiples(sample)
        assert multiples["enterprise_value"] > 0
        assert multiples["revenue"] > 0

    def test_negative_denominator_yields_nan_not_a_negative_multiple(self):
        from tests.conftest import make_financials

        loss_maker = make_financials(
            revenue=1000.0,
            ebitda=-200.0,
            ebit=-300.0,
            net_income=-250.0,
            total_debt=0.0,
            cash=0.0,
            info={"marketCap": 5000.0},
        )
        multiples = compute_multiples(loss_maker)
        assert pd.isna(multiples["ev_ebitda"])
        assert pd.isna(multiples["ev_ebit"])
        assert pd.isna(multiples["pe"])
        assert multiples["ev_revenue"] > 0

    def test_offline_comps_report_a_thin_peer_set(self):
        """Only three fixtures exist, so the peer set is honestly labelled as thin."""
        assumptions = DCFAssumptions.from_yaml()
        target = YFinanceClient("AAPL", offline_mode=True).get_financials()
        result = CompsEngine(
            "AAPL", assumptions, offline_mode=True, target_financials=target
        ).run()

        assert result.peer_source == "offline_fixtures"
        assert not result.usable_for_terminal
        assert result.terminal_inputs() == {}
        assert any("below the minimum" in note for note in result.notes())

    def test_outlier_peer_is_screened_from_medians(self):
        assumptions = DCFAssumptions.from_yaml()
        target = YFinanceClient("AAPL", offline_mode=True).get_financials()
        result = CompsEngine(
            "AAPL", assumptions, offline_mode=True, target_financials=target
        ).run()
        # TSLA trades far outside any plausible EV/EBITDA band.
        assert "TSLA" in result.screened_out.get("ev_ebitda", [])
        assert "TSLA" in result.table.index  # still shown, just not in the median


class TestSensitivityAndSimulation:
    @pytest.fixture
    def base_run(self):
        financials = YFinanceClient("AAPL", offline_mode=True).get_financials()
        assumptions = DCFAssumptions.from_yaml()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = DCFEngine(financials, assumptions, ticker="AAPL").run()
        return financials, assumptions, result

    def test_wacc_growth_table_is_monotonic(self, base_run):
        financials, assumptions, result = base_run
        table = wacc_vs_growth(financials, assumptions, result).frame

        # Value falls as the discount rate rises, and rises with perpetuity growth.
        for column in table.columns:
            series = table[column].dropna()
            assert list(series) == sorted(series, reverse=True), f"column {column}"
        for _, row in table.iterrows():
            series = row.dropna()
            assert list(series) == sorted(series), "growth should increase value"

    def test_exit_multiple_table_builds(self, base_run):
        financials, assumptions, result = base_run
        table = wacc_vs_exit_multiple(financials, assumptions, result).frame
        assert table.notna().to_numpy().any()

    def test_monte_carlo_distribution_is_sane(self, base_run):
        _, assumptions, result = base_run
        mc = run_monte_carlo(result, assumptions)

        assert mc.stats["n_valid"] > assumptions.monte_carlo.iterations * 0.9
        assert mc.stats["p10"] < mc.stats["p50"] < mc.stats["p90"]
        # The base case should sit near the middle of its own distribution.
        assert mc.stats["p10"] < result.value_per_share < mc.stats["p90"]

    def test_monte_carlo_is_reproducible(self, base_run):
        _, assumptions, result = base_run
        first = run_monte_carlo(result, assumptions).stats["p50"]
        second = run_monte_carlo(result, assumptions).stats["p50"]
        assert first == pytest.approx(second)

    def test_football_field_includes_market_price(self, base_run):
        financials, assumptions, result = base_run
        sensitivity = wacc_vs_growth(financials, assumptions, result)
        field = football_field(result, sensitivity, None, run_monte_carlo(result, assumptions).stats)
        assert "Current market price" in set(field["method"])
        assert (field["low"] <= field["high"]).all()
