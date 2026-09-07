"""Revenue-to-cash-flow projection.

Reported FCF is CFO less capex. Adjusted unlevered FCF uses after-SBC
operating earnings less cash taxes, plus D&A less capex and operating NWC
investment. The dilution policy adds back gross SBC and deducts repurchase
cash, preserving the SBC tax deduction. See RELIABILITY.md for tax and margin
basis assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.models.assumptions import DCFAssumptions, expand_series
from src.models.financials import Financials, field_value
from src.models.quality_gate import QualityReport, run_quality_gate

LINE_ORDER = [
    "revenue",
    "revenue_growth",
    "ebit",
    "ebit_margin",
    "sbc",
    "ebit_pre_sbc",
    "taxes",
    "nol_opening",
    "nol_used",
    "nol_ending",
    "nopat",
    "da",
    "capex",
    "nwc",
    "nwc_investment",
    "adjusted_fcf",
    "sbc_neutral_fcf",
    "unlevered_fcf",
]


@dataclass
class ProjectionResult:
    """Forecast line items, one column per projection year."""

    table: pd.DataFrame
    reported_fcf_latest: float
    drivers: dict[str, Any]
    quality: QualityReport

    @property
    def years(self) -> list[int]:
        return list(self.table.columns)

    def line(self, name: str) -> pd.Series:
        return self.table.loc[name]

    @property
    def adjusted_fcf(self) -> pd.Series:
        return self.table.loc["adjusted_fcf"]

    @property
    def sbc_neutral_fcf(self) -> pd.Series:
        return self.table.loc["sbc_neutral_fcf"]

    @property
    def unlevered_fcf(self) -> pd.Series:
        """The series the DCF actually discounts, chosen by sbc.method."""
        return self.table.loc["unlevered_fcf"]

    @property
    def terminal_ebitda(self) -> float:
        return terminal_year_ebitda(self)


class Projector:
    """Build the forecast. Runs the data quality gate before any arithmetic."""

    def __init__(
        self,
        financials: Any,
        assumptions: DCFAssumptions | None = None,
        *,
        check_quality: bool = True,
    ) -> None:
        self.financials = financials
        self.assumptions = assumptions or DCFAssumptions()

        if check_quality:
            self.quality = run_quality_gate(
                financials,
                min_history_years=self.assumptions.quality.min_history_years,
                max_sbc_pct_revenue=self.assumptions.quality.max_sbc_pct_revenue,
                # Conversion is not flagged here. Converting rewrites the statement
                # currency to the price currency, so the coherence check then passes
                # on its own -- only an explicit opt-out has to be carried in.
                currency_reconciled=self.assumptions.currency.allow_mismatch,
                allow_unsuitable_sector=self.assumptions.quality.allow_unsuitable_sector,
            )
        else:
            self.quality = QualityReport()

        self.result = self._project()

    # ------------------------------------------------------------------ helpers

    def _hist(self, name: str, default: float | None = None) -> float:
        return field_value(self.financials, name, default)

    def _hist_pct_revenue(self, name: str, fallback: float = 0.0) -> float:
        """Trailing average of a field over revenue, falling back when unavailable."""
        if isinstance(self.financials, Financials):
            ratio = self.financials.mean_pct_revenue(name)
            if pd.notna(ratio):
                return float(ratio)
        value = self._hist(name)
        revenue = self._hist("revenue")
        if pd.notna(value) and pd.notna(revenue) and revenue != 0:
            return float(value / revenue)
        return fallback

    # --------------------------------------------------------------- projection

    def _project(self) -> ProjectionResult:
        proj = self.assumptions.projection
        sbc_cfg = self.assumptions.sbc
        years = proj.years
        idx = list(range(1, years + 1))

        base_revenue = self._hist("revenue")
        if pd.isna(base_revenue) or base_revenue == 0:
            raise ValueError("cannot project without a positive base revenue")

        growth = expand_series(proj.revenue_growth, years, 0.05)

        base_ebit = self._hist("ebit")
        base_margin = (
            float(base_ebit / base_revenue) if pd.notna(base_ebit) and base_revenue else 0.10
        )
        if proj.margin_basis == "before_sbc" and proj.ebit_margin is None:
            historical_sbc = self._hist("sbc")
            if pd.notna(historical_sbc):
                base_margin += historical_sbc / base_revenue
        margins = expand_series(proj.ebit_margin, years, base_margin)

        da_pct = expand_series(proj.da_pct_revenue, years, self._hist_pct_revenue("da", 0.03))
        capex_pct = expand_series(
            proj.capex_pct_revenue, years, self._hist_pct_revenue("capex", 0.03)
        )
        if proj.fade_capex_to_da and years > 1:
            start_ratio = capex_pct[0]
            target_ratio = proj.terminal_capex_to_da * da_pct[-1]
            capex_pct = [
                start_ratio + (target_ratio - start_ratio) * (i / (years - 1)) for i in range(years)
            ]
        nwc_pct = expand_series(proj.nwc_pct_revenue, years, self._base_nwc_pct())

        sbc_series = self._forecast_sbc(years, growth, base_revenue)

        # revenue path
        revenue: list[float] = []
        prior = base_revenue
        for g in growth:
            prior = prior * (1.0 + g)
            revenue.append(prior)

        ebit = [r * m for r, m in zip(revenue, margins, strict=True)]
        if proj.margin_basis == "before_sbc":
            ebit = [e - s for e, s in zip(ebit, sbc_series, strict=True)]
            margins = [e / r for e, r in zip(ebit, revenue, strict=True)]
        ebit_pre_sbc = [e + s for e, s in zip(ebit, sbc_series, strict=True)]

        tax_rate = proj.tax_rate
        # SBC remains deductible in the cash-tax schedule in both treatments.
        from src.dcf.accounting import cash_tax_schedule

        taxes, nol_opening, nol_used, nol_ending = cash_tax_schedule(
            ebit, tax_rate, proj.starting_nol
        )
        nopat = [e - t for e, t in zip(ebit, taxes, strict=True)]

        da = [r * p for r, p in zip(revenue, da_pct, strict=True)]
        capex = [r * p for r, p in zip(revenue, capex_pct, strict=True)]

        nwc = [r * p for r, p in zip(revenue, nwc_pct, strict=True)]
        base_nwc = base_revenue * self._base_nwc_pct()
        nwc_investment: list[float] = []
        prior_nwc = base_nwc
        for level in nwc:
            nwc_investment.append(level - prior_nwc)
            prior_nwc = level

        # nopat already reflects the chosen treatment, so both series derive from it
        core = [n + d - c - w for n, d, c, w in zip(nopat, da, capex, nwc_investment, strict=True)]
        adjusted = core
        # SBC is a noncash expense: add back the gross charge, preserving actual taxes.
        sbc_neutral = [f + s for f, s in zip(core, sbc_series, strict=True)]
        if sbc_cfg.grow_share_count:
            sbc_neutral = [
                f - s * sbc_cfg.buyback_offset_pct
                for f, s in zip(sbc_neutral, sbc_series, strict=True)
            ]

        unlevered = sbc_neutral if sbc_cfg.grow_share_count else adjusted

        table = pd.DataFrame(
            {
                "revenue": revenue,
                "revenue_growth": growth,
                "ebit": ebit,
                "ebit_margin": margins,
                "sbc": sbc_series,
                "ebit_pre_sbc": ebit_pre_sbc,
                "taxes": taxes,
                "nol_opening": nol_opening,
                "nol_used": nol_used,
                "nol_ending": nol_ending,
                "nopat": nopat,
                "da": da,
                "capex": capex,
                "nwc": nwc,
                "nwc_investment": nwc_investment,
                "adjusted_fcf": adjusted,
                "sbc_neutral_fcf": sbc_neutral,
                "unlevered_fcf": unlevered,
            },
            index=idx,
        ).T.reindex(LINE_ORDER)

        cfo = self._hist("cfo")
        capex_hist = self._hist("capex")
        reported = (
            float(cfo - capex_hist) if pd.notna(cfo) and pd.notna(capex_hist) else float("nan")
        )

        drivers = {
            "base_revenue": base_revenue,
            "base_ebit_margin": base_margin,
            "da_pct_revenue": da_pct[0],
            "capex_pct_revenue": capex_pct[0],
            "nwc_pct_revenue": nwc_pct[0],
            "base_nwc": base_nwc,
            "tax_rate": tax_rate,
            "sbc_method": sbc_cfg.method,
            "sbc_pct_revenue": (sbc_series[0] / revenue[0]) if revenue[0] else 0.0,
        }

        return ProjectionResult(
            table=table,
            reported_fcf_latest=reported,
            drivers=drivers,
            quality=self.quality,
        )

    def _base_nwc_pct(self) -> float:
        """Operating NWC as a share of revenue, from the balance sheet where possible.

        Often negative for companies that collect from customers before paying
        suppliers -- Apple is the canonical case. A negative ratio means growth
        releases cash rather than consuming it, which is a real effect and not a
        sign error.
        """
        revenue = self._hist("revenue")
        if isinstance(self.financials, Financials):
            nwc = self.financials.net_working_capital().dropna()
            if not nwc.empty and revenue:
                return float(nwc.iloc[-1] / revenue)
        ca = self._hist("current_assets")
        cl = self._hist("current_liabilities")
        cash = self._hist("cash")
        sti = self._hist("short_term_investments")
        combined = self._hist("cash_and_sti_combined")
        cash_to_deduct = (cash if pd.notna(cash) else 0.0) + (sti if pd.notna(sti) else 0.0)
        if cash_to_deduct == 0.0 and pd.notna(combined):
            cash_to_deduct = combined
        cur_debt = self._hist("current_debt")
        debt_to_deduct = cur_debt if pd.notna(cur_debt) else 0.0

        if pd.notna(ca) and pd.notna(cl) and revenue:
            op_ca = ca - cash_to_deduct
            op_cl = cl - debt_to_deduct
            return float((op_ca - op_cl) / revenue)
        return 0.0

    def _forecast_sbc(self, years: int, growth: list[float], base_revenue: float) -> list[float]:
        cfg = self.assumptions.sbc

        if cfg.forecast_method == "explicit" and cfg.explicit:
            return [float(v) for v in cfg.explicit]

        revenue: list[float] = []
        prior = base_revenue
        for g in growth:
            prior = prior * (1.0 + g)
            revenue.append(prior)

        if cfg.forecast_method == "pct_sga":
            sga_pct = self._hist_pct_revenue("sga", 0.0)
            pcts = expand_series(cfg.sbc_pct_sga, years, 0.15)
            return [r * sga_pct * p for r, p in zip(revenue, pcts, strict=True)]

        default_pct = self._hist_pct_revenue("sbc", 0.0)
        pcts = expand_series(cfg.sbc_pct_revenue, years, default_pct)
        return [r * p for r, p in zip(revenue, pcts, strict=True)]

    # -------------------------------------------------------- convenience views

    @property
    def table(self) -> pd.DataFrame:
        return self.result.table

    @property
    def adjusted_fcf(self) -> pd.Series:
        return self.result.adjusted_fcf

    @property
    def sbc_neutral_fcf(self) -> pd.Series:
        return self.result.sbc_neutral_fcf

    @property
    def unlevered_fcf(self) -> pd.Series:
        return self.result.unlevered_fcf

    @property
    def reported_fcf(self) -> float:
        return self.result.reported_fcf_latest

    def fcf_bridge(self) -> pd.DataFrame:
        """The three FCF definitions side by side, for the Excel bridge and the README."""
        table = self.result.table
        return pd.DataFrame(
            {
                "Adjusted FCF (SBC expensed)": table.loc["adjusted_fcf"],
                "SBC-Neutral FCF (SBC added back)": table.loc["sbc_neutral_fcf"],
                "Discounted by this model": table.loc["unlevered_fcf"],
            }
        )


def terminal_year_ebitda(result: ProjectionResult) -> float:
    """EBITDA in the final projection year, the base for an exit-multiple TV."""
    ebit = float(result.table.loc["ebit"].iloc[-1])
    da = float(result.table.loc["da"].iloc[-1])
    return ebit + da


def terminal_year_growth(result: ProjectionResult) -> float:
    return float(result.table.loc["revenue_growth"].iloc[-1])
