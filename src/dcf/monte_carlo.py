"""Monte Carlo simulation over the valuation's key uncertainties.

The point is not a more accurate number -- the output is only as good as the input
distributions, which are assumptions in their own right. The point is the shape:
how wide the plausible range is, and how much of it sits above or below the market
price. A DCF that produces a single figure invites false confidence; a distribution
makes the uncertainty visible.

Deliberately fast. Rather than re-running the whole engine ten thousand times, the
projection is computed once and only the discounting and terminal value are
resimulated -- those are what the sampled parameters actually touch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.dcf.engine import ValuationResult, terminal_discount_factor
from src.models.assumptions import DCFAssumptions

PERCENTILES = (5, 10, 25, 50, 75, 90, 95)


@dataclass
class MonteCarloResult:
    values: np.ndarray
    stats: dict[str, float]
    iterations: int
    seed: int
    inputs: dict[str, float]
    draws: list[list[float]] = None
    diagnostics: dict[str, Any] = None

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame({"value_per_share": self.values})

    def histogram(self, bins: int = 50) -> pd.DataFrame:
        counts, edges = np.histogram(self.values, bins=bins)
        return pd.DataFrame(
            {
                "bin_low": edges[:-1],
                "bin_high": edges[1:],
                "count": counts,
            }
        )

    def probability_above(self, price: float) -> float:
        if not price or price <= 0:
            return float("nan")
        return float((self.values > price).mean())


def run_monte_carlo(
    base_result: ValuationResult, assumptions: DCFAssumptions | None = None
) -> MonteCarloResult:
    """Conditional uncertainty under the same accounting and terminal policy as DCF.

    WACC is a direct discount-rate shock; it does not also shock the issuance-price
    growth assumption. Invalid input-domain draws are reported, not replaced silently.
    """
    from src.dcf.accounting import cash_tax_schedule
    from src.dcf.bridge import base_share_count
    from src.dcf.engine import closed_form_dilution_price
    from src.dcf.terminal_value import TerminalValue
    from src.models.errors import ConvergenceError

    assumptions = assumptions or base_result.assumptions
    if assumptions.model_dump(exclude={"monte_carlo"}) != base_result.assumptions.model_dump(
        exclude={"monte_carlo"}
    ):
        raise ValueError("Simulation valuation assumptions must match the supplied base result")
    cfg = assumptions.monte_carlo
    n = cfg.iterations
    rng = np.random.default_rng(cfg.seed)
    corr = np.array(
        [
            [1, cfg.corr_wacc_growth, cfg.corr_wacc_margin],
            [cfg.corr_wacc_growth, 1, cfg.corr_growth_margin],
            [cfg.corr_wacc_margin, cfg.corr_growth_margin, 1],
        ]
    )
    shocks = np.linalg.cholesky(corr) @ rng.standard_normal((3, n))
    ws = base_result.wacc.wacc + shocks[0] * cfg.wacc_std
    gs = assumptions.terminal.perpetuity_growth + shocks[1] * cfg.terminal_growth_std
    ms = shocks[2] * cfg.ebit_margin_std
    table = base_result.projection.table
    revenue = table.loc["revenue"].to_numpy(float)
    ebit = table.loc["ebit"].to_numpy(float)[None, :] + ms[:, None] * revenue
    tax_rate = assumptions.projection.tax_rate
    taxes, _, _, _ = cash_tax_schedule(ebit, tax_rate, assumptions.projection.starting_nol)
    nopat = ebit - taxes
    investment = (table.loc["da"] - table.loc["capex"] - table.loc["nwc_investment"]).to_numpy(
        float
    )
    adjusted = nopat + investment
    sbc = table.loc["sbc"].to_numpy(float)
    net_sbc = sbc * (1 - assumptions.sbc.buyback_offset_pct)
    fcf = adjusted + net_sbc if assumptions.sbc.grow_share_count else adjusted
    terminal_nopat = ebit[:, -1] - np.maximum(ebit[:, -1], 0) * tax_rate
    terminal_fcf = terminal_nopat + investment[-1]
    years = len(revenue)
    mid = assumptions.projection.mid_year_convention
    periods = np.arange(1, years + 1) - (0.5 if mid else 0)
    bridge = base_result.bridge
    net_bridge = (
        bridge.total_debt
        - bridge.cash
        + bridge.minority_interest
        + bridge.preferred_equity
        - bridge.investments
    )
    values = []
    accepted = []
    rejected = {}
    methods = {}
    opening = (
        base_share_count(base_result.financials)
        if base_result.financials is not None
        else bridge.shares
    ) + (assumptions.sbc.option_overhang_shares or 0)
    for i in range(n):
        if not (
            0 < ws[i] <= 1
            and -0.02 <= gs[i] <= 0.06
            and ((ebit[i] / revenue) >= -5).all()
            and ((ebit[i] / revenue) <= 1).all()
        ):
            rejected["input_domain"] = rejected.get("input_domain", 0) + 1
            continue
        terminal_cfg = assumptions.terminal.model_copy(update={"perpetuity_growth": float(gs[i])})
        calculator = TerminalValue(terminal_cfg, base_result.comps_inputs)
        try:
            all_tv = calculator.compute(
                float(terminal_fcf[i]),
                float(ebit[i, -1] + table.loc["da"].iloc[-1]),
                float(ws[i]),
                float(table.loc["revenue_growth"].iloc[-1]),
                float(terminal_nopat[i]),
            )
            terminal = calculator.select(all_tv)
            equity = float(
                (fcf[i] / (1 + ws[i]) ** periods).sum()
                + terminal.value * terminal_discount_factor(ws[i], years, mid, terminal.method)
                - net_bridge
            )
            if assumptions.sbc.grow_share_count:
                value = closed_form_dilution_price(
                    equity,
                    opening,
                    sbc.tolist(),
                    base_result.wacc.cost_of_equity,
                    assumptions.sbc.buyback_offset_pct,
                )
            else:
                value = equity / bridge.shares
            if not np.isfinite(value):
                raise ValueError("nonfinite simulation result")
            values.append(value)
            accepted.append([float(ws[i]), float(gs[i]), float(ms[i])])
            methods[terminal.method] = methods.get(terminal.method, 0) + 1
        except (ValueError, ConvergenceError):
            rejected["valuation_invalid"] = rejected.get("valuation_invalid", 0) + 1
    if not values:
        raise ValueError("No valid simulation draws; revise distributions and model assumptions")
    values = np.asarray(values)
    stats = {f"p{p}": float(np.percentile(values, p)) for p in PERCENTILES}
    stats.update(
        mean=float(values.mean()),
        std=float(values.std(ddof=1)) if len(values) > 1 else 0.0,
        min=float(values.min()),
        max=float(values.max()),
        base_case=base_result.value_per_share,
        n_valid=float(len(values)),
        n_rejected=float(n - len(values)),
    )
    if bridge.current_price and bridge.current_price > 0:
        stats["prob_above_market"] = float((values > bridge.current_price).mean())
    return MonteCarloResult(
        values,
        stats,
        n,
        cfg.seed,
        {
            "base_wacc": base_result.wacc.wacc,
            "base_growth": assumptions.terminal.perpetuity_growth,
            "wacc_std": cfg.wacc_std,
            "terminal_growth_std": cfg.terminal_growth_std,
            "ebit_margin_std": cfg.ebit_margin_std,
        },
        draws=accepted,
        diagnostics={
            "rejected": rejected,
            "terminal_methods": methods,
            "interpretation": "Assumption-conditional distribution of valid draws, not calibrated market probabilities; invalid outcomes are counted separately.",
        },
    )


def summarize(result: MonteCarloResult) -> dict[str, Any]:
    return {"iterations": result.iterations, "seed": result.seed, **result.stats}
