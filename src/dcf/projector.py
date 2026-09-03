"""Revenue-to-free-cash-flow projection, with three FCF definitions.

The reason there are three:

  reported_fcf     CFO - CapEx, exactly as data providers publish it. Adds SBC back
                   as a non-cash item and stops there, so it flatters any company
                   that pays its staff in stock. Carried purely for contrast.

  adjusted_fcf     EBIT(1-t) + D&A - CapEx - increase in NWC, with SBC left inside
                   EBIT where GAAP already put it. This is owner earnings, and it is
                   what the model discounts under sbc.method = "expense".

  sbc_neutral_fcf  adjusted_fcf with SBC added back after tax. Used under
                   sbc.method = "dilute", where the cost is charged through a growing
                   share count instead, and for reconciling to sell-side numbers.

The critical detail is that reported EBIT is already net of stock compensation.
Subtracting SBC from EBIT(1-t) *and* growing the share count charges shareholders
twice for one cost. This module never does both; SBCAssumptions refuses to let it.
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
        return float(self.table.loc["nopat"].iloc[-1])


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
        margins = expand_series(proj.ebit_margin, years, base_margin)

        da_pct = expand_series(proj.da_pct_revenue, years, self._hist_pct_revenue("da", 0.03))
        capex_pct = expand_series(
            proj.capex_pct_revenue, years, self._hist_pct_revenue("capex", 0.03)
        )
        nwc_pct = expand_series(proj.nwc_pct_revenue, years, self._base_nwc_pct())

        sbc_series = self._forecast_sbc(years, growth, base_revenue)

        # revenue path
        revenue: list[float] = []
        prior = base_revenue
        for g in growth:
            prior = prior * (1.0 + g)
            revenue.append(prior)

        ebit = [r * m for r, m in zip(revenue, margins, strict=True)]
        ebit_pre_sbc = [e + s for e, s in zip(ebit, sbc_series, strict=True)]

        tax_rate = proj.tax_rate
        # Which EBIT the tax is computed on follows the SBC treatment: under
        # "dilute" the SBC add-back is part of the cash flow being valued, so it
        # is taxed too.
        taxable = ebit_pre_sbc if sbc_cfg.grow_share_count else ebit
        taxes = [max(e, 0.0) * tax_rate for e in taxable]
        nopat = [e - t for e, t in zip(taxable, taxes, strict=True)]

        da = [r * p for r, p in zip(revenue, da_pct, strict=True)]
        capex = [r * p for r, p in zip(revenue, capex_pct, strict=True)]

        nwc = [r * p for r, p in zip(revenue, nwc_pct, strict=True)]
        base_nwc = base_revenue * nwc_pct[0]
        nwc_investment: list[float] = []
        prior_nwc = base_nwc
        for level in nwc:
            nwc_investment.append(level - prior_nwc)
            prior_nwc = level

        # nopat already reflects the chosen treatment, so both series derive from it
        core = [
            n + d - c - w
            for n, d, c, w in zip(nopat, da, capex, nwc_investment, strict=True)
        ]
        if sbc_cfg.grow_share_count:
            sbc_neutral = core
            adjusted = [f - s * (1.0 - tax_rate) for f, s in zip(core, sbc_series, strict=True)]
        else:
            adjusted = core
            sbc_neutral = [f + s * (1.0 - tax_rate) for f, s in zip(core, sbc_series, strict=True)]

        # Buybacks used to offset dilution are real cash leaving the business.
        #
        # The charge must be after tax, to match the after-tax add-back it reverses.
        # Subtracting the pre-tax amount against an after-tax add-back left the
        # dilute-with-full-buyback case exactly SBC x tax_rate below the economically
        # equivalent expense method -- the tax shield granted once and removed twice.
        if sbc_cfg.grow_share_count and sbc_cfg.buyback_offset_pct > 0:
            offset = [
                s * sbc_cfg.buyback_offset_pct * (1.0 - tax_rate) for s in sbc_series
            ]
            sbc_neutral = [f - o for f, o in zip(sbc_neutral, offset, strict=True)]

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
        if pd.notna(ca) and pd.notna(cl) and revenue:
            return float((ca - cl) / revenue)
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

