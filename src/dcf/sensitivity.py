"""Two-way sensitivity tables and the football-field valuation range.

A DCF point estimate carries a precision it has not earned. These tables are the
honest version of the output: here is what the answer does when the two assumptions
it is most sensitive to move by plausible amounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.dcf.engine import DCFEngine, ValuationResult, terminal_discount_factor
from src.models.assumptions import DCFAssumptions
from src.models.errors import ValuationError


@dataclass
class SensitivityTable:
    frame: pd.DataFrame
    row_label: str
    column_label: str
    base_value: float

    def as_percentage_of_base(self) -> pd.DataFrame:
        return self.frame / self.base_value - 1.0


def _revalue(
    financials: Any,
    assumptions: DCFAssumptions,
    comps: dict[str, Any] | None,
    overrides: dict[str, Any],
    ticker: str,
) -> float:
    """Re-run the valuation with patched assumptions, returning value per share."""
    patched = assumptions.model_copy(deep=True)
    try:
        for path, value in overrides.items():
            section, key = path.split(".", 1)
            setattr(getattr(patched, section), key, value)
        return DCFEngine(financials, patched, comps, ticker=ticker).run().value_per_share
    except (ValueError, ArithmeticError, ValuationError):
        # A cell can legitimately be undefined -- WACC below the perpetuity growth
        # rate, for instance. Blank is the correct entry, not a crash.
        return float("nan")


def wacc_vs_growth(
    financials: Any,
    assumptions: DCFAssumptions,
    base_result: ValuationResult,
    comps: dict[str, Any] | None = None,
) -> SensitivityTable:
    """Classic DCF sensitivity: discount rate against perpetuity growth."""
    cfg = assumptions.sensitivity
    base_wacc = base_result.wacc.wacc
    base_growth = assumptions.terminal.perpetuity_growth
    ticker = base_result.ticker

    rows: dict[str, dict[str, float]] = {}
    for dw in cfg.wacc_deltas:
        wacc = base_wacc + dw
        row: dict[str, float] = {}
        for dg in cfg.growth_deltas:
            growth = base_growth + dg
            label = f"{growth:.2%}"
            if wacc <= growth:
                row[label] = float("nan")
                continue
            row[label] = _revalue(
                financials,
                assumptions,
                comps,
                {
                    "wacc.discount_rate_override": wacc,
                    "terminal.perpetuity_growth": growth,
                },
                ticker,
            )
        rows[f"{wacc:.2%}"] = row

    return SensitivityTable(
        frame=pd.DataFrame(rows).T,
        row_label="WACC",
        column_label="Perpetuity growth",
        base_value=base_result.value_per_share,
    )


def wacc_vs_exit_multiple(
    financials: Any,
    assumptions: DCFAssumptions,
    base_result: ValuationResult,
    comps: dict[str, Any] | None = None,
) -> SensitivityTable:
    """Discount rate against the terminal exit multiple."""
    cfg = assumptions.sensitivity
    base_wacc = base_result.wacc.wacc
    exit_result = base_result.terminal_all.get("exit_multiple")
    base_multiple = (
        exit_result.multiple_used
        if exit_result and exit_result.multiple_used
        else assumptions.terminal.static_exit_multiple
    )
    ticker = base_result.ticker

    forced = assumptions.model_copy(deep=True)
    forced.terminal.method = "exit_multiple"
    forced.terminal.exit_multiple_mode = "static"

    rows: dict[str, dict[str, float]] = {}
    for dw in cfg.wacc_deltas:
        wacc = base_wacc + dw
        row: dict[str, float] = {}
        for dm in cfg.multiple_deltas:
            multiple = max(base_multiple + dm, 0.5)
            row[f"{multiple:.1f}x"] = _revalue(
                financials,
                forced,
                comps,
                {
                    "terminal.static_exit_multiple": multiple,
                    "terminal.mature_industry_multiple": min(
                        forced.terminal.mature_industry_multiple, multiple
                    ),
                    "wacc.discount_rate_override": wacc,
                },
                ticker,
            )
        rows[f"{wacc:.2%}"] = row

    return SensitivityTable(
        frame=pd.DataFrame(rows).T,
        row_label="WACC",
        column_label="Exit multiple",
        base_value=base_result.value_per_share,
    )


def _beta_for_wacc(base_result: ValuationResult, target_wacc: float) -> float:
    """Back out the beta that produces a target WACC.

    Sensitivity tables move the discount rate, but WACC is an output of beta, the
    capital structure and the cost of debt -- not a free input. Solving for the beta
    that produces the target keeps every other relationship in the model intact,
    rather than injecting an inconsistent rate.
    """
    calc = base_result.wacc
    equity_weight = calc.equity_weight
    if equity_weight <= 0:
        return calc.beta

    debt_leg = calc.debt_weight * calc.after_tax_cost_of_debt
    required_coe = (target_wacc - debt_leg) / equity_weight
    premium = calc.equity_risk_premium
    if premium <= 0:
        return calc.beta
    levered = (
        required_coe - calc.risk_free_rate - calc.size_premium - calc.country_risk_premium
    ) / premium
    factor = calc.beta / calc.raw_beta if calc.raw_beta else 1.0
    return levered / factor


def football_field(
    base_result: ValuationResult,
    sensitivity: SensitivityTable | None = None,
    comps_implied: pd.DataFrame | None = None,
    monte_carlo: dict[str, float] | None = None,
    shares: float | None = None,
    net_debt: float | None = None,
) -> pd.DataFrame:
    """Valuation ranges by method, the standard summary exhibit.

    Each row is a low/high band rather than a point, because that is the only honest
    way to present a valuation.
    """
    rows: list[dict[str, Any]] = []
    shares = shares or base_result.bridge.shares
    bridge = base_result.bridge
    net_debt = (
        net_debt
        if net_debt is not None
        else (
            bridge.net_debt
            + bridge.minority_interest
            + bridge.preferred_equity
            - bridge.investments
        )
    )

    if sensitivity is not None:
        values = sensitivity.frame.to_numpy(dtype="float64").ravel()
        values = values[pd.notna(values)]
        if len(values):
            rows.append(
                {
                    "method": "DCF (WACC / growth range)",
                    "low": float(values.min()),
                    "high": float(values.max()),
                    "midpoint": float(base_result.value_per_share),
                }
            )

    for key, label in (
        ("gordon", "DCF (perpetuity growth TV)"),
        ("exit_multiple", "DCF (exit multiple TV)"),
    ):
        result = base_result.terminal_all.get(key)
        if result is None or not result.ok:
            continue
        # A single terminal-value method is a point, not a range. Give it a hairline
        # span so the stacked floating bar renders a visible tick: a zero-length
        # visible segment draws nothing at all, which silently dropped three of the
        # four bars -- including the market-price reference the exhibit exists for.
        point = _per_share_from_tv(base_result, result.value, shares, key)
        tick = max(abs(point) * 0.004, 0.01)
        rows.append(
            {"method": label, "low": point - tick / 2, "high": point + tick / 2, "midpoint": point}
        )

    if comps_implied is not None and not comps_implied.empty:
        from src.dcf.bridge import base_share_count

        current_shares = base_share_count(base_result.financials) + (
            base_result.assumptions.sbc.option_overhang_shares or 0
        )
        per_share = (comps_implied["implied_ev"] - net_debt) / current_shares
        per_share = per_share[pd.notna(per_share)]
        if not per_share.empty:
            rows.append(
                {
                    "method": "Comparable companies",
                    "low": float(per_share.min()),
                    "high": float(per_share.max()),
                    "midpoint": float(per_share.median()),
                }
            )

    if monte_carlo:
        rows.append(
            {
                "method": "Monte Carlo (P10-P90)",
                "low": monte_carlo.get("p10", float("nan")),
                "high": monte_carlo.get("p90", float("nan")),
                "midpoint": monte_carlo.get("p50", float("nan")),
            }
        )

    if base_result.bridge.current_price:
        price = base_result.bridge.current_price
        tick = max(abs(price) * 0.004, 0.01)
        rows.append(
            {
                "method": "Current market price",
                "low": price - tick / 2,
                "high": price + tick / 2,
                "midpoint": price,
            }
        )

    return pd.DataFrame(rows)


def _per_share_from_tv(
    result: ValuationResult, terminal_value: float, shares: float, method: str = "gordon"
) -> float:
    """Value per share if the terminal value were swapped for `terminal_value`.

    The equity bridge must match `build_bridge` line for line. It previously omitted
    preferred equity and non-operating investments, so the football-field rows silently
    disagreed with the headline number whenever either was set.
    """
    years = len(result.pv_explicit)
    mid_year = result.assumptions.projection.mid_year_convention
    pv_tv = terminal_value * terminal_discount_factor(result.wacc.wacc, years, mid_year, method)
    enterprise_value = sum(result.pv_explicit) + pv_tv
    bridge = result.bridge
    equity = (
        enterprise_value
        - bridge.total_debt
        + bridge.cash
        - bridge.minority_interest
        - bridge.preferred_equity
        + bridge.investments
    )
    if result.assumptions.sbc.grow_share_count:
        from src.dcf.bridge import base_share_count
        from src.dcf.engine import closed_form_dilution_price

        return closed_form_dilution_price(
            equity,
            base_share_count(result.financials),
            result.projection.table.loc["sbc"].tolist(),
            result.wacc.cost_of_equity,
            result.assumptions.sbc.buyback_offset_pct,
            result.assumptions.sbc.option_overhang_shares,
        )
    return float(equity / shares)
