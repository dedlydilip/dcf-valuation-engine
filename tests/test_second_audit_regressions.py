"""One regression test per defect confirmed in the second audit (September 2026).

Kept separate from `test_audit_regressions.py` so each round's findings stay legible
as a set. Same discipline: every test here failed before its fix.

Two of these pin defects the audit report described incorrectly, and the tests are
written to the *reproduced* behaviour rather than the reported one:

  the offline-fixture path breaks through the CLI, which passes an explicit relative
  string -- a test that constructs YFinanceClient directly passes against the bug

  openpyxl coerces inf and nan to None on write, so a non-finite value is only
  visible on the in-memory workbook; asserting on a reloaded file cannot fail
"""

from __future__ import annotations

import math
import warnings
import zipfile

import pytest
from click.testing import CliRunner

from src.cli import cli
from src.dcf.bridge import base_share_count, build_bridge
from src.dcf.engine import DCFEngine, closed_form_dilution_price
from src.fetcher.yfinance_client import YFinanceClient
from src.models.assumptions import DCFAssumptions
from src.models.errors import ConvergenceError
from src.paths import PACKAGE_ROOT, resolve_path


class TestOfflineFixturePathResolution:
    """`--use-offline` broke from any directory but the repo root.

    The README's headline promise is "no API key, no network, and no configuration".
    It held only if you happened to be standing in the repo. `resolve_config_path`
    was already solving this for config files; offline fixtures were missed.
    """

    def test_client_resolves_a_relative_fixture_dir_from_elsewhere(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        client = YFinanceClient("AAPL", offline_mode=True)
        assert client.offline_path.exists(), (
            f"fixture path did not resolve from {tmp_path}: {client.offline_path}"
        )

    def test_client_resolves_an_explicit_relative_dir_from_elsewhere(self, tmp_path, monkeypatch):
        """The path the CLI actually takes.

        `src/cli.py` passes `offline_path=Path(offline_dir) / ticker`, so the
        DEFAULT_OFFLINE_DIR branch is never reached in a real run. Fixing only the
        constant left this broken while the test above went green.
        """
        monkeypatch.chdir(tmp_path)
        client = YFinanceClient("AAPL", offline_mode=True, offline_path="data/offline_sample/AAPL")
        assert client.offline_path.exists()

    def test_cli_values_a_company_from_any_working_directory(self, tmp_path, monkeypatch):
        """End to end, because the two tests above still would not have caught it."""
        monkeypatch.chdir(tmp_path)
        warnings.simplefilter("ignore")
        result = CliRunner().invoke(
            cli,
            [
                "value",
                "--ticker",
                "AAPL",
                "--use-offline",
                "--no-excel",
                "--no-monte-carlo",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Implied value per share" in result.output

    def test_cli_scenarios_still_differ_from_any_working_directory(self, tmp_path, monkeypatch):
        """Guards both audits at once: the fixtures resolve AND the overrides apply."""
        monkeypatch.chdir(tmp_path)
        warnings.simplefilter("ignore")
        result = CliRunner().invoke(cli, ["scenarios", "--ticker", "AAPL", "--use-offline"])
        assert result.exit_code == 0, result.output
        values = []
        for line in result.output.splitlines():
            for scenario in ("bear", "base", "bull"):
                if line.strip().startswith(scenario):
                    values.append(line.split("$")[1].split()[0])
        assert len(values) == 3, f"expected three scenario rows, got: {result.output}"
        assert len(set(values)) == 3, f"scenarios collapsed to the same number: {values}"

    def test_comps_peer_glob_resolves_from_elsewhere(self, tmp_path, monkeypatch):
        """An unresolved fixture dir returned an empty peer set with no error."""
        from src.comps.comps_engine import CompsEngine

        monkeypatch.chdir(tmp_path)
        engine = CompsEngine("AAPL", offline_mode=True)
        peers, source = engine.resolve_peers()
        assert source == "offline_fixtures"
        assert peers, "peer set silently empty -- the fixture directory did not resolve"

    def test_an_explicit_local_directory_still_wins(self, tmp_path, monkeypatch):
        """Resolution order must not hijack a directory the user actually has."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "offline_sample" / "AAPL").mkdir(parents=True)
        client = YFinanceClient("AAPL", offline_mode=True)
        assert (
            client.offline_path.resolve()
            == (tmp_path / "data" / "offline_sample" / "AAPL").resolve()
        )

    def test_absolute_paths_pass_through_untouched(self, tmp_path):
        assert resolve_path(tmp_path) == tmp_path

    def test_unresolvable_paths_keep_the_name_the_user_asked_for(self, tmp_path, monkeypatch):
        """So the error message names their path, not one inside the package."""
        monkeypatch.chdir(tmp_path)
        assert resolve_path("no/such/dir") == pytest.importorskip("pathlib").Path("no/such/dir")
        assert not (PACKAGE_ROOT / "no/such/dir").exists()


class TestOptionOverhangSymmetry:
    """The dilute path dropped the already-granted option overhang; expense kept it.

    Options already granted vest whatever the model assumes about future grants. The
    two SBC treatments differ in how they charge *future* grants -- so the overhang
    belongs in the opening share count under both, and omitting it from one silently
    broke the convergence property the README rests on.

    Latent on the shipped config (`option_overhang_shares: null`), which is why 173
    tests passed over it. With a 5% overhang on AAPL it understated the dilute share
    count by 4.8% and overstated value per share by 5.0%.
    """

    @staticmethod
    def _run(method: str, overhang: float | None):
        warnings.simplefilter("ignore")
        financials = YFinanceClient("AAPL", offline_mode=True).get_financials()
        assumptions = DCFAssumptions.from_yaml(
            overrides={"sbc": {"method": method, "option_overhang_shares": overhang}}
        )
        return DCFEngine(financials, assumptions, ticker="AAPL").run(), financials

    def test_both_methods_include_the_overhang_in_the_opening_count(self):
        base = base_share_count(YFinanceClient("AAPL", offline_mode=True).get_financials())
        overhang = 0.05 * base

        expense, _ = self._run("expense", overhang)
        dilute, _ = self._run("dilute", overhang)

        assert expense.bridge.shares == pytest.approx(base + overhang)
        # Dilute adds future issuance on top of the same opening count, so it must
        # exceed the expense count -- never fall short of it, which is what dropping
        # the overhang did.
        assert dilute.bridge.shares > expense.bridge.shares

    def test_the_overhang_moves_both_methods_by_the_same_opening_amount(self):
        base = base_share_count(YFinanceClient("AAPL", offline_mode=True).get_financials())
        overhang = 0.05 * base

        without, _ = self._run("dilute", None)
        with_overhang, _ = self._run("dilute", overhang)

        delta = with_overhang.bridge.shares - without.bridge.shares
        assert delta > overhang * 0.99, (
            f"adding {overhang:,.0f} shares of overhang moved the dilute count by only "
            f"{delta:,.0f} -- the overhang is being dropped"
        )

    def test_methods_still_converge_with_a_non_zero_overhang(self):
        """The convergence claim must survive the knob, not only its default."""
        base = base_share_count(YFinanceClient("AAPL", offline_mode=True).get_financials())
        expense, _ = self._run("expense", 0.05 * base)
        dilute, _ = self._run("dilute", 0.05 * base)
        gap = abs(dilute.value_per_share / expense.value_per_share - 1.0)
        assert gap < 0.05, f"methods diverged by {gap:.2%} with an overhang applied"


class TestClosedFormGuards:
    """`closed_form_dilution_price` ignored the overhang and never raised on K >= E."""

    def test_overhang_enters_the_denominator(self):
        without = closed_form_dilution_price(
            equity_value=1_000.0,
            base_shares=100.0,
            sbc_dollars=[10.0, 10.0],
            cost_of_equity=0.10,
        )
        with_overhang = closed_form_dilution_price(
            equity_value=1_000.0,
            base_shares=100.0,
            sbc_dollars=[10.0, 10.0],
            cost_of_equity=0.10,
            option_overhang_shares=10.0,
        )
        assert with_overhang < without
        assert with_overhang == pytest.approx(without * 100.0 / 110.0)

    def test_raises_rather_than_returning_a_negative_price(self):
        """Its own docstring said the solver diverges when K exceeds E. Now it enforces it.

        Before: returned -8.18, a confidently-labelled negative share price.
        """
        with pytest.raises(ConvergenceError, match="stock compensation"):
            closed_form_dilution_price(
                equity_value=100.0,
                base_shares=10.0,
                sbc_dollars=[200.0],
                cost_of_equity=0.10,
            )

    def test_the_solver_and_the_closed_form_agree_with_an_overhang(self):
        warnings.simplefilter("ignore")
        financials = YFinanceClient("AAPL", offline_mode=True).get_financials()
        base = base_share_count(financials)
        overhang = 0.05 * base
        assumptions = DCFAssumptions.from_yaml(
            overrides={"sbc": {"method": "dilute", "option_overhang_shares": overhang}}
        )
        engine = DCFEngine(financials, assumptions, ticker="AAPL")
        result = engine.run()
        core = engine._compute_core()
        equity = build_bridge(
            core.enterprise_value, financials, assumptions, base + overhang
        ).equity_value
        sbc = [float(v) for v in core.projection.table.loc["sbc"]]

        expected = closed_form_dilution_price(
            equity,
            base,
            sbc,
            core.wacc_calc.cost_of_equity,
            option_overhang_shares=overhang,
        )
        assert result.value_per_share == pytest.approx(expected, rel=2e-3)


class TestBridgeInputsAreSourced:
    """Preferred equity and restricted cash had no way into the model.

    The audit report claimed the bridge "drops preferred equity and investments"
    entirely. It does not -- the formula is
    `EV - debt + cash - minority - preferred + investments` and both appear as line
    items. The real defect was narrower and quieter: their derived values were
    hardcoded NaN, so `_resolve` fell through to zero and the line printed 0 as
    though that were the reported figure.
    """

    def test_preferred_equity_comes_off_enterprise_value(self):
        from tests.conftest import make_financials

        financials = make_financials(preferred_equity=5_000.0)
        assumptions = DCFAssumptions()
        with_preferred = build_bridge(100_000.0, financials, assumptions, 1_000.0)
        without = build_bridge(100_000.0, make_financials(), assumptions, 1_000.0)
        assert with_preferred.preferred_equity == pytest.approx(5_000.0)
        assert without.equity_value - with_preferred.equity_value == pytest.approx(5_000.0)

    def test_config_still_overrides_the_balance_sheet(self):
        from tests.conftest import make_financials

        assumptions = DCFAssumptions.model_validate({"bridge": {"preferred_equity": 250.0}})
        bridge = build_bridge(
            100_000.0, make_financials(preferred_equity=5_000.0), assumptions, 1_000.0
        )
        assert bridge.preferred_equity == pytest.approx(250.0)

    def test_restricted_cash_is_excluded_from_the_cash_credit(self):
        from src.models.financials import total_cash_position
        from tests.conftest import make_financials

        financials = make_financials(cash=1_000.0, restricted_cash=300.0)
        with pytest.warns(UserWarning, match="Restricted cash"):
            assert total_cash_position(financials, includes_restricted=True) == pytest.approx(700.0)

    def test_immaterial_restricted_cash_deducts_without_shouting(self):
        from src.models.financials import total_cash_position
        from tests.conftest import make_financials

        financials = make_financials(cash=1_000.0, restricted_cash=10.0)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert total_cash_position(financials, includes_restricted=True) == pytest.approx(990.0)

    def test_uncounted_long_term_investments_are_surfaced(self):
        """Deliberately warned about rather than added -- see the note in build_bridge."""
        warnings.simplefilter("always")
        financials = YFinanceClient("AAPL", offline_mode=True).get_financials()
        with pytest.warns(UserWarning, match="long-term investments"):
            build_bridge(1_800_000_000_000.0, financials, DCFAssumptions(), 15e9)

    def test_corrected_sample_valuations_are_pinned(self):
        """Pins include the correction matching historical interest to its own debt period."""
        warnings.simplefilter("ignore")
        expected = {"AAPL": 120.2420, "MSFT": 165.06, "TSLA": 28.63}
        for ticker, value in expected.items():
            financials = YFinanceClient(ticker, offline_mode=True).get_financials()
            result = DCFEngine(financials, DCFAssumptions.from_yaml(), ticker=ticker).run()
            assert result.value_per_share == pytest.approx(value, abs=0.01), (
                f"{ticker} moved to {result.value_per_share:.2f}; the corrected fixture record and "
                f"the audit record both quote {value:.2f}"
            )


class TestNonFiniteCellsAreActuallyChecked:
    """The guard existed; the workbook-level tests that claimed to check it did not.

    `_num()` in the builder maps non-finite to None before writing, and that is
    directly tested. But both workbook-level tests asserted on a *reloaded* file,
    where openpyxl has already coerced inf/nan to None -- so `isinstance(v, float)`
    was False, the assertion never ran, and the test could not fail. The docstring
    explaining why infinities "DO survive the round trip" was wrong.
    """

    def test_openpyxl_coerces_every_non_finite_value_to_none_on_reload(self, tmp_path):
        """Pins the fact that makes the old approach unworkable."""
        from openpyxl import Workbook, load_workbook

        book = Workbook()
        sheet = book.active
        sheet["A1"], sheet["A2"], sheet["A3"] = math.inf, -math.inf, math.nan
        assert all(isinstance(sheet[c].value, float) for c in ("A1", "A2", "A3")), (
            "in memory the value is still a float -- this is where it can be caught"
        )

        path = tmp_path / "nonfinite.xlsx"
        book.save(path)
        reloaded = load_workbook(path).active
        assert [reloaded[c].value for c in ("A1", "A2", "A3")] == [None, None, None], (
            "if this ever changes, the reload-based check becomes viable again"
        )

    def test_a_non_finite_write_produces_a_malformed_numeric_cell(self, tmp_path):
        """Why this matters at all: the cell is typed numeric with an empty value."""
        from openpyxl import Workbook

        book = Workbook()
        book.active["A1"] = math.inf
        path = tmp_path / "malformed.xlsx"
        book.save(path)
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("xl/worksheets/sheet1.xml").decode()
        assert '<c r="A1" t="n"><v></v></c>' in xml

    def test_a_poisoned_value_is_refused_rather_than_blanked(self):
        """End to end: an infinity reaching a real write path now stops the build.

        Before, `_num` blanked it and the workbook was written with an empty cell in
        the equity bridge -- which every formula reading it treats as zero.
        """
        from src.excel.builder import ExcelModelBuilder

        warnings.simplefilter("ignore")
        financials = YFinanceClient("AAPL", offline_mode=True).get_financials()
        result = DCFEngine(financials, DCFAssumptions.from_yaml(), ticker="AAPL").run()
        result.bridge.minority_interest = math.inf

        with pytest.raises(ValueError, match="infinite value reached the workbook"):
            ExcelModelBuilder(result=result, financials=financials).workbook()

    def test_a_clean_model_still_builds(self):
        """The guard must not be so eager that the normal path stops working."""
        from src.excel.builder import ExcelModelBuilder

        warnings.simplefilter("ignore")
        financials = YFinanceClient("AAPL", offline_mode=True).get_financials()
        result = DCFEngine(financials, DCFAssumptions.from_yaml(), ticker="AAPL").run()
        book = ExcelModelBuilder(result=result, financials=financials).workbook()
        assert "Summary" in book.sheetnames


class TestDualClassShareCount:
    """`sharesOutstanding` covers the listed class only; equity value covers all of them.

    Found by generating a Nike workbook: the share-count basis check warned that
    `sharesOutstanding` (1.202bn) disagreed with `marketCap / price` (1.483bn) by 19%.
    Nike is dual class -- Yahoo reports Class B, market capitalisation covers A and B --
    and the DCF's equity value belongs to every class, so dividing by one of them
    overstates value per share by the ratio between them.

        GOOGL  5.867bn reported vs 12.230bn total   $158.36 -> $75.97
        NKE    1.202bn vs 1.483bn                   $44.05  -> $35.70
        META   2.205bn vs 2.548bn                   $229.28 -> $199.87

    Nike is the one that matters: it reverses the conclusion, from 13.6% upside to
    7.9% downside. Four of the 56 fixtures are affected and all four are dual class.
    """

    @staticmethod
    def _run(ticker: str):
        warnings.simplefilter("ignore")
        financials = YFinanceClient(ticker, offline_mode=True).get_financials()
        return DCFEngine(financials, DCFAssumptions.from_yaml(), ticker=ticker).run(), financials

    @pytest.mark.parametrize(
        # GOOGL now includes FY2025 D&A through the per-period alias fallback,
        # correcting the stale historical depreciation ratio in the old fixture run.
        "ticker,expected",
        [("NKE", 35.70), ("GOOGL", 75.5526), ("META", 199.87)],
    )
    def test_dual_class_uses_the_total_share_count(self, ticker, expected):
        result, _ = self._run(ticker)
        assert result.value_per_share == pytest.approx(expected, abs=0.05)

    @pytest.mark.parametrize("ticker", ["NKE", "GOOGL", "META"])
    def test_the_denominator_matches_market_capitalisation(self, ticker):
        """The invariant: equity value covers the whole company, so the count must too."""
        result, financials = self._run(ticker)
        info = financials.info
        market_total = info["marketCap"] / info["currentPrice"]
        assert result.bridge.shares == pytest.approx(market_total, rel=0.02), (
            f"{ticker} divides equity value by {result.bridge.shares:,.0f} shares while "
            f"the market prices {market_total:,.0f} -- one share class against all of them"
        )

    def test_nike_is_not_a_buy(self):
        """The reversal, pinned. It read as 13.6% upside on one class of stock."""
        result, _ = self._run("NKE")
        assert result.bridge.upside < 0

    @pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "TSLA"])
    def test_single_class_companies_are_untouched(self, ticker):
        result, financials = self._run(ticker)
        assert result.bridge.shares == pytest.approx(financials.info["sharesOutstanding"], rel=0.02)

    def test_a_disagreeing_count_warns(self):
        from src.dcf.bridge import base_share_count

        financials = YFinanceClient("NKE", offline_mode=True).get_financials()
        with pytest.warns(UserWarning, match="dual-class"):
            base_share_count(financials)
