"""Validated valuation assumptions.

Everything the model treats as an input lives here, so there is exactly one place
to look when defending a number in an interview. Pydantic enforces the invariants
that would otherwise become silent valuation errors -- above all the rule that
stock-based compensation is charged EITHER as an expense OR as dilution, never both.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Literal

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.paths import PACKAGE_ROOT, resolve_path

Series = float | list[float]

# Kept as an alias so the intent reads clearly at the call sites below. Config paths
# and offline fixture paths have the same problem and now share one resolver; see
# src/paths.py for why. `from_yaml` raises rather than skipping when a file is still
# missing after resolution.
resolve_config_path = resolve_path


def expand_series(value: Series | None, years: int, default: float | None = None) -> list[float]:
    """Expand a scalar to a flat series, or pass a list through after a length check."""
    if value is None:
        if default is None:
            raise ValueError("no value and no default supplied")
        return [float(default)] * years
    if isinstance(value, list):
        if len(value) != years:
            raise ValueError(f"expected {years} values, got {len(value)}")
        return [float(v) for v in value]
    return [float(value)] * years


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` into a copy of `base`. Scalars and lists replace."""
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)

    def __setattr__(self, name, value):
        # Pydantic's after validators can fail after mutating a field. Restore the
        # complete state so a rejected assignment never leaves invalid assumptions.
        previous = self.__dict__.copy()
        fields_set = self.__pydantic_fields_set__.copy()
        try:
            super().__setattr__(name, value)
        except (ValueError, TypeError):
            object.__setattr__(self, "__dict__", previous)
            object.__setattr__(self, "__pydantic_fields_set__", fields_set)
            raise


class ProjectionAssumptions(_Base):
    years: int = Field(5, ge=1, le=20)
    revenue_growth: Series = 0.05
    ebit_margin: Series | None = None
    tax_rate: float = Field(0.21, ge=0.0, lt=1.0)
    da_pct_revenue: Series | None = None
    capex_pct_revenue: Series | None = None
    nwc_pct_revenue: Series | None = None
    mid_year_convention: bool = True
    starting_nol: float = Field(0.0, ge=0.0)
    margin_basis: Literal["after_sbc", "before_sbc"] = "after_sbc"
    fade_capex_to_da: bool = False
    terminal_capex_to_da: float = Field(1.0, ge=0.5, le=5.0)

    @model_validator(mode="after")
    def _check_series_lengths(self) -> ProjectionAssumptions:
        for name in (
            "revenue_growth",
            "ebit_margin",
            "da_pct_revenue",
            "capex_pct_revenue",
            "nwc_pct_revenue",
        ):
            val = getattr(self, name)
            if isinstance(val, list) and len(val) != self.years:
                raise ValueError(
                    f"projection.{name} has {len(val)} values but projection.years is {self.years}"
                )
        bounds = {
            "revenue_growth": (-1.0, 10.0),
            "ebit_margin": (-5.0, 1.0),
            "da_pct_revenue": (0.0, 5.0),
            "capex_pct_revenue": (0.0, 5.0),
            "nwc_pct_revenue": (-5.0, 5.0),
        }
        for name, (lo, hi) in bounds.items():
            val = getattr(self, name)
            for x in val if isinstance(val, list) else [val]:
                if x is not None and (
                    not math.isfinite(x)
                    or x < lo
                    or x > hi
                    or (name == "revenue_growth" and x == -1)
                ):
                    raise ValueError(f"projection.{name} outside supported finite domain")
        return self


class WACCAssumptions(_Base):
    risk_free_rate: float = Field(0.042, ge=0.0, le=0.25)
    equity_risk_premium: float = Field(0.055, gt=0.0, le=0.20)
    beta_override: float | None = Field(None, ge=-5.0, le=10.0)
    discount_rate_override: float | None = Field(None, gt=0.0, le=1.0)
    cost_of_debt_override: float | None = Field(None, ge=0.0, le=0.50)
    size_premium: float = Field(0.0, ge=0.0, le=0.10)
    country_risk_premium: float = Field(0.0, ge=0.0, le=0.20)
    target_debt_weight: float = Field(0.20, ge=0.0, le=0.95)
    capital_structure: Literal["current", "target"] = "current"
    floor_cost_of_equity: bool = False


class SBCAssumptions(_Base):
    """Stock-based compensation policy.

    The two treatments are alternative approximations and mutually exclusive:

      expense -- SBC stays an operating cost inside EBIT, and the share count is
                 held at the current diluted figure plus treasury-stock overhang.
      dilute  -- SBC is added back as a non-cash charge, and the share count grows
                 each year as new stock is issued to employees.

    Charging the cash flow AND growing the denominator bills shareholders twice for
    one cost. `deduct_from_fcf` and `grow_share_count` derive from `method` but can
    be set explicitly for testing; the validator refuses any double-counting combo.
    """

    method: Literal["expense", "dilute"] = "expense"
    forecast_method: Literal["pct_revenue", "pct_sga", "explicit"] = "pct_revenue"
    sbc_pct_revenue: Series | None = None
    sbc_pct_sga: Series = 0.15
    explicit: list[float] | None = None
    option_overhang_shares: float | None = Field(None, ge=0.0)
    buyback_offset_pct: float = Field(0.0, ge=0.0, le=1.0)

    deduct_from_fcf: bool | None = None
    grow_share_count: bool | None = None

    def __setattr__(self, name, value):
        if name == "method" and hasattr(self, "method"):
            if value not in ("expense", "dilute"):
                raise ValueError("invalid SBC method")
            object.__setattr__(self, "deduct_from_fcf", value == "expense")
            object.__setattr__(self, "grow_share_count", value == "dilute")
        super().__setattr__(name, value)

    @model_validator(mode="after")
    def _resolve_and_guard(self) -> SBCAssumptions:
        if self.deduct_from_fcf is None:
            object.__setattr__(self, "deduct_from_fcf", self.method == "expense")
        if self.grow_share_count is None:
            object.__setattr__(self, "grow_share_count", self.method == "dilute")

        if self.deduct_from_fcf and self.grow_share_count:
            raise ValueError(
                "SBC double-count: deduct_from_fcf and grow_share_count are both true. "
                "Expensing SBC and diluting the share count are two routes to the same "
                "answer, so applying both charges shareholders twice. Pick one via "
                "sbc.method (expense or dilute)."
            )
        if not self.deduct_from_fcf and not self.grow_share_count:
            raise ValueError(
                "SBC is being ignored: deduct_from_fcf and grow_share_count are both "
                "false, which treats stock compensation as free. That is the error this "
                "model exists to avoid."
            )
        if self.grow_share_count != (self.method == "dilute"):
            raise ValueError("SBC method and policy flags disagree")
        for name in ("sbc_pct_revenue", "sbc_pct_sga", "explicit"):
            val = getattr(self, name)
            if val is not None and any(
                not math.isfinite(x) or x < 0 for x in (val if isinstance(val, list) else [val])
            ):
                raise ValueError(f"sbc.{name} must be finite and nonnegative")
        if self.forecast_method == "explicit" and not self.explicit:
            raise ValueError("sbc.forecast_method is explicit but sbc.explicit is empty")
        return self


class TerminalAssumptions(_Base):
    method: Literal["gordon", "exit_multiple", "both", "value_driver"] = "both"
    perpetuity_growth: float = Field(0.025, ge=-0.02, le=0.06)
    terminal_fcf_mode: Literal["fcf5", "value_driver"] = "fcf5"
    ronic: float | None = Field(None, gt=0.0, le=2.0)
    exit_multiple_mode: Literal["static", "dynamic"] = "dynamic"
    static_exit_multiple: float = Field(12.0, gt=0.0, le=100.0)
    decay_turns_per_pp: float = Field(2.0, ge=0.0, le=20.0)
    mature_industry_multiple: float = Field(10.0, gt=0.0, le=50.0)
    max_implied_growth: float = Field(0.035, ge=0.0, le=0.10)


class BridgeAssumptions(_Base):
    minority_interest: float | None = None
    preferred_equity: float | None = None
    investments: float | None = None
    include_investments: bool = False
    cash_includes_restricted: bool = False


class CompsAssumptions(_Base):
    peers: list[str] = Field(default_factory=list)
    max_peers: int = Field(8, ge=1, le=30)
    # Below this many screened peers the medians are reported but not used to drive
    # the terminal multiple. Two comparables is an anecdote, not a benchmark.
    min_peers: int = Field(3, ge=1, le=30)
    screen_outliers: bool = True


class MonteCarloAssumptions(_Base):
    enabled: bool = True
    iterations: int = Field(10_000, ge=100, le=1_000_000)
    seed: int = 42
    wacc_std: float = Field(0.010, ge=0.0, le=0.10)
    terminal_growth_std: float = Field(0.005, ge=0.0, le=0.05)
    ebit_margin_std: float = Field(0.020, ge=0.0, le=0.20)
    corr_wacc_growth: float = Field(0.35, ge=-0.99, le=0.99)
    corr_wacc_margin: float = Field(-0.15, ge=-0.99, le=0.99)
    corr_growth_margin: float = Field(0.25, ge=-0.99, le=0.99)

    @model_validator(mode="after")
    def _valid_correlation(self):
        a, b, c = self.corr_wacc_growth, self.corr_wacc_margin, self.corr_growth_margin
        if np.linalg.eigvalsh([[1, a, b], [a, 1, c], [b, c, 1]]).min() <= 1e-10:
            raise ValueError("Monte Carlo correlation matrix must be positive definite")
        return self


class SensitivityAssumptions(_Base):
    wacc_deltas: list[float] = Field(default_factory=lambda: [-0.02, -0.01, 0.0, 0.01, 0.02])
    growth_deltas: list[float] = Field(default_factory=lambda: [-0.01, -0.005, 0.0, 0.005, 0.01])
    multiple_deltas: list[float] = Field(default_factory=lambda: [-2.0, -1.0, 0.0, 1.0, 2.0])


class SolverAssumptions(_Base):
    tolerance: float = Field(0.001, gt=0.0, le=0.1)
    max_iterations: int = Field(50, ge=1, le=1000)


class CurrencyAssumptions(_Base):
    """How to handle a company that reports and trades in different currencies.

    Yahoo gives the statements in the reporting currency and the price, market cap and
    share count in the listing currency. For an ADR these differ, and the model has no
    way to reconcile them on its own: Toyota's JPY statements against its USD quote
    produced $79,467 per share against a $198 price, and a 0.43% WACC from weighing a
    USD market cap against JPY debt.

    So the default is to refuse. `fx_rate` or `auto_fx` converts the statements into
    the price currency, which also makes the discount rate coherent -- a USD risk-free
    rate and equity risk premium belong against USD cash flows.
    """

    # Statement currency -> price currency. 1 unit of statement currency buys this
    # many units of price currency (JPY->USD is roughly 0.0063).
    fx_rate: float | None = Field(None, gt=0.0)
    auto_fx: bool = False
    # Proceed without converting. Only sensible if you have already reconciled the
    # units yourself; nothing downstream will check them again.
    allow_mismatch: bool = False

    @model_validator(mode="after")
    def _one_source_of_truth(self) -> CurrencyAssumptions:
        if self.fx_rate is not None and self.auto_fx:
            raise ValueError(
                "currency: fx_rate and auto_fx are both set. Supply a rate or fetch "
                "one, not both -- otherwise which rate produced a valuation depends "
                "on precedence rather than on what you asked for."
            )
        if self.allow_mismatch and (self.fx_rate is not None or self.auto_fx):
            raise ValueError(
                "currency: allow_mismatch skips conversion, so setting it alongside "
                "fx_rate or auto_fx asks for two contradictory things."
            )
        return self

    @property
    def converts(self) -> bool:
        return self.fx_rate is not None or self.auto_fx


class QualityAssumptions(_Base):
    min_history_years: int = Field(2, ge=1, le=10)
    max_sbc_pct_revenue: float = Field(0.60, gt=0.0, le=5.0)
    # Value a bank, insurer, asset manager or broker-dealer anyway. Unlevered free
    # cash flow treats financing as outside the operating business, which is exactly
    # backwards for a lender or an underwriter -- for them financing IS the business.
    allow_unsuitable_sector: bool = False


class DCFAssumptions(_Base):
    projection: ProjectionAssumptions = Field(default_factory=ProjectionAssumptions)
    wacc: WACCAssumptions = Field(default_factory=WACCAssumptions)
    sbc: SBCAssumptions = Field(default_factory=SBCAssumptions)
    terminal: TerminalAssumptions = Field(default_factory=TerminalAssumptions)
    bridge: BridgeAssumptions = Field(default_factory=BridgeAssumptions)
    comps: CompsAssumptions = Field(default_factory=CompsAssumptions)
    monte_carlo: MonteCarloAssumptions = Field(default_factory=MonteCarloAssumptions)
    sensitivity: SensitivityAssumptions = Field(default_factory=SensitivityAssumptions)
    solver: SolverAssumptions = Field(default_factory=SolverAssumptions)
    quality: QualityAssumptions = Field(default_factory=QualityAssumptions)
    currency: CurrencyAssumptions = Field(default_factory=CurrencyAssumptions)

    valuation_date: str | None = None
    scenario: str = "base"

    @model_validator(mode="after")
    def _cross_section_checks(self) -> DCFAssumptions:
        years = self.projection.years
        if self.sbc.explicit is not None and len(self.sbc.explicit) != years:
            raise ValueError(
                f"sbc.explicit has {len(self.sbc.explicit)} values but projection.years is {years}"
            )
        if isinstance(self.sbc.sbc_pct_revenue, list) and len(self.sbc.sbc_pct_revenue) != years:
            raise ValueError(
                f"sbc.sbc_pct_revenue has {len(self.sbc.sbc_pct_revenue)} values "
                f"but projection.years is {years}"
            )
        if (
            self.terminal.exit_multiple_mode == "static"
            and self.terminal.mature_industry_multiple > self.terminal.static_exit_multiple
        ):
            raise ValueError(
                "terminal.mature_industry_multiple exceeds terminal.static_exit_multiple; "
                "the floor would silently override the multiple you set"
            )
        return self

    @classmethod
    def from_yaml(
        cls,
        base_path: str | Path = "config/assumptions.yaml",
        scenario: str = "base",
        scenarios_path: str | Path | None = "config/scenarios.yaml",
        overrides: dict[str, Any] | None = None,
    ) -> DCFAssumptions:
        """Load base assumptions, deep-merge the named scenario, then any CLI overrides."""
        resolved_base = resolve_config_path(base_path)
        if not resolved_base.exists():
            raise FileNotFoundError(
                f"assumptions file not found: {base_path} (looked in the working directory "
                f"and in {PACKAGE_ROOT})"
            )
        base = yaml.safe_load(resolved_base.read_text(encoding="utf-8")) or {}

        if scenarios_path is not None:
            resolved_scenarios = resolve_config_path(scenarios_path)
            if resolved_scenarios.exists():
                scenarios = yaml.safe_load(resolved_scenarios.read_text(encoding="utf-8")) or {}
                if scenario not in scenarios:
                    raise ValueError(
                        f"unknown scenario {scenario!r}; available: {sorted(scenarios)}"
                    )
                base = deep_merge(base, scenarios.get(scenario) or {})
            elif scenario != "base":
                # Never silently return base-case numbers under a bull or bear heading.
                raise FileNotFoundError(
                    f"scenario {scenario!r} was requested but no scenarios file was found at "
                    f"{scenarios_path} (looked in the working directory and in {PACKAGE_ROOT}). "
                    f"Refusing to fall back to the base case under a {scenario!r} label."
                )

        if overrides:
            base = deep_merge(base, overrides)

        base["scenario"] = scenario
        return cls.model_validate(base)
