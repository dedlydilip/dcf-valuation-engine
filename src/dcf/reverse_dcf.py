"""Infer assumptions by bounded search through the complete forward engine.

Only roots whose repriced value matches the target are reported. Multiple
roots and unsupported ranges remain explicit; inferred assumptions are not
forecasts or uniquely identified market beliefs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from scipy.optimize import root_scalar

from src.dcf.engine import DCFEngine
from src.models.assumptions import DCFAssumptions
from src.models.errors import ValuationError


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
    base_result = DCFEngine(financials, assumptions, comps_terminal, ticker=ticker).run()

    price = target_price if target_price is not None else base_result.bridge.current_price
    if price is None or not math.isfinite(price) or price <= 0:
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
    warnings_list: list[str] = []

    # Terminal growth is identified only when it affects the selected valuation.
    implied_g = None
    growth_status = "Not identified for exit-multiple valuation"

    # Helper to evaluate valuation delta for a parameter patch
    def _eval_patch(patch_dict: dict[str, Any]) -> float:
        patched = assumptions.model_copy(deep=True)
        try:
            for path, val in patch_dict.items():
                sec, key = path.split(".", 1)
                setattr(getattr(patched, sec), key, val)
            val_res = DCFEngine(financials, patched, comps_terminal, ticker=ticker).run()
            return val_res.value_per_share - price
        except (ValueError, ArithmeticError, ValuationError):
            return float("nan")

    def solve_parameter(path, lower, upper, anchor, label):
        import numpy as np

        def evaluate(x):
            val = [float(x)] * years if path.startswith("projection.") else float(x)
            return _eval_patch({path: val})

        # Nonmonotonicity and terminal fallback can create multiple roots or jumps.
        nodes = sorted(
            set(np.linspace(lower, upper, 45).tolist() + [min(max(anchor, lower), upper)])
        )
        values = [evaluate(x) for x in nodes]
        tolerance = max(1e-6, abs(price) * 1e-7)
        roots = []
        for x, v in zip(nodes, values, strict=True):
            if pd.notna(v) and abs(v) < tolerance:
                roots.append(x)
        for lo, hi, fl, fh in zip(nodes[:-1], nodes[1:], values[:-1], values[1:], strict=True):
            if pd.isna(fl) or pd.isna(fh) or fl * fh >= 0:
                continue
            try:
                sol = root_scalar(evaluate, bracket=(lo, hi), method="brentq", xtol=1e-11)
                if sol.converged and abs(evaluate(sol.root)) < tolerance:
                    roots.append(float(sol.root))
            except (ValueError, ArithmeticError):
                continue
        if not roots:
            return None, "No verified solution in supported range"
        roots = sorted(roots)
        unique = [roots[0]]
        for x in roots[1:]:
            if abs(x - unique[-1]) > 1e-6:
                unique.append(x)
        if len(unique) > 1:
            warnings_list.append(
                f"{label}: multiple verified solutions; nearest base assumption reported."
            )
        return min(unique, key=lambda x: abs(x - anchor)), "Solved (forward residual verified)"

    if base_result.terminal.method != "exit_multiple":
        implied_g, growth_status = solve_parameter(
            "terminal.perpetuity_growth",
            -0.02,
            min(0.06, wacc - 1e-5),
            assumptions.terminal.perpetuity_growth,
            "Terminal growth",
        )
    if implied_g is not None and implied_g > assumptions.terminal.max_implied_growth:
        warnings_list.append("Implied terminal growth exceeds the configured long-run ceiling.")
    base_growth = float(base_result.projection.table.loc["revenue_growth"].mean())
    implied_rev_growth, rev_status = solve_parameter(
        "projection.revenue_growth", -0.9, 2.0, base_growth, "Revenue growth"
    )
    margin_anchor = float(base_result.projection.table.loc["ebit_margin"].mean())
    if assumptions.projection.margin_basis == "before_sbc":
        margin_anchor += float(
            (
                base_result.projection.table.loc["sbc"]
                / base_result.projection.table.loc["revenue"]
            ).mean()
        )
    implied_margin, margin_status = solve_parameter(
        "projection.ebit_margin", -0.5, 0.95, margin_anchor, "Operating margin"
    )

    # ------------------------------------------------ 4. Cash Flow Yields
    from src.dcf.bridge import base_share_count

    market_cap = price * base_share_count(financials)
    fcf_reported = base_result.projection.reported_fcf_latest
    fcf_forward_yr1 = float(base_result.projection.unlevered_fcf.iloc[0])

    fcf_yield_trailing = fcf_reported / market_cap if market_cap > 0 else None
    market_ev = (
        market_cap
        + base_result.bridge.net_debt
        + base_result.bridge.minority_interest
        + base_result.bridge.preferred_equity
        - base_result.bridge.investments
    )
    fcf_yield_forward = fcf_forward_yr1 / market_ev if market_ev > 0 else None
    warnings_list.append(
        "Forward yield is unlevered FCF / enterprise value; trailing yield is reported FCF / equity market cap. They have different bases."
    )

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
