"""Probabilistic Scenario Weighting and Margin of Safety Framework.

Blends Bear, Base, and Bull scenario valuations by assigned probability weights
and calculates Graham/Buffett target entry prices across margin-of-safety tiers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SCENARIO_WEIGHTS: dict[str, float] = {
    "bear": 0.25,
    "base": 0.50,
    "bull": 0.25,
}

MARGIN_OF_SAFETY_TIERS: dict[str, float] = {
    "Illustrative 15% discount": 0.15,
    "Illustrative 25% discount": 0.25,
    "Illustrative 35% discount": 0.35,
}


@dataclass
class ScenarioBlendResult:
    """Blended valuation outcome and margin-of-safety targets."""

    scenario_values: dict[str, float]
    normalized_weights: dict[str, float]
    expected_value: float
    target_buy_prices: dict[str, float]
    current_price: float | None = None
    discount_to_expected: float | None = None
    asymmetry_ratio: float | None = None
    verdict: str = "Neutral"
    diagnostics: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "scenario_values": self.scenario_values,
            "weights": self.normalized_weights,
            "expected_value": self.expected_value,
            "target_buy_prices": self.target_buy_prices,
            "current_price": self.current_price,
            "discount_to_expected": self.discount_to_expected,
            "asymmetry_ratio": self.asymmetry_ratio,
            "verdict": self.verdict,
            "diagnostics": self.diagnostics,
        }


def normalize_weights(weights: dict[str, float] | None) -> dict[str, float]:
    """Normalize weights to sum to 1.0, guarding against negative or empty weights."""
    if not weights:
        return dict(DEFAULT_SCENARIO_WEIGHTS)

    if any(not math.isfinite(float(v)) or float(v) < 0 for v in weights.values()):
        raise ValueError("Scenario weights must be finite and nonnegative")
    cleaned = {k: float(v) for k, v in weights.items()}
    total = sum(cleaned.values())
    if total <= 0:
        return dict(DEFAULT_SCENARIO_WEIGHTS)

    return {k: v / total for k, v in cleaned.items()}


def blend_scenarios(
    scenario_values: dict[str, float],
    weights: dict[str, float] | None = None,
    current_price: float | None = None,
) -> ScenarioBlendResult:
    """Calculate probability-weighted assumed blended value and margin-of-safety targets."""
    norm_weights = normalize_weights(weights)
    diagnostics: list[str] = []

    if any(s not in scenario_values or not math.isfinite(scenario_values[s]) for s in norm_weights):
        raise ValueError("Every weighted scenario needs a finite valuation")
    diagnostics.append(
        "Weights are analyst assumptions, not calibrated probabilities. Discounts below are illustrations, not buy recommendations."
    )
    # Enforce limited liability: equity value cannot drop below 0.0 in bankruptcy
    clamped_values = {s: max(0.0, scenario_values.get(s, 0.0)) for s in norm_weights}

    expected_value = sum(norm_weights[s] * clamped_values[s] for s in norm_weights)

    target_buys = {
        label: expected_value * (1.0 - discount)
        for label, discount in MARGIN_OF_SAFETY_TIERS.items()
    }

    discount_to_expected: float | None = None
    asymmetry: float | None = None
    verdict = "Neutral"

    if current_price is not None and current_price > 0:
        discount_to_expected = (
            1.0 - (current_price / expected_value) if expected_value > 0 else -1.0
        )

        bull_val = clamped_values.get("bull", expected_value)
        bear_val = clamped_values.get("bear", 0.0)

        upside_dollars = max(0.0, bull_val - current_price)
        if current_price <= bear_val:
            asymmetry = float("inf")
        else:
            downside_dollars = current_price - bear_val
            asymmetry = upside_dollars / max(0.01, downside_dollars)

        if discount_to_expected >= 0.25:
            verdict = "At least 25% below assumed blended value"
            diagnostics.append(
                f"Snapshot price ({current_price:,.2f}) trades at a {discount_to_expected:.1%} discount to assumed blended value ({expected_value:,.2f})."
            )
        elif discount_to_expected >= 0.10:
            verdict = "10-25% below assumed blended value"
            diagnostics.append(
                f"Snapshot price ({current_price:,.2f}) trades at a {discount_to_expected:.1%} discount to assumed blended value."
            )
        elif discount_to_expected >= -0.15:
            verdict = "Within assumed valuation range"
            diagnostics.append(
                f"Snapshot price ({current_price:,.2f}) is roughly aligned with assumed blended value ({expected_value:,.2f})."
            )
        else:
            verdict = "Above assumed blended value"
            diagnostics.append(
                f"Snapshot price ({current_price:,.2f}) is {abs(discount_to_expected):.1%} above assumed blended value ({expected_value:,.2f})."
            )
    else:
        diagnostics.append(
            "Snapshot price not supplied; illustrative discounts computed from assumed blended value."
        )

    return ScenarioBlendResult(
        scenario_values=scenario_values,
        normalized_weights=norm_weights,
        expected_value=expected_value,
        target_buy_prices=target_buys,
        current_price=current_price,
        discount_to_expected=discount_to_expected,
        asymmetry_ratio=asymmetry,
        verdict=verdict,
        diagnostics=diagnostics,
    )
