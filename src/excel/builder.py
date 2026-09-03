"""Build the Excel model.

Every computed cell is a real Excel formula, not a number Python worked out and
pasted in. That distinction is the whole point: an analyst opening this file can
change the risk-free rate, the EBIT margin, or the exit multiple and watch the
valuation move, exactly as they would in a model built by hand. A workbook of
hardcoded values looks identical until someone clicks a cell, and then it is
obviously a report rather than a model.

The SBC treatment is a live switch too. Type "dilute" in the SBC method cell on the
Inputs sheet and the tax line, the free cash flow line and the share count all
change together -- which makes the double-count the model refuses to commit visible
rather than theoretical.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from src.excel import styles as S

FIRST_FORECAST_COL = 4  # column D; column C holds the base year


class SheetCursor:
    """Row-by-row writer that remembers where it put things."""

    def __init__(self, worksheet: Worksheet, refs: dict[str, str]) -> None:
        self.ws = worksheet
        self.refs = refs
        self.row = 1

    # ------------------------------------------------------------------ helpers

    def addr(self, row: int, col: int) -> str:
        return f"{self.ws.title}!${get_column_letter(col)}${row}"

    def cell_addr(self, row: int, col: int) -> str:
        return f"${get_column_letter(col)}${row}"

    def remember(self, key: str, row: int, col: int) -> str:
        ref = self.addr(row, col)
        self.refs[key] = ref
        return ref

    def skip(self, n: int = 1) -> None:
        self.row += n

    # ------------------------------------------------------------------- blocks

    def title(self, text: str, width: int = 9) -> None:
        cell = self.ws.cell(row=self.row, column=1, value=text)
        cell.font = S.TITLE_FONT
        for col in range(1, width + 1):
            self.ws.cell(row=self.row, column=col).fill = S.TITLE_FILL
        self.ws.row_dimensions[self.row].height = 22
        self.row += 2

    def section(self, text: str, width: int = 9) -> None:
        cell = self.ws.cell(row=self.row, column=1, value=text)
        cell.font = S.SECTION_FONT
        for col in range(1, width + 1):
            self.ws.cell(row=self.row, column=col).fill = S.SECTION_FILL
        self.row += 1

    def note(self, text: str, warn: bool = False) -> None:
        cell = self.ws.cell(row=self.row, column=1, value=text)
        cell.font = S.WARN_FONT if warn else S.NOTE_FONT
        self.row += 1

    def label_value(
        self,
        label: str,
        value: Any,
        fmt: str | None = None,
        key: str | None = None,
        kind: str = "input",
        col: int = 2,
    ) -> str:
        self.ws.cell(row=self.row, column=1, value=label).font = S.LABEL_FONT
        cell = self.ws.cell(row=self.row, column=col, value=_num(value))
        cell.font = _font_for(kind)
        if fmt:
            cell.number_format = fmt
        ref = self.remember(key, self.row, col) if key else self.addr(self.row, col)
        self.row += 1
        return ref

    def series_row(
        self,
        label: str,
        values: list[Any],
        fmt: str | None = None,
        key: str | None = None,
        kind: str = "formula",
        start_col: int = FIRST_FORECAST_COL,
        bold: bool = False,
        border: Any = None,
    ) -> int:
        """Write a labelled row of per-year cells and return its row number."""
        label_cell = self.ws.cell(row=self.row, column=1, value=label)
        label_cell.font = S.TOTAL_FONT if bold else S.LABEL_FONT
        for offset, value in enumerate(values):
            cell = self.ws.cell(row=self.row, column=start_col + offset, value=_num(value))
            cell.font = S.TOTAL_FONT if bold else _font_for(kind)
            if fmt:
                cell.number_format = fmt
            if border is not None:
                cell.border = border
        row = self.row
        if key:
            self.refs[key] = f"{self.ws.title}!${get_column_letter(start_col)}${row}"
            self.refs[f"{key}_row"] = str(row)
            self.refs[f"{key}_start_col"] = str(start_col)
            self.refs[f"{key}_end_col"] = str(start_col + len(values) - 1)
        self.row += 1
        return row

    def headers(self, labels: list[str], start_col: int = 1) -> None:
        for offset, text in enumerate(labels):
            cell = self.ws.cell(row=self.row, column=start_col + offset, value=text)
            cell.font = S.HEADER_FONT
            cell.fill = S.HEADER_FILL
            cell.alignment = S.CENTER
        self.row += 1


def _font_for(kind: str):
    return {
        "input": S.INPUT_FONT,
        "formula": S.FORMULA_FONT,
        "link": S.LINK_FONT,
        "total": S.TOTAL_FONT,
    }.get(kind, S.FORMULA_FONT)


class ExcelModelBuilder:
    """Assemble the workbook from a completed valuation."""

    def __init__(
        self,
        result: Any,
        financials: Any = None,
        comps: Any = None,
        monte_carlo: Any = None,
        football: pd.DataFrame | None = None,
    ) -> None:
        self.result = result
        self.financials = financials
        self.comps = comps
        self.monte_carlo = monte_carlo
        self.football = football
        self.refs: dict[str, str] = {}
        self.years = len(result.pv_explicit)

    def _base_shares(self) -> float:
        """Share count before any forecast issuance.

        The workbook applies dilution itself, so it needs the undiluted starting
        point rather than the engine's already-diluted output.
        """
        from src.dcf.bridge import base_share_count
        from src.models.errors import ValuationError

        if self.financials is not None:
            try:
                return base_share_count(self.financials)
            except (ValueError, ValuationError):
                pass
        if self.result.assumptions.sbc.grow_share_count:
            raise ValueError(
                "financials are required to build a dilute-method workbook, because the "
                "undiluted share count cannot be recovered from the diluted result"
            )
        overhang = self.result.assumptions.sbc.option_overhang_shares or 0.0
        return self.result.bridge.shares - overhang

    # -------------------------------------------------------------------- build

    def build(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        book = self.workbook()
        book.save(path)
        return path

    def workbook(self) -> Workbook:
        """Build the workbook in memory, without writing it.

        Separate from `build` so the non-finite sweep below -- and the tests that
        check it -- can run against a workbook that still holds its float values.
        Once openpyxl has written and reloaded a file, every non-finite number has
        become None, so nothing downstream of `save` can tell whether one was there.
        """
        book = Workbook()
        book.remove(book.active)

        # Order matters: sheets that are referenced must exist before the formulas
        # that point at them are written.
        self._inputs(book.create_sheet("Inputs"))
        self._wacc(book.create_sheet("WACC"))
        self._dcf(book.create_sheet("DCF"))
        self._sensitivity(book.create_sheet("Sensitivity"))
        self._comps(book.create_sheet("Comps"))
        self._football(book.create_sheet("FootballField"))
        self._historicals(book.create_sheet("Historicals"))
        self._summary(book.create_sheet("Summary", 0))

        assert_all_cells_finite(book)
        return book

    # ------------------------------------------------------------------- inputs

    def _inputs(self, ws: Worksheet) -> None:
        S.col_width(ws, {"A": 42, "B": 16, "C": 14, "D": 14, "E": 14, "F": 14, "G": 14, "H": 14})
        cur = SheetCursor(ws, self.refs)
        result = self.result
        assumptions = result.assumptions
        proj = result.projection
        table = proj.table
        info = getattr(self.financials, "info", None) or {}

        cur.title(f"{result.ticker} - Valuation Inputs", width=8)
        cur.note("Blue cells are inputs and are safe to change. Every other sheet recalculates.")
        cur.skip()

        cur.section("Company", width=8)
        cur.label_value("Ticker", result.ticker, key="ticker")
        cur.label_value("Name", info.get("longName", result.ticker))
        cur.label_value("Scenario", assumptions.scenario)
        cur.label_value("Data source", getattr(self.financials, "source", "n/a"))
        # A valuation with no date on it is unreadable six months later: the reader
        # cannot tell whether a stale-looking price is an error or simply old. These
        # are two different dates and both matter -- when the model was run, and how
        # old the statements behind it are.
        cur.label_value("Analysis date", datetime.now().strftime("%Y-%m-%d"))
        cur.label_value("Financial data as of", _latest_period(self.financials))
        cur.label_value(
            "Current share price", result.bridge.current_price or 0.0, S.PRICE, key="price"
        )
        cur.skip()

        cur.section("Discount rate build-up", width=8)
        w = result.wacc
        cur.label_value("Risk-free rate", w.risk_free_rate, S.PERCENT_2, key="rf")
        cur.label_value("Equity risk premium", w.equity_risk_premium, S.PERCENT_2, key="erp")
        cur.label_value("Beta", w.beta, S.RATIO, key="beta")
        cur.label_value("Size premium", w.size_premium, S.PERCENT_2, key="size_prem")
        cur.label_value("Country risk premium", w.country_risk_premium, S.PERCENT_2, key="crp")
        cur.label_value("Cost of debt (pre-tax)", w.cost_of_debt, S.PERCENT_2, key="kd")
        cur.label_value("Tax rate", assumptions.projection.tax_rate, S.PERCENT, key="tax")
        cur.label_value("Market capitalisation", w.market_cap, S.MONEY_MM, key="mcap")
        cur.label_value("Total debt", w.total_debt, S.MONEY_MM, key="debt")
        cur.label_value("Cash and short-term investments", w.cash, S.MONEY_MM, key="cash")
        cur.label_value(
            "Capital structure (current / target)",
            assumptions.wacc.capital_structure,
            key="cap_structure",
        )
        cur.label_value(
            "Target debt weight (used only when target)",
            assumptions.wacc.target_debt_weight,
            S.PERCENT,
            key="target_wd",
        )
        cur.skip()

        cur.section("Share count", width=8)
        # Must be the UNDILUTED count. `result.bridge.shares` is already diluted when
        # the run used sbc.method = "dilute", and feeding that back into the
        # workbook's own dilution formula would dilute it a second time -- the same
        # double-count, one level up.
        cur.label_value(
            "Diluted shares (current, before forecast issuance)",
            self._base_shares(),
            S.SHARES_MM,
            key="shares_in",
        )
        cur.label_value(
            "Option overhang (treasury method)",
            assumptions.sbc.option_overhang_shares or 0.0,
            S.SHARES_MM,
            key="overhang",
        )
        cur.skip()

        cur.section("Stock-based compensation policy", width=8)
        cur.label_value("SBC method (expense / dilute)", assumptions.sbc.method, key="sbc_method")
        cur.note(
            "expense: SBC stays a cost inside EBIT, share count held flat. "
            "dilute: SBC added back, share count grows. Never both -- that double-counts."
        )
        cur.skip()

        cur.section("Projection drivers", width=8)
        year_labels = ["Base"] + [f"Year {i}" for i in range(1, self.years + 1)]
        cur.headers(year_labels, start_col=3)

        base_revenue = proj.drivers["base_revenue"]
        cur.series_row(
            "Revenue growth",
            [""] + [float(v) for v in table.loc["revenue_growth"]],
            S.PERCENT,
            key="growth",
            kind="input",
            start_col=3,
        )
        cur.series_row(
            "EBIT margin (GAAP, SBC included)",
            [""] + [float(v) for v in table.loc["ebit_margin"]],
            S.PERCENT,
            key="margin",
            kind="input",
            start_col=3,
        )
        cur.series_row(
            "D&A % of revenue",
            [""] + [float(v) / float(r) for v, r in zip(table.loc["da"], table.loc["revenue"], strict=True)],
            S.PERCENT,
            key="da_pct",
            kind="input",
            start_col=3,
        )
        cur.series_row(
            "Capex % of revenue",
            [""] + [float(v) / float(r) for v, r in zip(table.loc["capex"], table.loc["revenue"], strict=True)],
            S.PERCENT,
            key="capex_pct",
            kind="input",
            start_col=3,
        )
        cur.series_row(
            "Net working capital % of revenue",
            [proj.drivers["nwc_pct_revenue"]]
            + [float(v) / float(r) for v, r in zip(table.loc["nwc"], table.loc["revenue"], strict=True)],
            S.PERCENT,
            key="nwc_pct",
            kind="input",
            start_col=3,
        )
        cur.series_row(
            "SBC % of revenue",
            [""] + [float(v) / float(r) for v, r in zip(table.loc["sbc"], table.loc["revenue"], strict=True)],
            S.PERCENT,
            key="sbc_pct",
            kind="input",
            start_col=3,
        )
        cur.label_value("Base year revenue", base_revenue, S.MONEY_MM, key="base_revenue")
        cur.skip()

        cur.section("Terminal value", width=8)
        term = assumptions.terminal
        cur.label_value("Perpetuity growth rate", term.perpetuity_growth, S.PERCENT_2, key="g")
        exit_result = result.terminal_all.get("exit_multiple")
        multiple = (
            exit_result.multiple_used
            if exit_result and exit_result.multiple_used
            else term.static_exit_multiple
        )
        cur.label_value("Exit multiple (EV/EBITDA)", multiple, S.MULTIPLE, key="exit_mult")
        cur.label_value(
            "Mature industry floor", term.mature_industry_multiple, S.MULTIPLE, key="floor_mult"
        )
        cur.label_value(
            "Decay (turns per pp of growth lost)", term.decay_turns_per_pp, S.RATIO, key="decay"
        )
        cur.label_value(
            "Terminal method preference", term.method, key="tv_pref"
        )
        cur.label_value(
            "Mid-year convention (1 = yes)",
            1 if assumptions.projection.mid_year_convention else 0,
            S.INTEGER,
            key="midyear",
        )
        cur.skip()

        cur.section("Bridge items", width=8)
        cur.label_value("Minority interest", result.bridge.minority_interest, S.MONEY_MM, key="minority")
        cur.label_value("Preferred equity", result.bridge.preferred_equity, S.MONEY_MM, key="preferred")
        cur.label_value("Non-operating investments", result.bridge.investments, S.MONEY_MM, key="invest")

        ws.freeze_panes = "A3"

    # --------------------------------------------------------------------- wacc

    def _wacc(self, ws: Worksheet) -> None:
        S.col_width(ws, {"A": 42, "B": 16})
        cur = SheetCursor(ws, self.refs)
        r = self.refs

        cur.title("Weighted Average Cost of Capital", width=4)
        cur.note("Every figure below is a formula driven from the Inputs sheet.")
        cur.skip()

        cur.section("Cost of equity (CAPM)", width=4)
        cur.label_value("Risk-free rate", f"={r['rf']}", S.PERCENT_2, kind="link")
        cur.label_value("Beta", f"={r['beta']}", S.RATIO, kind="link")
        cur.label_value("Equity risk premium", f"={r['erp']}", S.PERCENT_2, kind="link")
        cur.label_value("Size premium", f"={r['size_prem']}", S.PERCENT_2, kind="link")
        cur.label_value("Country risk premium", f"={r['crp']}", S.PERCENT_2, kind="link")
        cur.label_value(
            "Cost of equity",
            f"={r['rf']}+{r['beta']}*{r['erp']}+{r['size_prem']}+{r['crp']}",
            S.PERCENT_2,
            key="coe",
            kind="total",
        )
        cur.note("No floor is applied: a negative beta legitimately gives a cost of equity below rf.")
        cur.skip()

        cur.section("Cost of debt", width=4)
        cur.label_value("Pre-tax cost of debt", f"={r['kd']}", S.PERCENT_2, kind="link")
        cur.label_value("Tax rate", f"={r['tax']}", S.PERCENT, kind="link")
        cur.label_value(
            "After-tax cost of debt",
            f"={r['kd']}*(1-{r['tax']})",
            S.PERCENT_2,
            key="atkd",
            kind="total",
        )
        cur.skip()

        cur.section("Capital structure", width=4)
        cur.label_value("Market capitalisation", f"={r['mcap']}", S.MONEY_MM, kind="link")
        cur.label_value("Total debt", f"={r['debt']}", S.MONEY_MM, kind="link")
        cur.label_value(
            "Total capital", f"={r['mcap']}+{r['debt']}", S.MONEY_MM, key="totcap", kind="formula"
        )
        # Mirrors WACCCalculator.debt_weight / equity_weight exactly, including the
        # target-structure branch. Hardcoding market weights here made the workbook
        # disagree with the Python valuation printed beside it by up to 17% whenever
        # capital_structure was set to "target".
        cur.label_value(
            "Debt weight",
            f'=IF({r["cap_structure"]}="target",{r["target_wd"]},'
            f"IF({r['totcap']}=0,0,{r['debt']}/{r['totcap']}))",
            S.PERCENT,
            key="wd",
        )
        cur.label_value("Equity weight", f"=1-{r['wd']}", S.PERCENT, key="we")
        cur.note(
            'Type "target" in the capital-structure cell on Inputs to weight the WACC on '
            "the target debt ratio instead of today's market weights."
        )
        cur.skip()

        cur.label_value(
            "WACC",
            f"={r['we']}*{r['coe']}+{r['wd']}*{r['atkd']}",
            S.PERCENT_2,
            key="wacc",
            kind="total",
        )
        ws.cell(row=cur.row - 1, column=2).border = S.TOTAL_BORDER

    # ---------------------------------------------------------------------- dcf

    def _dcf(self, ws: Worksheet) -> None:
        S.col_width(
            ws, {"A": 44, "B": 18, "C": 15, "D": 15, "E": 15, "F": 15, "G": 15, "H": 15, "I": 15}
        )
        cur = SheetCursor(ws, self.refs)
        r = self.refs
        n = self.years
        base_col = 3
        first = FIRST_FORECAST_COL
        last = first + n - 1
        letters = [get_column_letter(c) for c in range(first, last + 1)]
        base_letter = get_column_letter(base_col)

        cur.title(f"{self.result.ticker} - Discounted Cash Flow", width=last)
        cur.note("Black cells are formulas. Change any blue input and this sheet recalculates.")
        cur.skip()

        cur.headers(["Base"] + [f"Year {i}" for i in range(1, n + 1)], start_col=base_col)
        header_row = cur.row - 1

        # -- operating build ---------------------------------------------------
        cur.section("Unlevered free cash flow", width=last)

        rev_row = cur.row
        ws.cell(row=rev_row, column=1, value="Revenue").font = S.LABEL_FONT
        c = ws.cell(row=rev_row, column=base_col, value=f"={r['base_revenue']}")
        c.font, c.number_format = S.LINK_FONT, S.MONEY_MM
        for i in range(len(letters)):
            prev = base_letter if i == 0 else letters[i - 1]
            g = _cell(r["growth"], offset=i + 1)
            c = ws.cell(row=rev_row, column=first + i, value=f"={prev}{rev_row}*(1+{g})")
            c.font, c.number_format = S.FORMULA_FONT, S.MONEY_MM
        cur.row += 1

        ebit_row = self._driver_row(
            ws, cur, "EBIT (GAAP, after SBC)", rev_row, r["margin"], letters, first, S.MONEY_MM
        )
        sbc_row = self._driver_row(
            ws, cur, "Stock-based compensation", rev_row, r["sbc_pct"], letters, first, S.MONEY_MM
        )

        presbc_row = cur.row
        ws.cell(row=presbc_row, column=1, value="EBIT before SBC").font = S.LABEL_FONT
        for i, letter in enumerate(letters):
            c = ws.cell(
                row=presbc_row, column=first + i, value=f"={letter}{ebit_row}+{letter}{sbc_row}"
            )
            c.font, c.number_format = S.FORMULA_FONT, S.MONEY_MM
        cur.row += 1

        taxable_row = cur.row
        ws.cell(row=taxable_row, column=1, value="Taxable EBIT (per SBC method)").font = S.LABEL_FONT
        for i, letter in enumerate(letters):
            formula = (
                f'=IF({r["sbc_method"]}="dilute",{letter}{presbc_row},{letter}{ebit_row})'
            )
            c = ws.cell(row=taxable_row, column=first + i, value=formula)
            c.font, c.number_format = S.FORMULA_FONT, S.MONEY_MM
        cur.row += 1
        cur.note(
            'Type "dilute" in the SBC method cell on Inputs and this line, the cash flow '
            "and the share count all switch together."
        )

        tax_row = cur.row
        ws.cell(row=tax_row, column=1, value="(-) Taxes").font = S.LABEL_FONT
        for i, letter in enumerate(letters):
            c = ws.cell(
                row=tax_row, column=first + i, value=f"=-MAX({letter}{taxable_row},0)*{r['tax']}"
            )
            c.font, c.number_format = S.FORMULA_FONT, S.MONEY_MM
        cur.row += 1

        nopat_row = cur.row
        ws.cell(row=nopat_row, column=1, value="NOPAT").font = S.TOTAL_FONT
        for i, letter in enumerate(letters):
            c = ws.cell(row=nopat_row, column=first + i, value=f"={letter}{taxable_row}+{letter}{tax_row}")
            c.font, c.number_format, c.border = S.TOTAL_FONT, S.MONEY_MM, S.TOP_BORDER
        cur.row += 1

        da_row = self._driver_row(
            ws, cur, "(+) Depreciation & amortisation", rev_row, r["da_pct"], letters, first, S.MONEY_MM
        )
        capex_row = self._driver_row(
            ws, cur, "(-) Capital expenditure", rev_row, r["capex_pct"], letters, first,
            S.MONEY_MM, negate=True,
        )

        nwc_row = cur.row
        ws.cell(row=nwc_row, column=1, value="Net working capital").font = S.LABEL_FONT
        c = ws.cell(
            row=nwc_row,
            column=base_col,
            value=f"={base_letter}{rev_row}*{_cell(r['nwc_pct'], offset=0)}",
        )
        c.font, c.number_format = S.FORMULA_FONT, S.MONEY_MM
        for i, letter in enumerate(letters):
            c = ws.cell(
                row=nwc_row,
                column=first + i,
                value=f"={letter}{rev_row}*{_cell(r['nwc_pct'], offset=i + 1)}",
            )
            c.font, c.number_format = S.FORMULA_FONT, S.MONEY_MM
        cur.row += 1

        dnwc_row = cur.row
        ws.cell(row=dnwc_row, column=1, value="(-) Increase in working capital").font = S.LABEL_FONT
        for i, letter in enumerate(letters):
            prev = base_letter if i == 0 else letters[i - 1]
            c = ws.cell(row=dnwc_row, column=first + i, value=f"=-({letter}{nwc_row}-{prev}{nwc_row})")
            c.font, c.number_format = S.FORMULA_FONT, S.MONEY_MM
        cur.row += 1

        core_row = cur.row
        ws.cell(row=core_row, column=1, value="Unlevered FCF (per SBC method)").font = S.TOTAL_FONT
        for i, letter in enumerate(letters):
            c = ws.cell(
                row=core_row,
                column=first + i,
                value=f"={letter}{nopat_row}+{letter}{da_row}+{letter}{capex_row}+{letter}{dnwc_row}",
            )
            c.font, c.number_format, c.border = S.TOTAL_FONT, S.MONEY_MM, S.TOP_BORDER
        cur.row += 1

        adj_row = cur.row
        ws.cell(row=adj_row, column=1, value="Memo: adjusted FCF (SBC expensed)").font = S.LABEL_FONT
        for i, letter in enumerate(letters):
            formula = (
                f'=IF({r["sbc_method"]}="dilute",'
                f"{letter}{core_row}-{letter}{sbc_row}*(1-{r['tax']}),{letter}{core_row})"
            )
            c = ws.cell(row=adj_row, column=first + i, value=formula)
            c.font, c.number_format = S.FORMULA_FONT, S.MONEY_MM
        cur.row += 1

        neutral_row = cur.row
        ws.cell(row=neutral_row, column=1, value="Memo: SBC-neutral FCF (SBC added back)").font = S.LABEL_FONT
        for i, letter in enumerate(letters):
            c = ws.cell(
                row=neutral_row,
                column=first + i,
                value=f"={letter}{adj_row}+{letter}{sbc_row}*(1-{r['tax']})",
            )
            c.font, c.number_format = S.FORMULA_FONT, S.MONEY_MM
        cur.row += 1
        cur.note(
            "Reported FCF (CFO less capex) adds SBC back and stops there. It is shown on the "
            "Summary sheet for contrast and is never discounted."
        )
        cur.skip()

        # -- discounting -------------------------------------------------------
        cur.section("Discounting", width=last)
        idx_row = cur.row
        ws.cell(row=idx_row, column=1, value="Year index").font = S.LABEL_FONT
        for i in range(len(letters)):
            prev = letters[i - 1] if i else None
            value = "=1" if i == 0 else f"={prev}{idx_row}+1"
            c = ws.cell(row=idx_row, column=first + i, value=value)
            c.font, c.number_format = S.FORMULA_FONT, S.INTEGER
        cur.row += 1

        exp_row = cur.row
        ws.cell(row=exp_row, column=1, value="Discount period (years)").font = S.LABEL_FONT
        for i, letter in enumerate(letters):
            c = ws.cell(
                row=exp_row,
                column=first + i,
                value=f"={letter}{idx_row}-IF({r['midyear']}=1,0.5,0)",
            )
            c.font, c.number_format = S.FORMULA_FONT, S.RATIO
        cur.row += 1

        df_row = cur.row
        ws.cell(row=df_row, column=1, value="Discount factor").font = S.LABEL_FONT
        for i, letter in enumerate(letters):
            c = ws.cell(row=df_row, column=first + i, value=f"=1/(1+{r['wacc']})^{letter}{exp_row}")
            c.font, c.number_format = S.FORMULA_FONT, S.RATIO
        cur.row += 1

        pv_row = cur.row
        ws.cell(row=pv_row, column=1, value="PV of unlevered FCF").font = S.LABEL_FONT
        for i, letter in enumerate(letters):
            c = ws.cell(row=pv_row, column=first + i, value=f"={letter}{core_row}*{letter}{df_row}")
            c.font, c.number_format = S.FORMULA_FONT, S.MONEY_MM
        cur.row += 1

        sum_pv = cur.label_value(
            "Sum of PV, explicit period",
            f"=SUM({get_column_letter(first)}{pv_row}:{get_column_letter(last)}{pv_row})",
            S.MONEY_MM,
            key="sum_pv",
            kind="total",
        )
        cur.skip()

        # -- terminal value ----------------------------------------------------
        cur.section("Terminal value", width=last)
        last_letter = letters[-1]
        ebitda_ref = cur.label_value(
            "Terminal year EBITDA",
            f"={last_letter}{ebit_row}+{last_letter}{da_row}",
            S.MONEY_MM,
            key="term_ebitda",
        )
        term_fcf = cur.label_value(
            "Terminal year FCF (SBC expensed)",
            f"={last_letter}{adj_row}",
            S.MONEY_MM,
            key="term_fcf",
        )
        cur.note(
            "The terminal value always uses the SBC-expensed cash flow. Capitalising an "
            "added-back SBC into perpetuity while charging only five years of dilution "
            "against it would inflate the answer by an accounting choice."
        )
        gordon = cur.label_value(
            "Terminal value - perpetuity growth",
            f"=IF({r['wacc']}<={r['g']},NA(),{term_fcf}*(1+{r['g']})/({r['wacc']}-{r['g']}))",
            S.MONEY_MM,
            key="tv_gordon",
        )
        exit_tv = cur.label_value(
            "Terminal value - exit multiple",
            f"={ebitda_ref}*{r['exit_mult']}",
            S.MONEY_MM,
            key="tv_exit",
        )
        cur.label_value(
            "Implied exit multiple from perpetuity method",
            f"=IF({ebitda_ref}<=0,NA(),{gordon}/{ebitda_ref})",
            S.MULTIPLE,
            key="implied_mult",
        )
        cur.label_value(
            "Implied perpetuity growth from exit multiple",
            f"=IF(({exit_tv}+{term_fcf})=0,NA(),"
            f"({exit_tv}*{r['wacc']}-{term_fcf})/({exit_tv}+{term_fcf}))",
            S.PERCENT_2,
            key="implied_g",
        )
        cur.note(
            "The cross-check that catches a multiple applied without thinking: every exit "
            "multiple asserts a perpetuity growth rate, and it has to be believable."
        )
        selected = cur.label_value(
            "Terminal value used",
            f'=IF({r["tv_pref"]}="exit_multiple",{exit_tv},{gordon})',
            S.MONEY_MM,
            key="tv_used",
            kind="total",
        )
        # The discount period depends on what the terminal value IS. Gordon is a
        # perpetuity of flows and inherits the mid-year convention; an exit multiple is
        # a sale price at the end of year N and discounts at the full N.
        pv_tv = cur.label_value(
            "PV of terminal value",
            f'=IF({r["tv_pref"]}="exit_multiple",'
            f"{selected}/(1+{r['wacc']})^{last_letter}{idx_row},"
            f"{selected}/(1+{r['wacc']})^{last_letter}{exp_row})",
            S.MONEY_MM,
            key="pv_tv",
            kind="total",
        )
        cur.note(
            "An exit multiple is a sale price at a point in time, so it discounts at the "
            "full year count. Applying the mid-year factor to it overstates the terminal "
            "value by about 4.9% at a 10% discount rate."
        )
        cur.skip()

        # -- bridge ------------------------------------------------------------
        cur.section("Enterprise value to equity value", width=last)
        ev = cur.label_value(
            "Enterprise value", f"={sum_pv}+{pv_tv}", S.MONEY_MM, key="ev", kind="total"
        )
        cur.label_value(
            "Terminal value as % of EV", f"={pv_tv}/{ev}", S.PERCENT, key="tv_pct"
        )
        cur.label_value("(-) Total debt", f"=-{r['debt']}", S.MONEY_MM, kind="link")
        cur.label_value("(+) Cash and short-term investments", f"={r['cash']}", S.MONEY_MM, kind="link")
        cur.label_value("(-) Minority interest", f"=-{r['minority']}", S.MONEY_MM, kind="link")
        cur.label_value("(-) Preferred equity", f"=-{r['preferred']}", S.MONEY_MM, kind="link")
        cur.label_value("(+) Non-operating investments", f"={r['invest']}", S.MONEY_MM, kind="link")
        equity = cur.label_value(
            "Equity value",
            f"={ev}-{r['debt']}+{r['cash']}-{r['minority']}-{r['preferred']}+{r['invest']}",
            S.MONEY_MM,
            key="equity",
            kind="total",
        )
        ws.cell(row=cur.row - 1, column=2).border = S.TOP_BORDER

        sbc_pv = cur.label_value(
            "Memo: PV of future SBC (at cost of equity)",
            f"=SUMPRODUCT({get_column_letter(first)}{sbc_row}:{get_column_letter(last)}{sbc_row},"
            f"1/((1+{r['coe']})^{get_column_letter(first)}{idx_row}:{get_column_letter(last)}{idx_row}))",
            S.MONEY_MM,
            key="sbc_pv",
        )
        # The overhang is added under BOTH branches. Options already granted vest
        # whatever the model assumes about future grants; the two treatments differ
        # only in how they charge future ones. Adding it to the expense branch alone
        # understated the dilute share count by exactly the overhang -- 4.8% on AAPL
        # at a 5% overhang, worth 5.0% on value per share.
        opening = f"({r['shares_in']}+{r['overhang']})"
        shares = cur.label_value(
            "(/) Diluted shares",
            f'=IF({r["sbc_method"]}="dilute",'
            f"{opening}/(1-{sbc_pv}/{equity}),"
            f"{opening})",
            S.SHARES_MM,
            key="shares_out",
        )
        cur.note(
            "Under dilute the share count solves the circularity in closed form: shares "
            "issued depend on the price, which depends on the share count, and the fixed "
            "point resolves to S = S0 / (1 - PV(SBC) / equity value). No iterative "
            "calculation setting required, and it matches the Python solver exactly."
        )
        cur.note(
            "Equivalently: diluting is just subtracting the present value of the stock you "
            "will hand employees from equity value -- which is why also expensing SBC would "
            "charge the same cost twice."
        )
        cur.label_value(
            "Implied value per share", f"={equity}/{shares}", S.PRICE, key="vps", kind="total"
        )
        ws.cell(row=cur.row - 1, column=2).border = S.TOTAL_BORDER
        cur.label_value("Current share price", f"={r['price']}", S.PRICE, kind="link")
        cur.label_value(
            "Upside / (downside)",
            f"=IF({r['price']}=0,NA(),{self.refs['vps']}/{r['price']}-1)",
            S.PERCENT,
            key="upside",
        )

        # Net bridge helper used by the sensitivity sheet.
        cur.label_value(
            "Memo: net debt and other bridge items",
            f"={r['debt']}-{r['cash']}+{r['minority']}+{r['preferred']}-{r['invest']}",
            S.MONEY_MM,
            key="net_bridge",
        )
        self.refs["fcf_row"] = str(core_row)
        self.refs["adj_row"] = str(adj_row)
        self.refs["exp_row"] = str(exp_row)
        self.refs["first_letter"] = get_column_letter(first)
        self.refs["last_letter"] = get_column_letter(last)
        ws.freeze_panes = f"{get_column_letter(base_col)}{header_row + 1}"

    def _driver_row(
        self,
        ws: Worksheet,
        cur: SheetCursor,
        label: str,
        revenue_row: int,
        driver_ref: str,
        letters: list[str],
        first_col: int,
        fmt: str,
        negate: bool = False,
    ) -> int:
        """Write `revenue x driver` for each forecast year."""
        row = cur.row
        ws.cell(row=row, column=1, value=label).font = S.LABEL_FONT
        sign = "-" if negate else ""
        for i, letter in enumerate(letters):
            driver = _cell(driver_ref, offset=i + 1)
            c = ws.cell(row=row, column=first_col + i, value=f"={sign}{letter}{revenue_row}*{driver}")
            c.font, c.number_format = S.FORMULA_FONT, fmt
        cur.row += 1
        return row

    # -------------------------------------------------------------- sensitivity

    def _sensitivity(self, ws: Worksheet) -> None:
        S.col_width(ws, {"A": 26, "B": 12, "C": 12, "D": 12, "E": 12, "F": 12, "G": 12, "H": 12})
        cur = SheetCursor(ws, self.refs)
        r = self.refs

        cur.title("Sensitivity Analysis", width=8)
        cur.note(
            "Live formulas, not pasted values. Each cell rebuilds the valuation at that "
            "discount rate and terminal assumption straight from the DCF sheet."
        )
        cur.skip()

        base_wacc = self.result.wacc.wacc
        base_growth = self.result.assumptions.terminal.perpetuity_growth
        cfg = self.result.assumptions.sensitivity

        cur.section("Value per share: WACC against perpetuity growth", width=8)
        growths = [base_growth + d for d in cfg.growth_deltas]
        waccs = [base_wacc + d for d in cfg.wacc_deltas]

        header_row = cur.row
        ws.cell(row=header_row, column=1, value="WACC \\ growth").font = S.HEADER_FONT
        ws.cell(row=header_row, column=1).fill = S.HEADER_FILL
        for j, g in enumerate(growths):
            c = ws.cell(row=header_row, column=2 + j, value=g)
            c.font, c.fill, c.number_format = S.HEADER_FONT, S.HEADER_FILL, S.PERCENT_2
        cur.row += 1

        first_l, last_l = r["first_letter"], r["last_letter"]
        fcf_range = f"DCF!${first_l}${r['fcf_row']}:${last_l}${r['fcf_row']}"
        exp_range = f"DCF!${first_l}${r['exp_row']}:${last_l}${r['exp_row']}"
        last_adj = f"DCF!${last_l}${r['adj_row']}"
        last_exp = f"DCF!${last_l}${r['exp_row']}"

        for wacc in waccs:
            row = cur.row
            c = ws.cell(row=row, column=1, value=wacc)
            c.font, c.number_format = S.INPUT_FONT, S.PERCENT_2
            for j, _ in enumerate(growths):
                g_cell = f"{get_column_letter(2 + j)}${header_row}"
                w_cell = f"$A{row}"
                formula = (
                    f'=IF({w_cell}<={g_cell},"n/a",'
                    f"(SUMPRODUCT({fcf_range},1/((1+{w_cell})^{exp_range}))"
                    f"+{last_adj}*(1+{g_cell})/({w_cell}-{g_cell})/(1+{w_cell})^{last_exp}"
                    f"-{r['net_bridge']})/{r['shares_out']})"
                )
                cell = ws.cell(row=row, column=2 + j, value=formula)
                cell.font, cell.number_format = S.FORMULA_FONT, S.PRICE
            cur.row += 1
        cur.skip()

        cur.section("Value per share: WACC against exit multiple", width=8)
        exit_result = self.result.terminal_all.get("exit_multiple")
        base_multiple = (
            exit_result.multiple_used
            if exit_result and exit_result.multiple_used
            else self.result.assumptions.terminal.static_exit_multiple
        )
        multiples = [max(base_multiple + d, 0.5) for d in cfg.multiple_deltas]

        header2 = cur.row
        ws.cell(row=header2, column=1, value="WACC \\ multiple").font = S.HEADER_FONT
        ws.cell(row=header2, column=1).fill = S.HEADER_FILL
        for j, m in enumerate(multiples):
            c = ws.cell(row=header2, column=2 + j, value=m)
            c.font, c.fill, c.number_format = S.HEADER_FONT, S.HEADER_FILL, S.MULTIPLE
        cur.row += 1

        term_ebitda = r["term_ebitda"]
        for wacc in waccs:
            row = cur.row
            c = ws.cell(row=row, column=1, value=wacc)
            c.font, c.number_format = S.INPUT_FONT, S.PERCENT_2
            for j, _ in enumerate(multiples):
                m_cell = f"{get_column_letter(2 + j)}${header2}"
                w_cell = f"$A{row}"
                formula = (
                    f"=(SUMPRODUCT({fcf_range},1/((1+{w_cell})^{exp_range}))"
                    f"+{term_ebitda}*{m_cell}/(1+{w_cell})^{last_exp}"
                    f"-{r['net_bridge']})/{r['shares_out']}"
                )
                cell = ws.cell(row=row, column=2 + j, value=formula)
                cell.font, cell.number_format = S.FORMULA_FONT, S.PRICE
            cur.row += 1

        ws.freeze_panes = "B4"

    # -------------------------------------------------------------------- comps

    def _comps(self, ws: Worksheet) -> None:
        S.col_width(ws, {"A": 14, "B": 18, "C": 18, "D": 14, "E": 14, "F": 14, "G": 14, "H": 16})
        cur = SheetCursor(ws, self.refs)
        cur.title("Comparable Companies", width=8)

        if self.comps is None or self.comps.table.empty:
            cur.note("No comparable-company data was available for this run.")
            return

        cur.note(f"Peer set source: {self.comps.peer_source}")
        cur.skip()

        columns = [
            ("market_cap", "Market cap", S.MONEY_MM),
            ("enterprise_value", "Enterprise value", S.MONEY_MM),
            ("ev_ebitda", "EV/EBITDA", S.MULTIPLE),
            ("ev_ebit", "EV/EBIT", S.MULTIPLE),
            ("ev_revenue", "EV/Revenue", S.MULTIPLE),
            ("pe", "P/E", S.MULTIPLE),
            ("revenue_growth", "Revenue growth", S.PERCENT),
            ("ebitda_margin", "EBITDA margin", S.PERCENT),
        ]
        cur.headers(["Ticker"] + [label for _, label, _ in columns])

        for ticker, row in self.comps.table.iterrows():
            is_target = ticker == self.comps.target
            cell = ws.cell(row=cur.row, column=1, value=str(ticker))
            cell.font = S.TOTAL_FONT if is_target else S.LABEL_FONT
            for j, (key, _, fmt) in enumerate(columns):
                value = row.get(key)
                cell = ws.cell(
                    row=cur.row,
                    column=2 + j,
                    value=None if value is None or pd.isna(value) else float(value),
                )
                cell.font = S.TOTAL_FONT if is_target else S.FORMULA_FONT
                cell.number_format = fmt
            cur.row += 1

        cur.skip()
        cur.section("Peer medians (target excluded)", width=9)
        for key, label, fmt in columns[2:]:
            median = self.comps.medians.get(f"{key}_median")
            if median is None:
                continue
            cur.label_value(label, float(median), fmt, kind="formula")
        cur.label_value("Screened peer count", float(self.comps.peer_count), S.INTEGER, kind="formula")

        notes = self.comps.notes()
        if notes:
            cur.skip()
            cur.section("Notes", width=9)
            for note in notes:
                cur.note(note, warn=True)

    # ----------------------------------------------------------- football field

    def _football(self, ws: Worksheet) -> None:
        S.col_width(ws, {"A": 34, "B": 14, "C": 14, "D": 14, "E": 14})
        cur = SheetCursor(ws, self.refs)
        cur.title("Valuation Summary - Football Field", width=5)
        cur.note("Each bar is a range. A valuation is a range; a point estimate is a guess.")
        cur.skip()

        if self.football is None or self.football.empty:
            cur.note("No valuation ranges were produced for this run.")
            return

        cur.headers(["Method", "Low", "Span", "High", "Midpoint"])
        first_data = cur.row

        for _, row in self.football.iterrows():
            low = _num(row.get("low"))
            high = _num(row.get("high"))
            mid = _num(row.get("midpoint"))
            ws.cell(row=cur.row, column=1, value=str(row["method"])).font = S.LABEL_FONT
            for col, value, fmt in (
                (2, low, S.PRICE),
                (3, None if low is None or high is None else high - low, S.PRICE),
                (4, high, S.PRICE),
                (5, mid, S.PRICE),
            ):
                cell = ws.cell(row=cur.row, column=col, value=value)
                cell.font = S.FORMULA_FONT
                cell.number_format = fmt
            cur.row += 1

        last_data = cur.row - 1

        # A floating bar: an invisible base up to `low`, then a visible span.
        chart = BarChart()
        chart.type = "bar"
        chart.grouping = "stacked"
        chart.overlap = 100
        chart.title = "Implied value per share"
        chart.y_axis.title = "US$ per share"
        chart.height, chart.width = 9, 18

        data = Reference(ws, min_col=2, max_col=3, min_row=first_data - 1, max_row=last_data)
        categories = Reference(ws, min_col=1, min_row=first_data, max_row=last_data)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(categories)
        if chart.series:
            chart.series[0].graphicalProperties.noFill = True
            chart.series[0].graphicalProperties.line.noFill = True
        ws.add_chart(chart, f"A{last_data + 3}")

    # -------------------------------------------------------------- historicals

    def _historicals(self, ws: Worksheet) -> None:
        cur = SheetCursor(ws, self.refs)
        cur.title("Reported Historical Financials", width=8)
        cur.note("As reported by the data source, mapped to canonical line items.")
        cur.skip()

        if self.financials is None or self.financials.statements.empty:
            cur.note("No historical statements were loaded.")
            return

        statements = self.financials.statements
        S.col_width(ws, {"A": 34})
        cur.headers(
            ["Line item"]
            + [str(p)[:10] if not hasattr(p, "date") else p.date().isoformat() for p in statements.columns]
        )
        for col in range(2, statements.shape[1] + 2):
            ws.column_dimensions[get_column_letter(col)].width = 16

        for name in statements.index:
            ws.cell(row=cur.row, column=1, value=name).font = S.LABEL_FONT
            for j, period in enumerate(statements.columns):
                value = statements.loc[name, period]
                cell = ws.cell(
                    row=cur.row, column=2 + j, value=None if pd.isna(value) else float(value)
                )
                cell.font = S.INPUT_FONT
                cell.number_format = S.MONEY_MM
            cur.row += 1

        ws.freeze_panes = "B5"

    # ------------------------------------------------------------------ summary

    def _summary(self, ws: Worksheet) -> None:
        S.col_width(ws, {"A": 46, "B": 20, "C": 20})
        cur = SheetCursor(ws, self.refs)
        r = self.refs
        result = self.result

        cur.title(f"{result.ticker} - Valuation Summary", width=4)
        cur.note(
            "Every figure links to the DCF sheet. Blue cells on Inputs are the only "
            "hardcoded numbers in the workbook."
        )
        cur.skip()

        cur.section("Conclusion", width=4)
        cur.label_value("Implied value per share", f"={r['vps']}", S.PRICE, kind="link")
        cur.label_value("Current share price", f"={r['price']}", S.PRICE, kind="link")
        cur.label_value("Upside / (downside)", f"={r['upside']}", S.PERCENT, kind="link")
        cur.skip()

        cur.section("Valuation build", width=4)
        cur.label_value("Enterprise value", f"={r['ev']}", S.MONEY_MM, kind="link")
        cur.label_value("Equity value", f"={r['equity']}", S.MONEY_MM, kind="link")
        cur.label_value("Diluted shares", f"={r['shares_out']}", S.SHARES_MM, kind="link")
        cur.label_value("WACC", f"={r['wacc']}", S.PERCENT_2, kind="link")
        cur.label_value("Terminal value as % of EV", f"={r['tv_pct']}", S.PERCENT, kind="link")
        cur.skip()

        cur.section("Free cash flow definitions", width=4)
        reported = result.projection.reported_fcf_latest
        cur.label_value(
            "Reported FCF (CFO less capex, latest year)",
            None if pd.isna(reported) else float(reported),
            S.MONEY_MM,
            kind="input",
        )
        cur.label_value(
            "Adjusted FCF, year 1 (SBC expensed)",
            float(result.projection.adjusted_fcf.iloc[0]),
            S.MONEY_MM,
            kind="input",
        )
        cur.label_value(
            "SBC-neutral FCF, year 1 (SBC added back)",
            float(result.projection.sbc_neutral_fcf.iloc[0]),
            S.MONEY_MM,
            kind="input",
        )
        cur.label_value("SBC treatment used", result.assumptions.sbc.method, kind="input")
        cur.note(
            "Reported FCF treats stock compensation as free. This model does not: it is "
            "charged either against cash flow or against the share count, never both."
        )
        cur.skip()

        if self.financials is not None:
            try:
                from src.dcf.moat import analyze_moat
                from src.dcf.reverse_dcf import solve_reverse_dcf

                moat = analyze_moat(self.financials, result)
                rev = solve_reverse_dcf(
                    self.financials,
                    result.assumptions,
                    self.comps.terminal_inputs() if self.comps else {},
                    ticker=result.ticker,
                )

                cur.section("Economic Moat & Capital Efficiency", width=4)
                cur.label_value(
                    "Invested capital (base)", moat.invested_capital_base, S.MONEY_MM, kind="input"
                )
                cur.label_value("Base ROIC", moat.roic_base, S.PERCENT_2, kind="input")
                cur.label_value(
                    "Economic spread (ROIC - WACC)", moat.economic_spread, S.PERCENT_2, kind="input"
                )
                cur.label_value("Moat assessment", moat.moat_rating, kind="input")
                cur.skip()

                cur.section("Market Expectations & Margin of Safety", width=4)
                if rev.implied_revenue_growth_cagr is not None:
                    cur.label_value(
                        "Market-implied 5-yr revenue CAGR",
                        rev.implied_revenue_growth_cagr,
                        S.PERCENT_2,
                        kind="input",
                    )
                else:
                    cur.label_value(
                        "Market-implied 5-yr revenue CAGR", rev.implied_revenue_status, kind="input"
                    )

                if rev.implied_perpetuity_growth is not None:
                    cur.label_value(
                        "Market-implied perpetuity growth (g)",
                        rev.implied_perpetuity_growth,
                        S.PERCENT_2,
                        kind="input",
                    )

                vps_ref = r["vps"]
                cur.label_value(
                    "Target entry (15% moat discount)", f"={vps_ref}*0.85", S.PRICE, kind="link"
                )
                cur.label_value(
                    "Target entry (25% standard discount)", f"={vps_ref}*0.75", S.PRICE, kind="link"
                )
                cur.label_value(
                    "Target entry (35% deep value discount)", f"={vps_ref}*0.65", S.PRICE, kind="link"
                )
                cur.skip()
            except Exception:
                pass

        if self.monte_carlo:
            cur.section("Monte Carlo (WACC, terminal growth, EBIT margin)", width=4)
            for key, label, fmt in (
                ("p10", "10th percentile", S.PRICE),
                ("p50", "Median", S.PRICE),
                ("p90", "90th percentile", S.PRICE),
                ("prob_above_market", "Probability above market price", S.PERCENT),
            ):
                if key in self.monte_carlo:
                    cur.label_value(label, float(self.monte_carlo[key]), fmt, kind="input")
            cur.skip()

        if result.warnings:
            cur.section("Model warnings", width=4)
            for message in result.warnings:
                cur.note(message, warn=True)

        ws.freeze_panes = "A3"


# ------------------------------------------------------------------- utilities


def _cell(range_ref: str, offset: int) -> str:
    """Shift a stored `Sheet!$C$12` reference `offset` columns to the right."""
    sheet, _, addr = range_ref.partition("!")
    column_letter = addr.split("$")[1]
    row = addr.split("$")[2]
    from openpyxl.utils import column_index_from_string

    new_col = get_column_letter(column_index_from_string(column_letter) + offset)
    return f"{sheet}!${new_col}${row}"


def _num(value: Any) -> float | None:
    """Coerce to a float Excel can hold.

    NaN becomes None, because NaN means "not reported" everywhere else in this
    codebase and a blank cell is the honest rendering of that.

    An infinity raises. It is never a legitimate figure in a valuation -- it means
    something upstream divided by zero -- and the alternatives are both worse than an
    error. Writing it produces `<c t="n"><v></v></c>`, a numeric cell holding nothing,
    which Excel may offer to repair. Quietly blanking it, which this function used to
    do, leaves a Summary line empty and every formula pointing at it reading zero:
    a confidently-presented wrong number with no warning, which is the exact failure
    mode this model is built to refuse.
    """
    if value is None or pd.isna(value):
        return None
    if not isinstance(value, (int, float, np.integer, np.floating)):
        return value
    number = float(value)
    if math.isinf(number):
        raise ValueError(
            "an infinite value reached the workbook, which means something upstream "
            "divided by zero. Excel cannot hold it and blanking it would leave a cell "
            "that reads as zero to every formula pointing at it."
        )
    return number


def _latest_period(financials: Any) -> str:
    """Period end of the most recent statement, for the "data as of" line."""
    statements = getattr(financials, "statements", None)
    if statements is None or not len(getattr(statements, "columns", [])):
        return "n/a"
    latest = statements.columns[-1]
    return latest.date().isoformat() if hasattr(latest, "date") else str(latest)[:10]


def assert_all_cells_finite(book: Workbook) -> None:
    """Fail loudly if any cell in an unsaved workbook holds a non-finite number.

    This is the only point at which the check is possible. `_num` sanitises the values
    that pass through it, but it guarded three call sites out of roughly forty when
    the second audit ran -- every other numeric write went straight to `ws.cell`. The
    two tests that claimed to cover this asserted on a *reloaded* workbook, where
    openpyxl has already replaced every non-finite value with None, so the guard
    `isinstance(cell.value, float)` was False and the assertion never executed.
    """
    offenders = []
    for sheet in book.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                value = cell.value
                if isinstance(value, float) and not math.isfinite(value):
                    offenders.append(f"{sheet.title}!{cell.coordinate} = {value}")
    if offenders:
        raise ValueError(
            "non-finite values would be written to the workbook, where they become "
            f"blank numeric cells no reader can distinguish from a real gap: {offenders[:10]}"
        )


def build_excel_model(
    result: Any,
    path: str | Path,
    financials: Any = None,
    comps: Any = None,
    monte_carlo: dict[str, float] | None = None,
    football: pd.DataFrame | None = None,
) -> Path:
    """Build the workbook.

    The Sensitivity sheet is generated from `result` and its assumptions rather than
    from a precomputed table: its cells are live Excel formulas, not pasted values, so
    a Python-side table would have nothing to contribute. The old `sensitivity` and
    `exit_sensitivity` parameters were accepted and silently discarded, which cost the
    CLI ~50 engine re-runs per model for nothing.
    """
    builder = ExcelModelBuilder(
        result=result,
        financials=financials,
        comps=comps,
        monte_carlo=monte_carlo,
        football=football,
    )
    return builder.build(path)
