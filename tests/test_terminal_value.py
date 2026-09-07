"""Terminal value: multiple decay, clamping, and the implied-growth cross-check."""

from __future__ import annotations

import pytest

from src.dcf.terminal_value import (
    TerminalValue,
    implied_exit_multiple,
    implied_perpetuity_growth,
)
from src.models.assumptions import TerminalAssumptions

PEERS = {
    "ev_ebitda_median": 22.0,
    "revenue_growth_median": 0.08,
    "peer_count": 6.0,
}


def _tv(**overrides) -> TerminalValue:
    settings = {
        "exit_multiple_mode": "dynamic",
        "decay_turns_per_pp": 2.0,
        "mature_industry_multiple": 10.0,
        "static_exit_multiple": 12.0,
    }
    settings.update(overrides)
    return TerminalValue(TerminalAssumptions(**settings), PEERS)


class TestMultipleDecay:
    def test_multiple_decays_with_growth(self):
        """Peers grow 8% and trade at 22x. A 3% year-5 grower loses 5pp x 2 = 10 turns."""
        multiple, detail = _tv().dynamic_exit_multiple(year5_revenue_growth=0.03)
        assert detail["growth_drop_pp"] == pytest.approx(5.0)
        assert detail["raw_multiple"] == pytest.approx(12.0)
        assert multiple == pytest.approx(12.0)

    def test_no_decay_when_growth_matches_peers(self):
        multiple, detail = _tv().dynamic_exit_multiple(year5_revenue_growth=0.08)
        assert detail["growth_drop_pp"] == 0.0
        assert multiple == pytest.approx(22.0)

    def test_faster_growth_does_not_earn_a_premium(self):
        """Decay is one-directional. Outgrowing peers does not inflate the multiple.

        The extra growth is already in the EBITDA the multiple gets applied to;
        paying for it again through the multiple would double-count it.
        """
        multiple, _ = _tv().dynamic_exit_multiple(year5_revenue_growth=0.20)
        assert multiple == pytest.approx(22.0)

    def test_clamped_at_the_mature_floor(self):
        """A collapse in growth cannot drive the multiple below the industry floor."""
        multiple, detail = _tv().dynamic_exit_multiple(year5_revenue_growth=-0.05)
        assert detail["raw_multiple"] < 10.0
        assert multiple == pytest.approx(10.0)

    def test_never_goes_negative(self):
        """The failure mode of the naive multiplicative formula."""
        multiple, _ = _tv(decay_turns_per_pp=20.0).dynamic_exit_multiple(year5_revenue_growth=-0.50)
        assert multiple > 0

    def test_thin_peer_set_falls_back_to_static(self):
        """Two comparables is not a benchmark, so the stated assumption wins."""
        cfg = TerminalAssumptions(exit_multiple_mode="dynamic", static_exit_multiple=12.0)
        tv = TerminalValue(cfg, {"ev_ebitda_median": 66.0, "peer_count": 1.0})
        multiple, detail = tv.dynamic_exit_multiple(year5_revenue_growth=0.04)
        assert multiple == pytest.approx(12.0)
        assert detail["anchor_is_peer_median"] == 0.0

    def test_static_mode_ignores_peers(self):
        cfg = TerminalAssumptions(exit_multiple_mode="static", static_exit_multiple=11.0)
        result = TerminalValue(cfg, PEERS).exit_multiple_value(
            terminal_ebitda=100.0, year5_revenue_growth=0.02
        )
        assert result.multiple_used == pytest.approx(11.0)
        assert result.value == pytest.approx(1100.0)


class TestNegativeEbitda:
    def test_exit_multiple_is_undefined(self):
        result = _tv().exit_multiple_value(terminal_ebitda=-500.0, year5_revenue_growth=0.03)
        assert not result.ok
        assert "no meaning" in result.warnings[0]

    def test_selection_falls_back_to_gordon(self):
        """With EBITDA negative the model must still return a valuation."""
        cfg = TerminalAssumptions(method="exit_multiple", exit_multiple_mode="static")
        tv = TerminalValue(cfg, PEERS)
        results = tv.compute(
            terminal_fcf=50.0, terminal_ebitda=-500.0, wacc=0.10, year5_revenue_growth=0.03
        )
        chosen = tv.select(results)
        assert chosen.method == "gordon"
        assert chosen.ok


class TestImpliedGrowthCrossCheck:
    def test_warns_when_multiple_implies_impossible_growth(self):
        """The check that catches a multiple stamped on without thinking."""
        cfg = TerminalAssumptions(
            exit_multiple_mode="static", static_exit_multiple=30.0, max_implied_growth=0.035
        )
        result = TerminalValue(cfg, PEERS).exit_multiple_value(
            terminal_ebitda=100.0,
            year5_revenue_growth=0.03,
            terminal_fcf=50.0,
            wacc=0.10,
        )
        assert result.implied_perpetuity_growth > 0.035
        assert any("outgrows the economy" in w for w in result.warnings)

    def test_no_warning_for_a_reasonable_multiple(self):
        cfg = TerminalAssumptions(
            exit_multiple_mode="static", static_exit_multiple=10.0, max_implied_growth=0.035
        )
        result = TerminalValue(cfg, PEERS).exit_multiple_value(
            terminal_ebitda=100.0,
            year5_revenue_growth=0.03,
            terminal_fcf=80.0,
            wacc=0.10,
        )
        assert result.implied_perpetuity_growth < 0.035
        assert not any("outgrows" in w for w in result.warnings)

    def test_round_trip_between_growth_and_value(self):
        fcf, wacc, growth = 200.0, 0.09, 0.03
        terminal = fcf * (1 + growth) / (wacc - growth)
        assert implied_perpetuity_growth(terminal, fcf, wacc) == pytest.approx(growth)

    def test_implied_multiple_from_gordon(self):
        result = TerminalValue().gordon_value(
            terminal_fcf=100.0, wacc=0.10, growth=0.02, terminal_ebitda=200.0
        )
        assert result.implied_exit_multiple == pytest.approx(result.value / 200.0)

    def test_implied_multiple_undefined_on_negative_base(self):
        assert implied_exit_multiple(1000.0, -50.0) is None


class TestMethodDisagreement:
    def test_large_gap_between_methods_is_flagged(self):
        cfg = TerminalAssumptions(
            method="both", exit_multiple_mode="static", static_exit_multiple=40.0
        )
        results = TerminalValue(cfg, PEERS).compute(
            terminal_fcf=50.0, terminal_ebitda=100.0, wacc=0.10, year5_revenue_growth=0.03
        )
        assert any("disagree" in w for r in results.values() for w in r.warnings)
