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
    base_result: ValuationResult,
    assumptions: DCFAssumptions | None = None,
) -> MonteCarloResult:
    """Resample WACC, perpetuity growth and EBIT margin around the base case.

    EBIT margin is applied as a parallel shift to every forecast year, which keeps
    the fade profile intact instead of scrambling it.
    """
    assumptions = assumptions or base_result.assumptions
    cfg = assumptions.monte_carlo
    rng = np.random.default_rng(cfg.seed)
    n = cfg.iterations

    base_wacc = base_result.wacc.wacc
    base_growth = assumptions.terminal.perpetuity_growth
    years = len(base_result.pv_explicit)
    mid_year = assumptions.projection.mid_year_convention
    tax_rate = assumptions.projection.tax_rate

    table = base_result.projection.table
    revenue = table.loc["revenue"].to_numpy(dtype="float64")
    fcf = base_result.projection.unlevered_fcf.to_numpy(dtype="float64")
    terminal_fcf_base = float(base_result.projection.adjusted_fcf.iloc[-1])

    # Construct correlation matrix: [wacc, growth, margin]
    rw_g = getattr(cfg, "corr_wacc_growth", 0.35)
    rw_m = getattr(cfg, "corr_wacc_margin", -0.15)
    rg_m = getattr(cfg, "corr_growth_margin", 0.25)
    corr_mat = np.array([
        [1.0, rw_g, rw_m],
        [rw_g, 1.0, rg_m],
        [rw_m, rg_m, 1.0],
    ])
    try:
        chol = np.linalg.cholesky(corr_mat)
    except np.linalg.LinAlgError:
        chol = np.eye(3)

    def _draw_correlated(k: int):
        z = rng.standard_normal((3, k))
        correlated = chol @ z
        w_d = base_wacc + correlated[0] * cfg.wacc_std
        g_d = base_growth + correlated[1] * cfg.terminal_growth_std
        m_d = correlated[2] * cfg.ebit_margin_std
        return w_d, g_d, m_d

    wacc_draws, growth_draws, margin_draws = _draw_correlated(n)

    # A discount rate at or below the perpetuity growth rate has no finite value.
    # Resampling rather than clipping avoids piling probability mass on the boundary.
    invalid = wacc_draws <= growth_draws + 1e-4
    for _ in range(20):
        if not invalid.any():
            break
        num_invalid = int(invalid.sum())
        w_sub, g_sub, m_sub = _draw_correlated(num_invalid)
        wacc_draws[invalid] = w_sub
        growth_draws[invalid] = g_sub
        margin_draws[invalid] = m_sub
        invalid = wacc_draws <= growth_draws + 1e-4
    keep = ~invalid

    # A margin shift moves after-tax operating profit in every year.
    #
    # The tax shield is applied only where the projector would apply it. The projector
    # taxes max(EBIT, 0), so a company driven to a loss pays nothing; applying
    # (1 - tax_rate) symmetrically would hand loss draws a tax *benefit* the base model
    # refuses, fattening the left tail with cash flows that cannot occur.
    ebit = table.loc["ebit"].to_numpy(dtype="float64")
    shocked_ebit = ebit[None, :] + np.outer(margin_draws, revenue)
    tax_multiplier = np.where(shocked_ebit > 0, 1.0 - tax_rate, 1.0)
    margin_effect = np.outer(margin_draws, revenue) * tax_multiplier
    fcf_paths = fcf[None, :] + margin_effect

    terminal_shocked = ebit[-1] + margin_draws * revenue[-1]
    terminal_fcf = terminal_fcf_base + margin_draws * revenue[-1] * np.where(
        terminal_shocked > 0, 1.0 - tax_rate, 1.0
    )

    exponents = np.arange(1, years + 1) - (0.5 if mid_year else 0.0)
    discount = (1.0 + wacc_draws[:, None]) ** (-exponents[None, :])
    pv_explicit = (fcf_paths * discount).sum(axis=1)

    # Match the terminal method the base case actually used. Simulating a Gordon
    # perpetuity while the base case was valued on an exit multiple describes a
    # different model, and the reported base_case can then sit off-centre in its own
    # distribution.
    method = base_result.terminal.method
    if method == "exit_multiple":
        multiple = base_result.terminal.multiple_used or 0.0
        terminal_ebitda = float(
            table.loc["ebit"].iloc[-1] + table.loc["da"].iloc[-1]
        ) + margin_draws * revenue[-1]
        terminal_value = terminal_ebitda * multiple
    else:
        terminal_value = terminal_fcf * (1.0 + growth_draws) / (wacc_draws - growth_draws)

    tv_factor = np.array(
        [terminal_discount_factor(w, years, mid_year, method) for w in wacc_draws],
        dtype="float64",
    )
    enterprise = pv_explicit + terminal_value * tv_factor

    bridge = base_result.bridge
    equity = (
        enterprise
        - bridge.total_debt
        + bridge.cash
        - bridge.minority_interest
        - bridge.preferred_equity
        + bridge.investments
    )
    values = equity[keep] / bridge.shares

    stats = {f"p{p}": float(np.percentile(values, p)) for p in PERCENTILES}
    stats.update(
        {
            "mean": float(values.mean()),
            "std": float(values.std(ddof=1)),
            "min": float(values.min()),
            "max": float(values.max()),
            "base_case": float(base_result.value_per_share),
            "n_valid": float(len(values)),
        }
    )
    if bridge.current_price:
        stats["prob_above_market"] = float((values > bridge.current_price).mean())

    return MonteCarloResult(
        values=values,
        stats=stats,
        iterations=n,
        seed=cfg.seed,
        inputs={
            "base_wacc": base_wacc,
            "wacc_std": cfg.wacc_std,
            "base_growth": base_growth,
            "terminal_growth_std": cfg.terminal_growth_std,
            "ebit_margin_std": cfg.ebit_margin_std,
        },
    )


def summarize(result: MonteCarloResult) -> dict[str, Any]:
    return {"iterations": result.iterations, "seed": result.seed, **result.stats}
