"""Economic Moat and Capital Efficiency Analyzer.

Calculates Return on Invested Capital (ROIC) and the Economic Spread (ROIC - WACC).
A DCF will project cash flows regardless of capital efficiency, but economic theory
(Buffett, Damodaran, Mauboussin) establishes that growth only creates shareholder value
when ROIC exceeds WACC. If ROIC < WACC, reinvestment and growth destroy equity value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.dcf.engine import ValuationResult
from src.models.financials import field_value, total_cash_position


@dataclass
class MoatAnalysis:
    """Capital efficiency and economic moat metrics."""

    invested_capital_base: float
    nopat_base: float
    roic_base: float
    wacc: float
    economic_spread: float
    projected_roics: list[float] = field(default_factory=list)
    projected_spreads: list[float] = field(default_factory=list)
    reinvestment_rates: list[float] = field(default_factory=list)
    is_value_accretive: bool = True
    moat_rating: str = "Neutral"
    diagnostics: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "invested_capital_base": self.invested_capital_base,
            "nopat_base": self.nopat_base,
            "roic_base": self.roic_base,
            "wacc": self.wacc,
            "economic_spread": self.economic_spread,
            "is_value_accretive": self.is_value_accretive,
            "moat_rating": self.moat_rating,
            "diagnostics": self.diagnostics,
        }


def _resolve_invested_capital(financials: Any) -> float:
    """Extract or construct a robust opening invested capital balance.

    Handles negative equity / heavy share-buyback companies (like Apple) gracefully
    by checking reported Invested Capital first, then financing balance, then operating
    assets fallback (Net PPE + Operating Working Capital).
    """
    reported_ic = field_value(financials, "invested_capital")
    if pd.notna(reported_ic) and reported_ic > 0:
        return float(reported_ic)

    total_debt = field_value(financials, "total_debt", 0.0)
    equity = field_value(financials, "stockholders_equity", 0.0)
    cash = total_cash_position(financials)
    long_term_inv = field_value(financials, "long_term_investments", 0.0)
    lt_inv_val = long_term_inv if pd.notna(long_term_inv) else 0.0

    financing_ic = (
        (total_debt if pd.notna(total_debt) else 0.0)
        + (equity if pd.notna(equity) else 0.0)
        - (cash if pd.notna(cash) else 0.0)
        - lt_inv_val
    )

    if pd.notna(financing_ic) and financing_ic > 0:
        return float(financing_ic)

    # Operating assets fallback: Net PPE + Operating Working Capital
    net_ppe = field_value(financials, "net_ppe", 0.0)
    nwc_series = (
        financials.net_working_capital().dropna()
        if hasattr(financials, "net_working_capital")
        else pd.Series(dtype=float)
    )
    nwc = float(nwc_series.iloc[-1]) if len(nwc_series) else float("nan")
    op_ic = (net_ppe if pd.notna(net_ppe) else 0.0) + (nwc if pd.notna(nwc) else 0.0)

    if pd.notna(op_ic) and op_ic > 0:
        return float(op_ic)

    # Absolute fallback: positive fraction of revenue to avoid division by zero
    return float("nan")


def analyze_moat(financials: Any, result: ValuationResult) -> MoatAnalysis:
    """Compute ROIC, economic spread vs WACC, and value creation metrics."""
    wacc = result.wacc.wacc
    tax_rate = result.assumptions.projection.tax_rate
    diagnostics: list[str] = []

    # Historical / Base year NOPAT
    base_ebit = field_value(financials, "ebit")
    if pd.isna(base_ebit):
        base_ebit = float("nan")
    base_nopat = float(base_ebit - max(base_ebit, 0) * tax_rate)

    ic_base = _resolve_invested_capital(financials)
    if pd.isna(ic_base) or ic_base <= 0:
        return MoatAnalysis(
            ic_base,
            base_nopat,
            float("nan"),
            wacc,
            float("nan"),
            is_value_accretive=False,
            moat_rating="Unavailable: capital base unsupported",
            diagnostics=[
                "ROIC unavailable: no defensible invested-capital denominator. No moat conclusion can be made."
            ],
        )
    diagnostics.append(
        "Accounting ROIC is a capital-efficiency diagnostic, not evidence of a durable competitive moat. Review capital definition, R&D and acquisitions."
    )
    roic_base = base_nopat / ic_base
    spread_base = roic_base - wacc

    # Projected ROIC series tracking reinvestment: IC_t = IC_{t-1} + (CapEx - D&A + NWC_inv)
    table = result.projection.table
    years = result.projection.years
    projected_roics: list[float] = []
    projected_spreads: list[float] = []
    reinvestment_rates: list[float] = []

    current_ic = ic_base
    for yr in years:
        nopat_t = float(table.loc["nopat"].loc[yr])
        capex_t = float(table.loc["capex"].loc[yr])
        da_t = float(table.loc["da"].loc[yr])
        nwc_inv_t = float(table.loc["nwc_investment"].loc[yr])

        reinvestment = (capex_t - da_t) + nwc_inv_t
        reinvest_rate = reinvestment / nopat_t if nopat_t > 0 else 0.0
        reinvestment_rates.append(reinvest_rate)

        # ROIC is return on opening capital of year t
        roic_t = nopat_t / current_ic if current_ic > 0 else float("nan")
        projected_roics.append(roic_t)
        projected_spreads.append(roic_t - wacc)

        # Reinvestment builds invested capital for next year
        current_ic = current_ic + reinvestment
        if current_ic <= 0:
            diagnostics.append(
                "Projected capital becomes nonpositive; subsequent ROIC is unavailable."
            )

    # Moat strength assessment
    avg_spread = (
        sum(projected_spreads) / len(projected_spreads) if projected_spreads else spread_base
    )
    avg_spread = spread_base
    is_accretive = avg_spread > 0

    if avg_spread >= 0.10:
        rating = "High accounting return spread (moat unverified)"
        diagnostics.append(
            f"Historical accounting ROIC ({roic_base:.1%}) substantially exceeds WACC ({wacc:.1%})."
        )
    elif avg_spread >= 0.02:
        rating = "Positive accounting return spread (moat unverified)"
        diagnostics.append(
            f"Value-accretive: ROIC ({roic_base:.1%}) generates a positive spread of {spread_base:+.1%} over WACC."
        )
    elif avg_spread >= -0.02:
        rating = "Neutral / Cost of Capital Returns"
        diagnostics.append(
            f"Neutral moat: ROIC ({roic_base:.1%}) roughly equals the cost of capital ({wacc:.1%})."
        )
    else:
        rating = "Value-Destructive (Negative Spread)"
        diagnostics.append(
            f"WARNING: Negative economic spread ({spread_base:+.1%}). ROIC ({roic_base:.1%}) < WACC ({wacc:.1%}). "
            "Reinvestment and growth at this rate destroy shareholder value."
        )

    return MoatAnalysis(
        invested_capital_base=ic_base,
        nopat_base=base_nopat,
        roic_base=roic_base,
        wacc=wacc,
        economic_spread=spread_base,
        projected_roics=projected_roics,
        projected_spreads=projected_spreads,
        reinvestment_rates=reinvestment_rates,
        is_value_accretive=is_accretive,
        moat_rating=rating,
        diagnostics=diagnostics,
    )
