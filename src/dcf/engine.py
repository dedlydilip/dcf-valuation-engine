"""Valuation orchestration and the analytic dilution fixed point.

Issuance prices are assumed to grow at cost of equity. Under that stated
policy the fixed point has a closed form; there is no iterative tolerance
error. A compensation claim that exhausts equity has no positive solution.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.dcf.bridge import BridgeResult, base_share_count, build_bridge
from src.dcf.dilution import DilutionPath, DilutionTracker, expense_method_share_count
from src.dcf.projector import ProjectionResult, Projector, terminal_year_ebitda
from src.dcf.terminal_value import TerminalValue, TerminalValueResult, implied_exit_multiple
from src.dcf.wacc import WACCCalculator
from src.models.assumptions import DCFAssumptions
from src.models.errors import ConvergenceError


@dataclass
class _Core:
    """Everything in the valuation that does not depend on the share count."""

    projection: ProjectionResult
    wacc_calc: WACCCalculator
    terminal: TerminalValueResult
    terminal_all: dict[str, TerminalValueResult]
    factors: list[float]
    pv_explicit: list[float]
    pv_terminal: float
    enterprise_value: float
    warnings: list[str]


@dataclass
class ValuationResult:
    ticker: str
    assumptions: DCFAssumptions
    projection: ProjectionResult
    wacc: WACCCalculator
    terminal: TerminalValueResult
    terminal_all: dict[str, TerminalValueResult]
    bridge: BridgeResult
    discount_factors: list[float]
    pv_explicit: list[float]
    pv_terminal: float
    dilution: DilutionPath | None = None
    iterations: int = 1
    warnings: list[str] = field(default_factory=list)
    financials: Any = None
    comps_inputs: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def enterprise_value(self) -> float:
        return sum(self.pv_explicit) + self.pv_terminal

    @property
    def value_per_share(self) -> float:
        return self.bridge.value_per_share

    @property
    def terminal_value_share(self) -> float:
        """Terminal value as a share of enterprise value. Above ~80% is a warning sign."""
        ev = self.enterprise_value
        return self.pv_terminal / ev if ev else float("nan")

    def terminal_value_of(self, method: str) -> float | None:
        result = self.terminal_all.get(method)
        return result.value if result is not None else None

    def summary(self) -> dict[str, Any]:
        exit_result = self.terminal_all.get("exit_multiple")
        gordon_result = self.terminal_all.get("gordon")
        return {
            "ticker": self.ticker,
            "scenario": self.assumptions.scenario,
            "sbc_method": self.assumptions.sbc.method,
            "wacc": self.wacc.wacc,
            "cost_of_equity": self.wacc.cost_of_equity,
            "cost_of_debt": self.wacc.cost_of_debt,
            "beta": self.wacc.beta,
            "terminal_method_used": self.terminal.method,
            "terminal_value": self.terminal.value,
            "gordon_terminal_value": gordon_result.value if gordon_result else None,
            "exit_multiple_terminal_value": exit_result.value if exit_result else None,
            "exit_multiple_used": exit_result.multiple_used if exit_result else None,
            "implied_perpetuity_growth": (
                exit_result.implied_perpetuity_growth if exit_result else None
            ),
            "implied_exit_multiple": (
                gordon_result.implied_exit_multiple if gordon_result else None
            ),
            "enterprise_value": self.enterprise_value,
            "equity_value": self.bridge.equity_value,
            "shares": self.bridge.shares,
            "value_per_share": self.value_per_share,
            "current_price": self.bridge.current_price,
            "upside": self.bridge.upside,
            "terminal_value_pct_ev": self.terminal_value_share,
            "iterations": self.iterations,
            "manifest": self.manifest,
            "warnings": self.warnings,
        }


def discount_factors(wacc: float, years: int, mid_year: bool = True) -> list[float]:
    """Present-value factors, offset half a year when mid-year convention is on.

    Cash arrives through the year rather than in a lump on 31 December, so the
    mid-year convention discounts year t at t-0.5. It lifts the valuation a few
    percent and is standard in banking models.
    """
    offset = 0.5 if mid_year else 0.0
    return [1.0 / ((1.0 + wacc) ** (t - offset)) for t in range(1, years + 1)]


def terminal_discount_factor(
    wacc: float, years: int, mid_year: bool = True, method: str = "gordon"
) -> float:
    """Discount factor for the terminal value. The right answer depends on the method.

    Gordon values a perpetuity of FLOWS. If the explicit period uses mid-year timing
    then the terminal flows arrive mid-year too, which lifts the perpetuity by
    (1+w)^0.5 -- equivalently, discount the standard formula at N-0.5.

    An exit multiple is not a flow stream. It is a SALE PRICE: one receipt at the end
    of year N. A point-in-time amount discounts at N regardless of how the explicit
    cash flows were timed. Applying the mid-year factor to it overstated the terminal
    value by (1+w)^0.5, about 4.9% at a 10% WACC.
    """
    if method == "exit_multiple":
        return 1.0 / ((1.0 + wacc) ** years)
    offset = 0.5 if mid_year else 0.0
    return 1.0 / ((1.0 + wacc) ** (years - offset))


def closed_form_dilution_price(
    equity_value: float,
    base_shares: float,
    sbc_dollars: list[float],
    cost_of_equity: float,
    buyback_offset_pct: float = 0.0,
    option_overhang_shares: float | None = None,
) -> float:
    """Analytic solution to the dilution fixed point.

    With shares issued each year at the prevailing price, and that price growing at
    the cost of equity, the fixed point resolves to

        S = S0 / (1 - K/E)     and     P = (E - K) / S0

    where K is the present value of future SBC discounted at the cost of equity.

    So the dilute treatment is exactly "subtract the present value of the stock you
    are going to hand employees from equity value" -- which is the clearest possible
    statement of why doing that AND expensing SBC would be charging the same cost
    twice. It also shows the solver diverges precisely when K exceeds E.

    `buyback_offset_pct` scales the issuance exactly as `DilutionTracker.forecast`
    does. Omitting it made this function silently disagree with the solver by up to
    3.2%, and the test that compares the two only passed because the default is zero.

    `option_overhang_shares` belongs in S0 for the same reason it does under the
    expense treatment: grants already made will vest whatever the model assumes about
    future ones. The two methods differ in how they charge *future* grants, not past
    ones, and omitting the overhang here made dilute understate the share count by
    exactly the overhang -- breaking the convergence property the two methods are
    supposed to have.
    """
    net_issuance = 1.0 - buyback_offset_pct
    k = sum(
        (sbc * net_issuance) / ((1.0 + cost_of_equity) ** t)
        for t, sbc in enumerate(sbc_dollars, start=1)
    )
    if k >= equity_value:
        raise ConvergenceError(
            f"the present value of future stock compensation ({k:,.0f}) is at least the "
            f"equity value ({equity_value:,.0f}), so there is no positive price at which "
            f"the company can pay its staff in stock. Value it with sbc.method = "
            f"'expense' instead."
        )
    return (equity_value - k) / (base_shares + float(option_overhang_shares or 0.0))


class DCFEngine:
    """Run a full valuation for one company."""

    def __init__(
        self,
        financials: Any,
        assumptions: DCFAssumptions | None = None,
        comps_multiples: dict[str, Any] | None = None,
        ticker: str | None = None,
    ) -> None:
        self.financials = financials
        self.assumptions = DCFAssumptions.model_validate(
            (assumptions or DCFAssumptions()).model_dump()
        )
        self.comps_multiples = comps_multiples or {}
        self.ticker = ticker or getattr(financials, "ticker", "N/A")
        self._core: _Core | None = None

    def run(self) -> ValuationResult:
        self._core = None
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            core = self._compute_core()
            result = (
                self._solve_dilution(core)
                if self.assumptions.sbc.grow_share_count
                else self._assemble(core, self._starting_shares(), dilution=None, iterations=1)
            )
        if not all(
            math.isfinite(v)
            for v in (result.value_per_share, result.enterprise_value, result.wacc.wacc)
        ):
            raise ValueError("Valuation is non-finite; no result can be published")
        result.warnings = _dedupe(result.warnings + [str(w.message) for w in captured])
        from src.models.provenance import run_manifest

        result.manifest = run_manifest(self.financials, self.assumptions)
        aliases = (
            getattr(self.financials, "provenance", {}).get("source_aliases", {}).get("ebit", {})
        )
        if aliases:
            latest_alias = next(reversed(aliases.values()))
            if latest_alias.lower() != "operating income":
                result.warnings.append(
                    f"EBIT uses provider alias '{latest_alias}'; reconcile its operating/non-operating definition with the filing before relying on the margin."
                )
        if self.assumptions.projection.margin_basis == "after_sbc":
            result.warnings.append(
                "Forecast margin is after SBC. Changing SBC alone holds total reported operating profit fixed; use before_sbc margin basis to model incremental compensation cost."
            )
        result.warnings.append(
            "Cash taxes assume SBC is deductible and tax losses carry forward without expiry or annual utilization caps. Local tax rules and grant settlement differences require separate modeling."
        )
        if self.assumptions.wacc.cost_of_debt_override is None:
            result.warnings.append(
                "Cost of debt is a historical book-yield proxy (or risk-free fallback), not a current marginal borrowing yield. Supply a currency-consistent market borrowing-rate override for decision use."
            )
        if not result.manifest.get("market_timestamp"):
            result.warnings.append(
                "Market quote timestamp is unknown; this is an undated snapshot valuation, not a current or historical as-of valuation."
            )
        result.warnings.append(
            "Scenario values depend on uncalibrated assumptions; they are not verified fair values or investment recommendations."
        )
        if self.assumptions.valuation_date is not None:
            raise ValueError(
                "As-of valuation with dated stub cash flows is not implemented; omit valuation_date for an explicitly undated snapshot model"
            )
        return result

    def _starting_shares(self) -> float:
        """Shares outstanding today, plus any already-granted overhang.

        Both SBC treatments start here. They differ only in how they charge *future*
        grants -- expensing them through cash flow, or issuing shares against them.
        Options already granted vest either way, so the overhang belongs in the
        opening count under both.

        This used to be called only on the expense path, while `_solve_dilution` seeded
        its tracker from the bare share count. With a non-zero `option_overhang_shares`
        the two methods then disagreed by exactly the overhang, which quietly broke the
        convergence property the README rests on.
        """
        return expense_method_share_count(base_share_count(self.financials), self.assumptions.sbc)

    # ---------------------------------------------------------------- core pass

    def _compute_core(self) -> _Core:
        """Everything up to enterprise value. Independent of the share count.

        Worth stating explicitly: the projection, WACC and terminal value do not
        depend on how many shares exist. Only the bridge does. That is why the
        dilution loop below re-runs a division rather than the whole model.
        """
        if self._core is not None:
            return self._core

        assumptions = self.assumptions
        warnings_out: list[str] = []

        projector = Projector(self.financials, assumptions)
        projection = projector.result
        wacc_calc = WACCCalculator(self.financials, assumptions)
        wacc = wacc_calc.wacc
        if not math.isfinite(wacc) or wacc <= 0:
            raise ValueError("A positive finite WACC is required")

        fcf = [float(v) for v in projection.unlevered_fcf]
        years = len(fcf)
        mid_year = assumptions.projection.mid_year_convention

        factors = discount_factors(wacc, years, mid_year)
        pv_explicit = [f * d for f, d in zip(fcf, factors, strict=True)]

        term_ebitda = terminal_year_ebitda(projection)
        term_growth = float(projection.table.loc["revenue_growth"].iloc[-1])

        # The terminal value is always built on the SBC-expensed cash flow, whichever
        # treatment the explicit period uses.
        #
        # Dilution can be modelled explicitly for five years; it cannot be modelled
        # explicitly forever. Capitalising an added-back SBC into perpetuity while
        # charging only five years of share issuance against it would inflate the
        # terminal value by roughly 1/(WACC-g) times the annual add-back -- for a
        # mega-cap that is over a hundred billion dollars of value conjured from an
        # accounting choice. Beyond the horizon, perpetual dilution and perpetual
        # expensing describe the same steady state, so the model expenses it.
        terminal_fcf = float(projection.adjusted_fcf.iloc[-1])
        terminal_ebit = float(projection.table.loc["ebit"].iloc[-1])
        terminal_nopat = terminal_ebit - max(terminal_ebit, 0.0) * assumptions.projection.tax_rate
        # NOLs are finite, so do not capitalize final-year tax relief forever.
        terminal_fcf += terminal_nopat - float(projection.table.loc["nopat"].iloc[-1])

        tv = TerminalValue(assumptions, self.comps_multiples)
        terminal_all = tv.compute(
            terminal_fcf=terminal_fcf,
            terminal_ebitda=term_ebitda,
            wacc=wacc,
            year5_revenue_growth=term_growth,
            terminal_nopat=terminal_nopat,
        )
        terminal = tv.select(terminal_all)
        for result in terminal_all.values():
            warnings_out.extend(result.warnings)

        # With method="both" the shipped default, say out loud which method carried the
        # headline and what the other one implies. Computing an exit multiple, printing
        # it, cross-checking it and then discarding it without comment reads as a blend.
        if assumptions.terminal.method == "both":
            other = terminal_all.get("exit_multiple" if terminal.method == "gordon" else "gordon")
            if other is not None and other.ok and terminal.value:
                spread = other.value / terminal.value - 1.0
                warnings_out.append(
                    f"Terminal method 'both': the {terminal.method.replace('_', ' ')} method "
                    f"carries the reported value. The alternative terminal value is "
                    f"{spread:+.1%} against it. Treat the two as a range, not a single answer."
                )

        if terminal.implied_exit_multiple is None and term_ebitda > 0:
            terminal.implied_exit_multiple = implied_exit_multiple(terminal.value, term_ebitda)

        pv_terminal = terminal.value * terminal_discount_factor(
            wacc, years, mid_year, terminal.method
        )
        enterprise_value = sum(pv_explicit) + pv_terminal

        # A terminal year still reinvesting far above its depreciation is not in steady
        # state, yet Gordon capitalises that gap into perpetuity. Microsoft's AI build-out
        # leaves capex at ~2.5x D&A in year 5; extrapolating that forever permanently
        # suppresses terminal cash flow and is the single largest driver of its valuation,
        # more than the discount rate. The model will not silently overrule the forecast,
        # but it says so.
        terminal_capex = float(projection.table.loc["capex"].iloc[-1])
        terminal_da = float(projection.table.loc["da"].iloc[-1])
        if terminal_da > 0:
            reinvestment = terminal_capex / terminal_da
            if reinvestment > 1.3 or reinvestment < 0.7:
                warnings_out.append(
                    f"Terminal-year capex is {reinvestment:.2f}x depreciation. Review whether "
                    f"this reinvestment supports the assumed perpetual growth and return on "
                    f"new capital. Capex equal to D&A is not a universal steady-state rule."
                )

        if pd.notna(enterprise_value) and enterprise_value > 0:
            share = pv_terminal / enterprise_value
            if share > 0.85:
                warnings_out.append(
                    f"The terminal value is {share:.0%} of enterprise value. Almost all of "
                    f"this valuation rests on assumptions beyond the forecast horizon."
                )

        warnings_out.extend(projection.quality.warnings)

        self._core = _Core(
            projection=projection,
            wacc_calc=wacc_calc,
            terminal=terminal,
            terminal_all=terminal_all,
            factors=factors,
            pv_explicit=pv_explicit,
            pv_terminal=pv_terminal,
            enterprise_value=enterprise_value,
            warnings=_dedupe(warnings_out),
        )
        return self._core

    def _assemble(
        self, core: _Core, shares: float, dilution: DilutionPath | None, iterations: int
    ) -> ValuationResult:
        bridge = build_bridge(core.enterprise_value, self.financials, self.assumptions, shares)
        return ValuationResult(
            ticker=self.ticker,
            assumptions=self.assumptions,
            projection=core.projection,
            wacc=core.wacc_calc,
            terminal=core.terminal,
            terminal_all=core.terminal_all,
            bridge=bridge,
            discount_factors=core.factors,
            pv_explicit=core.pv_explicit,
            pv_terminal=core.pv_terminal,
            dilution=dilution,
            iterations=iterations,
            warnings=list(core.warnings),
            financials=self.financials,
            comps_inputs=dict(self.comps_multiples),
        )

    # --------------------------------------------------------- dilution solver

    def _solve_dilution(self, core: _Core) -> ValuationResult:
        """Iterate share count and share price to a fixed point."""
        # Includes the already-granted overhang, exactly as the expense path does.
        # Seeding from the bare count here was the asymmetry between the two methods.
        base_shares = self._starting_shares()
        tracker = DilutionTracker(base_shares, self.assumptions.sbc)
        price_growth = core.wacc_calc.cost_of_equity

        # Seed from the undiluted valuation rather than the market price, so the
        # answer never depends on what the stock happens to trade at today.
        seed = self._assemble(core, base_shares, dilution=None, iterations=0)
        price = seed.value_per_share

        if pd.isna(price) or price <= 0:
            raise ConvergenceError(
                f"{self.ticker}: the undiluted valuation is not positive ({price:,.2f}), "
                f"so there is no share price to issue stock at. Value this company with "
                f"sbc.method = 'expense' instead."
            )

        sbc_dollars = [float(v) for v in core.projection.table.loc["sbc"]]
        final_price = closed_form_dilution_price(
            seed.bridge.equity_value,
            base_shares,
            sbc_dollars,
            price_growth,
            self.assumptions.sbc.buyback_offset_pct,
        )
        path = tracker.forecast(sbc_dollars, base_price=final_price, price_growth=price_growth)
        result = self._assemble(core, path.ending_shares, path, 1)
        result.warnings.append(
            "Dilution uses a stylized issuance-price path growing at cost of equity; grant expense is used as issuance dollars. This is not a vesting/option-pricing forecast."
        )
        return result


def value_company(
    financials: Any,
    assumptions: DCFAssumptions | None = None,
    comps_multiples: dict[str, Any] | None = None,
) -> ValuationResult:
    return DCFEngine(financials, assumptions, comps_multiples).run()


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
