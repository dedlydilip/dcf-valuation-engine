"""Tests for probabilistic scenario blending and margin-of-safety targets."""

from __future__ import annotations

import pytest

from src.dcf.scenario_blender import blend_scenarios, normalize_weights


def test_normalize_weights():
    # Standard normalized weights
    w = normalize_weights({"bear": 0.25, "base": 0.50, "bull": 0.25})
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["base"] == 0.50

    # Percentage integers
    w_pct = normalize_weights({"bear": 25, "base": 50, "bull": 25})
    assert sum(w_pct.values()) == pytest.approx(1.0)
    assert w_pct["base"] == 0.50

    # Empty / none falls back to default
    w_def = normalize_weights(None)
    assert sum(w_def.values()) == pytest.approx(1.0)


def test_blend_scenarios_math():
    scenarios = {"bear": 80.0, "base": 120.0, "bull": 160.0}
    weights = {"bear": 0.25, "base": 0.50, "bull": 0.25}
    price = 100.0

    blend = blend_scenarios(scenarios, weights, current_price=price)

    # 0.25*80 + 0.50*120 + 0.25*160 = 20 + 60 + 40 = 120
    assert blend.expected_value == pytest.approx(120.0)

    # 15% discount target = 120 * 0.85 = 102
    assert blend.target_buy_prices["Illustrative 15% discount"] == pytest.approx(102.0)
    # 25% discount target = 120 * 0.75 = 90
    assert blend.target_buy_prices["Illustrative 25% discount"] == pytest.approx(90.0)

    # Discount to expected: (120 - 100) / 120 = 16.67%
    assert blend.discount_to_expected == pytest.approx(20.0 / 120.0)
    assert blend.verdict == "10-25% below assumed blended value"


def test_blend_scenarios_limited_liability_clamping():
    # If bear scenario produces negative equity value, it should clamp to 0.0
    scenarios = {"bear": -40.0, "base": 50.0, "bull": 100.0}
    weights = {"bear": 0.20, "base": 0.50, "bull": 0.30}

    blend = blend_scenarios(scenarios, weights)
    # Bear clamped to 0: 0.20*0 + 0.50*50 + 0.30*100 = 0 + 25 + 30 = 55.0
    assert blend.expected_value == pytest.approx(55.0)
