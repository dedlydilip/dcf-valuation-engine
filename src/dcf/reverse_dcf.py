"""Reverse DCF / Expectations Investing Engine.

Inverts the discounted-cash-flow valuation. Instead of asking:
    "What is the company worth under my assumptions?"
It asks:
    "What growth, margin, and terminal rate is the market baking into today's price?"

Solves analytically for implied perpetuity growth, and uses bounded root-finding
(Brent's method) for implied 5-year revenue CAGR and implied operating margin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from scipy.optimize import root_scalar

from src.dcf.engine import DCFEngine, terminal_discount_factor
from src.models.assumptions import DCFAssumptions


@dataclass
class ReverseDCFResult:
    """Market-implied expectations extracted from current market price."""

    ticker: str
    current_price: float
    base_value_per_share: float
    wacc: float
    shares: float
    implied_revenue_growth_cagr: float | None = None
    implied_revenue_status: str = "Solved"
    implied_ebit_margin: float | None = None
    implied_margin_status: str = "Solved"
    implied_perpetuity_growth: float | None = None
    implied_growth_status: str = "Solved"
    fcf_yield_trailing: float | None = None
    fcf_yield_forward: float | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "current_price": self.current_price,
            "base_value_per_share": self.base_value_per_share,
            "wacc": self.wacc,
            "implied_revenue_growth_cagr": self.implied_revenue_growth_cagr,
            "implied_revenue_status": self.implied_revenue_status,
            "implied_ebit_margin": self.implied_ebit_margin,
            "implied_margin_status": self.implied_margin_status,
            "implied_perpetuity_growth": self.implied_perpetuity_growth,
            "implied_growth_status": self.implied_growth_status,
            "fcf_yield_trailing": self.fcf_yield_trailing,
            "fcf_yield_forward": self.fcf_yield_forward,
            "warnings": self.warnings,
        }


def solve_reverse_dcf(
    financials: Any,
    assumptions: DCFAssumptions,
    comps_terminal: dict[str, Any] | None,
    ticker: str,
    target_price: float | None = None,
) -> ReverseDCFResult:
    """Solve for the market-implied growth, margins, and terminal assumptions."""
    # Run baseline valuation first
    base_result = DCFEngine(
        financials, assumptions, comps_terminal, ticker=ticker
    ).run()

    price = target_price if target_price is not None else base_result.bridge.current_price
    if price is None or price <= 0:
        return ReverseDCFResult(
            ticker=ticker,
            current_price=0.0,
            base_value_per_share=base_result.value_per_share,
            wacc=base_result.wacc.wacc,
            shares=base_result.bridge.shares,
            warnings=["No valid positive market price found for reverse valuation."],
        )

    wacc = base_result.wacc.wacc
    shares = base_result.bridge.shares
    years = assumptions.projection.years
    mid_year = assumptions.projection.mid_year_convention
    warnings_list: list[str] = []

    # ------------------------------------------------ 1. Implied Perpetuity Growth (Closed-form)
    # Target EV required to yield target_price
    target_equity = price * shares
    target_ev = (
        target_equity
        + base_result.bridge.total_debt
        - base_result.bridge.cash
        + base_result.bridge.minority_interest
        + base_result.bridge.preferred_equity
        - base_result.bridge.investments
    )
    pv_explicit = sum(base_result.pv_explicit)
    target_pv_tv = target_ev - pv_explicit
    df_tv = terminal_discount_factor(wacc, years, mid_year=mid_year, method="gordon")
    target_tv = target_pv_tv / df_tv if df_tv > 0 else float("nan")

    fcf_final = float(base_result.projection.unlevered_fcf.iloc[-1])
    implied_g: float | None = None
    growth_status = "Solved"

    if (target_tv + fcf_final) > 0 and pd.notna(target_tv):
        g_candidate = (target_tv * wacc - fcf_final) / (target_tv + fcf_final)
        if g_candidate >= wacc:
            implied_g = g_candidate
            growth_status = "Exceeds WACC (Infinite Value)"
            warnings_list.append(
                f"Market price implies perpetuity growth ({g_candidate:.2%}) >= WACC ({wacc:.2%}), which is economically impossible."
            )
        elif g_candidate < -0.20:
            implied_g = g_candidate
            growth_status = "Severe Terminal Contraction"
        else:
            implied_g = g_candidate
            if implied_g > 0.035:
                warnings_list.append(
                    f"Implied perpetuity growth is {implied_g:.2%}, exceeding long-term global GDP ceiling (3.5%)."
                )
    else:
        growth_status = "Unattainable (Required TV is negative)"

    # Helper to evaluate valuation delta for a parameter patch
    def _eval_patch(patch_dict: dict[str, Any]) -> float:
        patched = assumptions.model_copy(deep=True)
        for path, val in patch_dict.items():
            sec, key = path.split(".", 1)
            setattr(getattr(patched, sec), key, val)
        try:
            val_res = DCFEngine(
                financials, patched, comps_terminal, ticker=ticker
            ).run()
            return val_res.value_per_share - price
        except Exception:
            return float("nan")

    # ------------------------------------------------ 2. Implied 5-Year Revenue Growth (CAGR)
    implied_rev_growth: float | None = None
    rev_status = "Solved"

    def _f_rev(g: float) -> float:
        return _eval_patch({"projection.revenue_growth": [g] * years})

    # Bracket search with automated expansion
    brackets = [(-0.30, 0.40), (-0.70, 0.90), (-0.90, 2.00)]
    bracket_found = False
    valid_bracket: tuple[float, float] | None = None

    for low, high in brackets:
        f_low = _f_rev(low)
        f_high = _f_rev(high)
        if pd.notna(f_low) and pd.notna(f_high) and f_low * f_high <= 0:
            valid_bracket = (low, high)
            bracket_found = True
            break

    if bracket_found and valid_bracket is not None:
        try:
            sol = root_scalar(_f_rev, bracket=valid_bracket, method="brentq", xtol=1e-4)
            if sol.converged:
                implied_rev_growth = float(sol.root)
            else:
                rev_status = "Solver did not converge"
        except Exception as exc:
            rev_status = f"Solver error: {exc}"
    else:
        # Determine whether price is above or below search space
        f_min = _f_rev(-0.90)
        f_max = _f_rev(2.00)
        if pd.notna(f_max) and f_max < 0:
            rev_status = "> 200% (Market expectation exceeds hyper-growth)"
        elif pd.notna(f_min) and f_min > 0:
            rev_status = "< -90% (Market expectation implies terminal decline)"
        else:
            rev_status = "Not solvable within realistic range"

    # ------------------------------------------------ 3. Implied Operating (EBIT) Margin
    implied_margin: float | None = None
    margin_status = "Solved"

    def _f_margin(m: float) -> float:
        return _eval_patch({"projection.ebit_margin": [m] * years})

    margin_brackets = [(0.02, 0.50), (0.005, 0.75), (0.001, 0.95)]
    m_bracket_found = False
    valid_m_bracket: tuple[float, float] | None = None

    for low, high in margin_brackets:
        f_low = _f_margin(low)
        f_high = _f_margin(high)
        if pd.notna(f_low) and pd.notna(f_high) and f_low * f_high <= 0:
            valid_m_bracket = (low, high)
            m_bracket_found = True
            break

    if m_bracket_found and valid_m_bracket is not None:
        try:
            sol_m = root_scalar(_f_margin, bracket=valid_m_bracket, method="brentq", xtol=1e-4)
            if sol_m.converged:
                implied_margin = float(sol_m.root)
            else:
                margin_status = "Solver did not converge"
        except Exception as exc:
            margin_status = f"Solver error: {exc}"
    else:
        f_m_max = _f_margin(0.95)
        f_m_min = _f_margin(0.001)
        if pd.notna(f_m_max) and f_m_max < 0:
            margin_status = "> 95% (Exceeds monopoly margin limits)"
        elif pd.notna(f_m_min) and f_m_min > 0:
            margin_status = "< 0.1% (Requires near-zero margin)"
        else:
            margin_status = "Not solvable within realistic range"

    # ------------------------------------------------ 4. Cash Flow Yields
    market_cap = price * shares
    fcf_reported = base_result.projection.reported_fcf_latest
    fcf_forward_yr1 = float(base_result.projection.unlevered_fcf.iloc[0])

    fcf_yield_trailing = fcf_reported / market_cap if market_cap > 0 else None
    fcf_yield_forward = fcf_forward_yr1 / market_cap if market_cap > 0 else None

    return ReverseDCFResult(
        ticker=ticker,
        current_price=price,
        base_value_per_share=base_result.value_per_share,
        wacc=wacc,
        shares=shares,
        implied_revenue_growth_cagr=implied_rev_growth,
        implied_revenue_status=rev_status,
        implied_ebit_margin=implied_margin,
        implied_margin_status=margin_status,
        implied_perpetuity_growth=implied_g,
        implied_growth_status=growth_status,
        fcf_yield_trailing=fcf_yield_trailing,
        fcf_yield_forward=fcf_yield_forward,
        warnings=warnings_list,
    )
