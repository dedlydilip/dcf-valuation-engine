"""Terminal value, by perpetuity growth and by exit multiple.

Terminal value is usually 60-80% of a DCF, so the two standard shortcuts for getting
it wrong both matter:

  Applying today's trading multiple to year-5 EBITDA. If a company trades at 22x
  while growing 8%, stamping 22x onto year-5 EBITDA asserts it is still an 8% grower
  in perpetuity -- when the same model just forecast growth fading to 4%. The
  multiple has to decay with the growth.

  Never checking what the multiple implies. Every exit multiple corresponds to some
  perpetuity growth rate. Back-solve it, and if it implies the company outgrows the
  economy forever, the multiple is wrong regardless of what comps trade at.

The decay is driven by growth alone, not margin. A better year-5 margin already
raises the EBITDA the multiple is applied to; paying for it a second time through a
higher multiple would double-count it -- the same error as charging SBC twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.models.assumptions import DCFAssumptions, TerminalAssumptions

# Minimum screened peers before a peer median may anchor the terminal multiple.
MIN_PEERS_FOR_MEDIAN = 3


@dataclass
class _Missing:
    """Stand-in so fallback checks read as `not result.ok` without a None dance."""

    ok: bool = False


_MISSING = _Missing()


@dataclass
class TerminalValueResult:
    method: str
    value: float
    multiple_used: float | None = None
    implied_perpetuity_growth: float | None = None
    implied_exit_multiple: float | None = None
    decay_detail: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return pd.notna(self.value) and self.value > 0


class TerminalValue:
    """Compute and cross-check terminal value."""

    def __init__(
        self,
        assumptions: DCFAssumptions | TerminalAssumptions | None = None,
        comps_multiples: dict[str, Any] | None = None,
    ) -> None:
        self.assumptions = _as_terminal(assumptions)
        self.comps = comps_multiples or {}

    # --------------------------------------------------------- exit multiple

    def dynamic_exit_multiple(self, year5_revenue_growth: float) -> tuple[float, dict[str, float]]:
        """Decay the peer multiple toward the mature-industry floor as growth fades.

        Decay is expressed in turns of EV/EBITDA per percentage POINT of growth given
        up against the peer median, then clamped to
        [mature_industry_multiple, peer_median_multiple]. The clamp is what stops the
        arithmetic going negative on a company whose growth collapses.
        """
        cfg = self.assumptions
        median = self.comps.get("ev_ebitda_median")
        peer_count = self.comps.get("peer_count")

        # A median needs a population. With too few screened peers the configured
        # static multiple is the more honest anchor: it is at least a stated
        # assumption rather than the midpoint of two arbitrary companies.
        use_median = (
            median is not None
            and pd.notna(median)
            and median > 0
            and (peer_count is None or peer_count >= MIN_PEERS_FOR_MEDIAN)
        )
        peer_multiple = float(median) if use_median else float(cfg.static_exit_multiple)

        peer_growth = self.comps.get("revenue_growth_median") if use_median else None
        peer_growth = float(peer_growth) if peer_growth is not None else year5_revenue_growth

        growth_drop_pp = max(0.0, (peer_growth - year5_revenue_growth) * 100.0)
        raw = peer_multiple - cfg.decay_turns_per_pp * growth_drop_pp
        floor = min(cfg.mature_industry_multiple, peer_multiple)
        clamped = min(max(raw, floor), peer_multiple)

        detail = {
            "peer_median_multiple": peer_multiple,
            "peer_median_growth": peer_growth,
            "terminal_growth": year5_revenue_growth,
            "growth_drop_pp": growth_drop_pp,
            "decay_turns_per_pp": cfg.decay_turns_per_pp,
            "raw_multiple": raw,
            "floor": floor,
            "multiple_used": clamped,
            "anchor_is_peer_median": 1.0 if use_median else 0.0,
            "peer_count": float(peer_count) if peer_count is not None else 0.0,
        }
        return clamped, detail

    def exit_multiple_value(
        self,
        terminal_ebitda: float,
        year5_revenue_growth: float,
        terminal_fcf: float | None = None,
        wacc: float | None = None,
    ) -> TerminalValueResult:
        warnings_out: list[str] = []

        if pd.isna(terminal_ebitda) or terminal_ebitda <= 0:
            return TerminalValueResult(
                method="exit_multiple",
                value=float("nan"),
                warnings=[
                    "Terminal EBITDA is not positive, so an exit multiple has no meaning. "
                    "Use the Gordon Growth terminal value instead."
                ],
            )

        if self.assumptions.exit_multiple_mode == "static":
            multiple = self.assumptions.static_exit_multiple
            detail = {"multiple_used": multiple, "mode": 0.0}
        else:
            multiple, detail = self.dynamic_exit_multiple(year5_revenue_growth)
            if detail["raw_multiple"] < detail["floor"]:
                warnings_out.append(
                    f"Decayed exit multiple {detail['raw_multiple']:.1f}x fell below the "
                    f"mature-industry floor of {detail['floor']:.1f}x and was clamped."
                )

        value = terminal_ebitda * multiple

        implied_growth = None
        if terminal_fcf is not None and wacc is not None:
            implied_growth = implied_perpetuity_growth(value, terminal_fcf, wacc)
            if implied_growth is not None and implied_growth > self.assumptions.max_implied_growth:
                warnings_out.append(
                    f"A {multiple:.1f}x exit multiple implies perpetuity growth of "
                    f"{implied_growth:.2%}, above the {self.assumptions.max_implied_growth:.2%} "
                    f"ceiling. No company outgrows the economy forever -- the multiple is "
                    f"too high, or the year-5 cash flow is too low."
                )

        return TerminalValueResult(
            method="exit_multiple",
            value=value,
            multiple_used=multiple,
            implied_perpetuity_growth=implied_growth,
            decay_detail=detail,
            warnings=warnings_out,
        )

    # -------------------------------------------------------- perpetuity growth

    def gordon_value(
        self,
        terminal_fcf: float,
        wacc: float,
        growth: float | None = None,
        terminal_ebitda: float | None = None,
    ) -> TerminalValueResult:
        g = self.assumptions.perpetuity_growth if growth is None else float(growth)
        warnings_out: list[str] = []

        if pd.isna(wacc) or wacc <= g:
            return TerminalValueResult(
                method="gordon",
                value=float("nan"),
                warnings=[
                    f"WACC of {wacc:.2%} does not exceed perpetuity growth of {g:.2%}, so the "
                    f"Gordon formula diverges. Lower the growth rate or revisit the discount "
                    f"rate."
                ],
            )

        value = terminal_fcf * (1.0 + g) / (wacc - g)

        if terminal_fcf < 0:
            warnings_out.append(
                "Year-5 free cash flow is negative, so the Gordon terminal value is negative. "
                "The forecast horizon is too short for a business that has not reached "
                "steady state."
            )

        implied_multiple = None
        if terminal_ebitda and terminal_ebitda > 0:
            implied_multiple = value / terminal_ebitda

        return TerminalValueResult(
            method="gordon",
            value=value,
            implied_exit_multiple=implied_multiple,
            decay_detail={"perpetuity_growth": g, "wacc": wacc},
            warnings=warnings_out,
        )

    # ------------------------------------------------------------------ combined

    def compute(
        self,
        terminal_fcf: float,
        terminal_ebitda: float,
        wacc: float,
        year5_revenue_growth: float,
    ) -> dict[str, TerminalValueResult]:
        """Run whichever methods are configured, each cross-checked against the other."""
        out: dict[str, TerminalValueResult] = {}
        method = self.assumptions.method

        if method in ("gordon", "both"):
            out["gordon"] = self.gordon_value(
                terminal_fcf, wacc, terminal_ebitda=terminal_ebitda
            )
        if method in ("exit_multiple", "both"):
            out["exit_multiple"] = self.exit_multiple_value(
                terminal_ebitda, year5_revenue_growth, terminal_fcf=terminal_fcf, wacc=wacc
            )

        # A configured method that turns out to be unusable still needs an answer.
        # Negative EBITDA makes the exit multiple meaningless, and the data quality
        # gate warns that the model will fall back to Gordon -- so compute it, rather
        # than leaving `select` to choose between one broken result and nothing.
        if "gordon" not in out and not out.get("exit_multiple", _MISSING).ok:
            out["gordon"] = self.gordon_value(
                terminal_fcf, wacc, terminal_ebitda=terminal_ebitda
            )
        if "exit_multiple" not in out and not out.get("gordon", _MISSING).ok:
            out["exit_multiple"] = self.exit_multiple_value(
                terminal_ebitda, year5_revenue_growth, terminal_fcf=terminal_fcf, wacc=wacc
            )

        if "gordon" in out and "exit_multiple" in out and out["gordon"].ok and out["exit_multiple"].ok:
            gap = abs(out["gordon"].value - out["exit_multiple"].value) / out["gordon"].value
            if gap > 0.35:
                note = (
                    f"The two terminal-value methods disagree by {gap:.0%}. Gordon implies "
                    f"{out['gordon'].implied_exit_multiple:.1f}x EV/EBITDA against an exit "
                    f"multiple of {out['exit_multiple'].multiple_used:.1f}x. Reconcile before "
                    f"quoting either number."
                )
                out["gordon"].warnings.append(note)
                out["exit_multiple"].warnings.append(note)

        return out

    def select(self, results: dict[str, TerminalValueResult]) -> TerminalValueResult:
        """Pick the terminal value to carry into the valuation.

        Prefers the configured method, but falls back automatically when it is
        unusable -- a negative EBITDA makes the exit multiple meaningless, which is
        exactly the case the data quality gate warns about.
        """
        preferred = self.assumptions.method
        if preferred == "both":
            # Gordon carries the point estimate: it is the more theoretically grounded
            # method and does not depend on a peer set that may not exist. But "both"
            # must not look like a blend it is not, so the choice is stated explicitly
            # rather than made silently -- see the note appended below.
            order = ["gordon", "exit_multiple"]
        else:
            order = [preferred, "gordon", "exit_multiple"]

        for key in order:
            result = results.get(key)
            if result is not None and result.ok:
                return result

        for result in results.values():
            if result is not None:
                return result
        raise ValueError("no terminal value could be computed")


def implied_perpetuity_growth(
    terminal_value: float, terminal_fcf: float, wacc: float
) -> float | None:
    """Back-solve g from TV = FCF*(1+g)/(WACC-g).

    Rearranged: g = (TV*WACC - FCF) / (TV + FCF). The number an exit multiple is
    really asserting about the company's future, made explicit.
    """
    denominator = terminal_value + terminal_fcf
    if denominator == 0 or pd.isna(denominator):
        return None
    return float((terminal_value * wacc - terminal_fcf) / denominator)


def implied_exit_multiple(terminal_value: float, terminal_ebitda: float) -> float | None:
    """The EV/EBITDA multiple a Gordon terminal value corresponds to."""
    if not terminal_ebitda or terminal_ebitda <= 0 or pd.isna(terminal_ebitda):
        return None
    return float(terminal_value / terminal_ebitda)


def _as_terminal(assumptions: Any) -> TerminalAssumptions:
    if isinstance(assumptions, DCFAssumptions):
        return assumptions.terminal
    if isinstance(assumptions, TerminalAssumptions):
        return assumptions
    return TerminalAssumptions()
