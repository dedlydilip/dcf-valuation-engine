"""Probabilistic Scenario Weighting and Margin of Safety Framework.

Blends Bear, Base, and Bull scenario valuations by assigned probability weights
and calculates Graham/Buffett target entry prices across margin-of-safety tiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_SCENARIO_WEIGHTS: dict[str, float] = {
    "bear": 0.25,
    "base": 0.50,
    "bull": 0.25,
}

MARGIN_OF_SAFETY_TIERS: dict[str, float] = {
    "Wide-Moat Entry (15% discount)": 0.15,
    "Standard Value (25% discount)": 0.25,
    "Deep Value / Cyclical (35% discount)": 0.35,
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

    cleaned = {k: max(0.0, float(v)) for k, v in weights.items()}
    total = sum(cleaned.values())
    if total <= 0:
        return dict(DEFAULT_SCENARIO_WEIGHTS)

    return {k: v / total for k, v in cleaned.items()}


def blend_scenarios(
    scenario_values: dict[str, float],
    weights: dict[str, float] | None = None,
    current_price: float | None = None,
) -> ScenarioBlendResult:
    """Calculate probability-weighted expected fair value and margin-of-safety targets."""
    norm_weights = normalize_weights(weights)
    diagnostics: list[str] = []

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
        discount_to_expected = 1.0 - (current_price / expected_value) if expected_value > 0 else -1.0

        bull_val = clamped_values.get("bull", expected_value)
        bear_val = clamped_values.get("bear", 0.0)

        upside_dollars = max(0.0, bull_val - current_price)
        if current_price <= bear_val:
            asymmetry = float("inf")
        else:
            downside_dollars = current_price - bear_val
            asymmetry = upside_dollars / max(0.01, downside_dollars)

        if discount_to_expected >= 0.25:
            verdict = "Substantial Margin of Safety (Favorable Entry)"
            diagnostics.append(
                f"Market price (${current_price:,.2f}) trades at a {discount_to_expected:.1%} discount to expected fair value (${expected_value:,.2f})."
            )
        elif discount_to_expected >= 0.10:
            verdict = "Moderate Margin of Safety"
            diagnostics.append(
                f"Market price (${current_price:,.2f}) trades at a {discount_to_expected:.1%} discount to expected fair value."
            )
        elif discount_to_expected >= -0.15:
            verdict = "Fairly Valued"
            diagnostics.append(
                f"Market price (${current_price:,.2f}) is roughly aligned with expected fair value (${expected_value:,.2f})."
            )
        else:
            verdict = "Demanding Valuation (Negative Margin of Safety)"
            diagnostics.append(
                f"Market price (${current_price:,.2f}) is {abs(discount_to_expected):.1%} above expected fair value (${expected_value:,.2f})."
            )
    else:
        diagnostics.append("Current market price not supplied; buy targets computed against expected value.")

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
