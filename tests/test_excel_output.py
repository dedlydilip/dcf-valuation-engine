"""The Excel workbook.

openpyxl writes formulas but never evaluates them, so these tests check structure and
guard the two bugs that structure alone would not have caught:

  a label beginning with "=" is parsed by Excel as a formula and returns #NAME?
  the share count fed to the workbook must be undiluted, or the workbook's own
  dilution formula applies dilution a second time

Both were found by opening the file in Excel and comparing against the Python engine.
tools/crosscheck.ps1 does that end to end; these run anywhere, Excel or not.
"""

from __future__ import annotations

import math
import warnings

import pytest
from openpyxl import load_workbook

from src.comps.comps_engine import CompsEngine
from src.dcf.bridge import base_share_count
from src.dcf.engine import DCFEngine
from src.dcf.monte_carlo import run_monte_carlo
from src.dcf.sensitivity import football_field, wacc_vs_growth
from src.excel.builder import build_excel_model
from src.fetcher.yfinance_client import YFinanceClient
from src.models.assumptions import DCFAssumptions

EXPECTED_SHEETS = {
    "Summary",
    "Inputs",
    "WACC",
    "DCF",
    "Sensitivity",
    "Comps",
    "FootballField",
    "Historicals",
}


def _build(tmp_path, ticker="AAPL", method="expense"):
    warnings.simplefilter("ignore")
    financials = YFinanceClient(ticker, offline_mode=True).get_financials()
    assumptions = DCFAssumptions.from_yaml(overrides={"sbc": {"method": method}})
    comps = CompsEngine(
        ticker, assumptions, offline_mode=True, target_financials=financials
    ).run()
    result = DCFEngine(financials, assumptions, comps.terminal_inputs(), ticker=ticker).run()
    sensitivity = wacc_vs_growth(financials, assumptions, result, comps.terminal_inputs())
    monte = run_monte_carlo(result, assumptions)
    field = football_field(result, sensitivity, None, monte.stats)

    path = tmp_path / f"{ticker}_{method}.xlsx"
    build_excel_model(
        result,
        path,
        financials=financials,
        comps=comps,
        monte_carlo=monte.stats,
        football=field,
    )
    return path, result, financials


@pytest.fixture(scope="module")
def workbook(tmp_path_factory):
    path, result, financials = _build(tmp_path_factory.mktemp("xl"))
    return load_workbook(path), result, financials


class TestStructure:
    def test_all_sheets_present(self, workbook):
        book, _, _ = workbook
        assert EXPECTED_SHEETS.issubset(set(book.sheetnames))

    def test_summary_is_the_first_sheet(self, workbook):
        book, _, _ = workbook
        assert book.sheetnames[0] == "Summary"

    def test_reopens_without_error(self, tmp_path):
        path, _, _ = _build(tmp_path)
        assert load_workbook(path) is not None
        assert path.stat().st_size > 10_000


class TestFormulasAreLive:
    """A model computes. A report shows numbers someone else computed."""

    def test_dcf_line_items_are_formulas(self, workbook):
        book, _, _ = workbook
        dcf = book["DCF"]
        wanted = {
            "Revenue",
            "EBIT (GAAP, after SBC)",
            "NOPAT",
            "Unlevered FCF (per SBC method)",
            "Discount factor",
            "PV of unlevered FCF",
            "Equity value",
            "Implied value per share",
        }
        found = {}
        for row in dcf.iter_rows(min_col=1, max_col=1):
            label = row[0].value
            if isinstance(label, str) and label in wanted:
                found[label] = row[0].row

        assert wanted == set(found), f"missing rows: {wanted - set(found)}"

        for label, row_number in found.items():
            values = [
                dcf.cell(row=row_number, column=col).value
                for col in range(2, 10)
                if dcf.cell(row=row_number, column=col).value is not None
            ]
            assert values, f"{label} has no values"
            assert any(
                isinstance(v, str) and v.startswith("=") for v in values
            ), f"{label} is hardcoded, not a formula"

    def test_wacc_sheet_is_formula_driven(self, workbook):
        book, _, _ = workbook
        wacc = book["WACC"]
        formulas = [
            c.value
            for row in wacc.iter_rows(min_col=2, max_col=2)
            for c in row
            if isinstance(c.value, str) and c.value.startswith("=")
        ]
        assert len(formulas) >= 10
        assert any("Inputs!" in f for f in formulas), "WACC should link to Inputs"

    def test_sensitivity_cells_recompute(self, workbook):
        book, _, _ = workbook
        sheet = book["Sensitivity"]
        formulas = [
            c.value
            for row in sheet.iter_rows()
            for c in row
            if isinstance(c.value, str) and c.value.startswith("=")
        ]
        assert len(formulas) >= 25, "sensitivity grid should be live, not pasted values"
        assert any("SUMPRODUCT" in f and "DCF!" in f for f in formulas)

    def test_summary_links_rather_than_duplicates(self, workbook):
        book, _, _ = workbook
        summary = book["Summary"]
        links = [
            c.value
            for row in summary.iter_rows(min_col=2, max_col=2)
            for c in row
            if isinstance(c.value, str) and c.value.startswith("=")
        ]
        assert any("DCF!" in f for f in links)


class TestExcelParsingHazards:
    def test_no_label_starts_with_equals(self, workbook):
        """Excel parses a cell beginning with "=" as a formula.

        Subtotal labels like "= NOPAT" read naturally in a model and produce #NAME?
        in every one of them. Bold text and a top border mark a subtotal instead.
        """
        book, _, _ = workbook
        offenders = []
        for sheet in book.worksheets:
            for row in sheet.iter_rows(min_col=1, max_col=1):
                value = row[0].value
                if isinstance(value, str) and value.startswith("="):
                    remainder = value[1:].strip()
                    # A real formula would not be prose.
                    looks_like_prose = bool(remainder) and (
                        " " in remainder or remainder[0].isalpha()
                    )
                    if looks_like_prose and not any(t in value for t in ("(", "!", "$")):
                        offenders.append(f"{sheet.title}!A{row[0].row}: {value}")
        assert not offenders, f"labels Excel will read as formulas: {offenders}"

    def test_no_cell_holds_a_non_finite_number(self):
        """Every numeric cell must be finite -- checked before the file is written.

        This test has now been wrong twice, in the same way both times. The first
        version asserted `value == value` behind an `isinstance(cell.value, float)`
        guard. The second swapped in `math.isfinite` and a docstring claiming
        infinities "DO survive the round trip". They do not: openpyxl coerces inf,
        -inf and nan alike to None on read, so the guard was False, the assertion
        never executed, and the test could not fail either time.

        A non-finite value is only visible on the in-memory workbook, so that is what
        this asserts against. What reaches the file is `<c t="n"><v></v></c>` -- a
        numeric cell holding nothing, which any formula pointing at it reads as zero.
        """
        from src.excel.builder import ExcelModelBuilder, assert_all_cells_finite

        warnings.simplefilter("ignore")
        financials = YFinanceClient("AAPL", offline_mode=True).get_financials()
        result = DCFEngine(financials, DCFAssumptions.from_yaml(), ticker="AAPL").run()

        book = ExcelModelBuilder(result=result, financials=financials).workbook()
        assert_all_cells_finite(book)

    def test_the_finite_check_can_actually_fail(self):
        """Two earlier versions of the test above could not. This pins that it now can."""
        from openpyxl import Workbook

        from src.excel.builder import assert_all_cells_finite

        book = Workbook()
        book.active["B4"] = math.inf
        with pytest.raises(ValueError, match="non-finite"):
            assert_all_cells_finite(book)


class TestShareCountRegression:
    """The workbook applies dilution itself, so it must start from undiluted shares."""

    @pytest.mark.parametrize("method", ["expense", "dilute"])
    def test_inputs_share_count_is_undiluted(self, tmp_path, method):
        path, result, financials = _build(tmp_path, ticker="TSLA", method=method)
        book = load_workbook(path)
        inputs = book["Inputs"]

        value = None
        for row in inputs.iter_rows(min_col=1, max_col=1):
            label = row[0].value
            if isinstance(label, str) and label.startswith("Diluted shares (current"):
                value = inputs.cell(row=row[0].row, column=2).value
        assert value is not None, "share count input not found"

        expected = base_share_count(financials)
        assert value == pytest.approx(expected), (
            "the Inputs sheet must carry the undiluted share count; feeding it the "
            "engine's already-diluted figure makes the workbook dilute twice"
        )

        if method == "dilute":
            assert result.bridge.shares > expected

    def test_dilute_workbook_requires_financials(self):
        """Without financials the undiluted count cannot be recovered, so refuse."""
        from src.excel.builder import ExcelModelBuilder

        warnings.simplefilter("ignore")
        financials = YFinanceClient("AAPL", offline_mode=True).get_financials()
        assumptions = DCFAssumptions.from_yaml(overrides={"sbc": {"method": "dilute"}})
        result = DCFEngine(financials, assumptions, ticker="AAPL").run()

        builder = ExcelModelBuilder(result=result, financials=None)
        with pytest.raises(ValueError, match="undiluted share count"):
            builder._base_shares()


class TestContent:
    def test_historicals_populated(self, workbook):
        book, _, financials = workbook
        sheet = book["Historicals"]
        labels = [
            row[0].value
            for row in sheet.iter_rows(min_col=1, max_col=1)
            if isinstance(row[0].value, str)
        ]
        assert "revenue" in labels
        assert "ebit" in labels

    def test_comps_sheet_records_where_the_peers_came_from(self, workbook):
        """Peer provenance has to reach the workbook, not just the console.

        This used to assert the sheet said "below the minimum". That held only while
        the repository shipped three fixtures and the offline peer set was too thin to
        use; snapshotting the full peer universe gave AAPL seven real Technology peers
        and the note correctly disappeared. Provenance is the durable property -- the
        module's own docstring argues a peer set is a judgement call and its source
        must be recorded rather than hidden.
        """
        book, _, _ = workbook
        sheet = book["Comps"]
        text = " ".join(
            str(c.value)
            for row in sheet.iter_rows(min_col=1, max_col=1)
            for c in row
            if c.value
        )
        assert "Peer set source:" in text
        assert "sector_map" in text or "industry_map" in text or "config" in text

    def test_football_field_has_a_chart(self, workbook):
        book, _, _ = workbook
        assert len(book["FootballField"]._charts) == 1

    def test_sbc_method_is_written_as_a_switch(self, workbook):
        book, result, _ = workbook
        inputs = book["Inputs"]
        found = False
        for row in inputs.iter_rows(min_col=1, max_col=1):
            if isinstance(row[0].value, str) and row[0].value.startswith("SBC method"):
                assert inputs.cell(row=row[0].row, column=2).value == result.assumptions.sbc.method
                found = True
        assert found


class TestSensitivityAgreesWithTheDCFSheet:
    """The exit-multiple grid over-discounted every cell by (1+w)^0.5, about 4.9%.

    The DCF sheet branches correctly -- an exit multiple is a sale price at a point in
    time, so it is discounted the full period, while Gordon's perpetuity of mid-year
    flows is discounted at N-0.5. Both sensitivity grids used the mid-year period, so
    the exit grid read $136.50 at 12x against the DCF sheet's $131.66 for the same
    assumptions.

    The cross-check did not catch it: `tools/build_crosscheck.py` computed its expected
    grid with the same exponent copied from the sheet, so Excel and Python agreed to
    0.000% and 324 comparisons passed. Two implementations of one error agree. These
    tests compare the grid against the *engine*, which derives the convention
    independently, rather than against a restatement of the formula.
    """

    def test_exit_grid_discounts_over_the_full_period_not_mid_year(self, workbook):
        book, _, _ = workbook
        sheet = book["Sensitivity"]

        corner = None
        for row in sheet.iter_rows(min_col=1, max_col=1):
            if row[0].value == "WACC \ multiple":
                corner = row[0].row
        assert corner, "exit-multiple grid not found"

        formula = sheet.cell(row=corner + 1, column=2).value
        assert isinstance(formula, str) and formula.startswith("=")

        dcf = book["DCF"]
        idx_row = exp_row = None
        for row in dcf.iter_rows(min_col=1, max_col=1):
            if row[0].value == "Year index":
                idx_row = row[0].row
            elif row[0].value == "Discount period (years)":
                exp_row = row[0].row
        assert idx_row and exp_row

        last_col = self._last_forecast_column(dcf, idx_row)

        # The terminal term discounts by a single cell: `^DCF!$H$25` (full index) or
        # `^DCF!$H$26` (mid-year). The explicit period uses the *range* `$D$26:$H$26`,
        # which is correctly mid-year and must not be confused with it -- an earlier
        # version of this test matched the bare row number and tripped over exactly that.
        full_period = f"^DCF!${last_col}${idx_row}"
        mid_year_point = f"^DCF!${last_col}${exp_row}"

        assert full_period in formula, (
            f"the exit-multiple grid must discount its terminal value over the full "
            f"year index ({full_period}); the mid-year period overstates every cell "
            f"by (1+w)^0.5. Formula: {formula}"
        )
        assert mid_year_point not in formula, (
            f"terminal value discounted at the mid-year period ({mid_year_point}): "
            f"an exit multiple is a sale price at a point in time. Formula: {formula}"
        )

    @staticmethod
    def _last_forecast_column(dcf, idx_row: int) -> str:
        from openpyxl.utils import get_column_letter

        last = max(
            c.column
            for c in next(dcf.iter_rows(min_row=idx_row, max_row=idx_row))
            if c.value is not None
        )
        return get_column_letter(last)

    def test_grid_axes_are_formulas_so_they_track_the_base_case(self, workbook):
        """Written as literals the axes froze at build time, so changing beta in the
        workbook left the grid no longer centred on the base case while still
        presenting itself as bracketing it."""
        book, _, _ = workbook
        sheet = book["Sensitivity"]

        axis_values = [
            sheet.cell(row=r, column=1).value
            for r in range(1, sheet.max_row + 1)
            if isinstance(sheet.cell(row=r, column=1).value, str)
            and sheet.cell(row=r, column=1).value.startswith("=")
        ]
        assert len(axis_values) >= 10, "WACC axis rows should be formulas, not literals"
        assert all("WACC!" in v or "Inputs!" in v for v in axis_values), (
            f"axis formulas must reference the live WACC cell: {axis_values[:3]}"
        )
