"""Weighted average cost of capital.

Deliberately tolerant about where its inputs come from: a `Financials` object, a
raw DataFrame, or explicit keyword scalars. That is not laziness -- it is what makes
the edge cases directly testable ("what does this do with a negative beta?") without
first constructing a whole company.

Two conventions worth defending out loud:

  No floor on cost of equity. A negative beta produces a cost of equity below the
  risk-free rate. That is economically strange but mathematically correct, and
  clamping it would hide a real signal about the asset.

  No debt means cost of debt falls back to the risk-free rate. The number is
  unobservable when a company has never borrowed, and it carries zero weight in the
  WACC anyway, so the fallback cannot distort the result.
"""

from __future__ import annotations

import math
import warnings
from typing import Any

import pandas as pd

from src.models.assumptions import DCFAssumptions, WACCAssumptions
from src.models.financials import (
    Financials,
    field_series,
    field_value,
    info_dict,
    total_cash_position,
)

COST_OF_DEBT_FLOOR = 0.005
COST_OF_DEBT_CEILING = 0.35


class WACCCalculator:
    """Compute cost of equity, cost of debt, capital weights and WACC."""

    def __init__(
        self,
        financials: Any = None,
        assumptions: DCFAssumptions | WACCAssumptions | None = None,
        *,
        beta: float | None = None,
        rf: float | None = None,
        erp: float | None = None,
        market_cap: float | None = None,
        total_debt: float | None = None,
        cash: float | None = None,
        interest_expense: float | None = None,
        tax_rate: float | None = None,
        cost_of_debt: float | None = None,
        size_premium: float | None = None,
        country_risk_premium: float | None = None,
    ) -> None:
        self.financials = financials
        self._wacc_assumptions = _as_wacc_assumptions(assumptions)
        self._tax_rate_override = tax_rate
        self._assumptions = assumptions if isinstance(assumptions, DCFAssumptions) else None

        self._beta = beta
        self._rf = rf
        self._erp = erp
        self._market_cap = market_cap
        self._total_debt = total_debt
        self._cash = cash
        self._interest_expense = interest_expense
        self._cost_of_debt = cost_of_debt
        self._size_premium = size_premium
        self._country_risk_premium = country_risk_premium

    # -------------------------------------------------------------- primitives

    @property
    def risk_free_rate(self) -> float:
        return _first(self._rf, self._wacc_assumptions.risk_free_rate)

    @property
    def equity_risk_premium(self) -> float:
        return _first(self._erp, self._wacc_assumptions.equity_risk_premium)

    @property
    def size_premium(self) -> float:
        return _first(self._size_premium, self._wacc_assumptions.size_premium)

    @property
    def country_risk_premium(self) -> float:
        return _first(self._country_risk_premium, self._wacc_assumptions.country_risk_premium)

    @property
    def tax_rate(self) -> float:
        if self._tax_rate_override is not None:
            return float(self._tax_rate_override)
        if self._assumptions is not None:
            return self._assumptions.projection.tax_rate
        effective = self.effective_tax_rate
        return effective if pd.notna(effective) else 0.21

    @property
    def effective_tax_rate(self) -> float:
        """Tax provision over pretax income, ignored when either side is unusable."""
        tax = field_value(self.financials, "tax_provision")
        pretax = field_value(self.financials, "pretax_income")
        if pd.isna(tax) or pd.isna(pretax) or pretax <= 0:
            return float("nan")
        rate = tax / pretax
        return rate if 0.0 <= rate < 1.0 else float("nan")

    @property
    def raw_beta(self) -> float:
        if self._beta is not None:
            return float(self._beta)
        if self._wacc_assumptions.beta_override is not None:
            return float(self._wacc_assumptions.beta_override)
        info = info_dict(self.financials)
        raw = info.get("beta")
        if raw is not None and pd.notna(raw):
            return float(raw)
        return 1.0

    @property
    def beta(self) -> float:
        """Levered beta, re-levered via Hamada equation if target capital structure is used."""
        b = self.raw_beta
        if self._wacc_assumptions.capital_structure == "target":
            # Hamada (1972) re-levering:
            # 1. Unlever raw beta using current capital structure
            mcap = self.market_cap
            debt = self.total_debt
            t = self.tax_rate
            current_de = (
                debt / mcap if pd.notna(mcap) and mcap > 0 and pd.notna(debt) and debt >= 0 else 0.0
            )
            denom = 1.0 + (1.0 - t) * current_de
            unlevered_beta = b / denom if denom > 0 else b

            # 2. Re-lever to target capital structure
            target_wd = self._wacc_assumptions.target_debt_weight
            target_de = target_wd / (1.0 - target_wd) if target_wd < 1.0 else 0.0
            return unlevered_beta * (1.0 + (1.0 - t) * target_de)
        return b

    @property
    def market_cap(self) -> float:
        if self._market_cap is not None:
            return float(self._market_cap)
        info = info_dict(self.financials)
        if info.get("marketCap"):
            return float(info["marketCap"])
        shares = info.get("sharesOutstanding")
        price = info.get("currentPrice")
        if shares and price:
            return float(shares) * float(price)
        return float("nan")

    @property
    def total_debt(self) -> float:
        if self._total_debt is not None:
            return float(self._total_debt)
        value = field_value(self.financials, "total_debt")
        if pd.notna(value):
            return value
        info = info_dict(self.financials)
        return float(info["totalDebt"]) if info.get("totalDebt") else 0.0

    @property
    def cash(self) -> float:
        """Cash plus short-term investments: both are available to retire debt."""
        if self._cash is not None:
            return float(self._cash)
        return total_cash_position(self.financials, default=0.0)

    @property
    def interest_expense(self) -> float:
        if self._interest_expense is not None:
            return float(self._interest_expense)
        return field_value(self.financials, "interest_expense")

    # --------------------------------------------------------------- components

    @property
    def cost_of_equity(self) -> float:
        """CAPM, plus optional size and country premia.

        Unfloored by default to preserve raw mathematical CAPM properties (e.g. gold miners
        with negative beta). If floor_cost_of_equity is set in assumptions, floored at Rf.
        """
        coe = (
            self.risk_free_rate
            + self.beta * self.equity_risk_premium
            + self.size_premium
            + self.country_risk_premium
        )
        if getattr(self._wacc_assumptions, "floor_cost_of_equity", False):
            return max(coe, self.risk_free_rate)
        return coe

    @property
    def cost_of_debt(self) -> float:
        """Interest expense over average total debt, with documented fallbacks."""
        if self._cost_of_debt is not None:
            return float(self._cost_of_debt)
        if self._wacc_assumptions.cost_of_debt_override is not None:
            return float(self._wacc_assumptions.cost_of_debt_override)

        debt = self.total_debt
        interest = self.interest_expense

        # No debt, or no interest disclosed: unobservable and zero-weighted.
        if not debt or debt <= 0 or pd.isna(interest) or interest <= 0:
            return self.risk_free_rate

        average_debt = self._average_debt()
        implied = interest / average_debt if average_debt > 0 else interest / debt
        if not math.isfinite(implied):
            return self.risk_free_rate
        rate = min(max(implied, COST_OF_DEBT_FLOOR), COST_OF_DEBT_CEILING)

        # A cost of debt below the risk-free rate is not automatically wrong: a company
        # that termed out at 2% coupons in 2021 really does pay less than today's
        # Treasury. It is deliberately NOT clamped to rf plus a spread, because that
        # would overwrite a real fact about the balance sheet with an assumption.
        #
        # What it does reliably indicate is worth saying out loud, because the usual
        # cause is a vintage mismatch: when the latest period reports no interest
        # expense the model reaches back a year, pairing an older interest figure with
        # a current average debt balance. On the shipped Apple fixture that is exactly
        # what happens -- FY2023 interest against FY2024-25 average debt, 3.83% against
        # a 4.20% risk-free rate.
        if rate < self.risk_free_rate:
            warnings.warn(
                f"Cost of debt ({rate:.2%}) is below the risk-free rate "
                f"({self.risk_free_rate:.2%}). Legacy low-coupon debt can genuinely do "
                f"this, but check the vintages first: if interest expense is missing "
                f"from the latest period the model pairs an earlier year's interest "
                f"with current debt, which understates the rate. Set "
                f"wacc.cost_of_debt_override to state it explicitly.",
                UserWarning,
                stacklevel=2,
            )
        return rate

    def _average_debt(self) -> float:
        """Average the last two balance-sheet debt figures when history allows.

        Interest expense is a flow over the year; debt is a point in time. Pairing a
        full year of interest with a single year-end balance overstates the rate for
        a company that paid debt down during the period.
        """
        series = pd.to_numeric(field_series(self.financials, "total_debt"), errors="coerce").dropna()
        if len(series) >= 2:
            return float(series.iloc[-2:].mean())
        return self.total_debt

    @property
    def after_tax_cost_of_debt(self) -> float:
        return self.cost_of_debt * (1.0 - self.tax_rate)

    # ------------------------------------------------------------------ weights

    @property
    def net_debt(self) -> float:
        return self.total_debt - self.cash

    @property
    def enterprise_value(self) -> float:
        """Market cap plus total debt less cash. Goes below market cap when net cash."""
        return self.market_cap + self.total_debt - self.cash

    @property
    def debt_weight(self) -> float:
        if self._wacc_assumptions.capital_structure == "target":
            return self._wacc_assumptions.target_debt_weight
        capital = self.market_cap + self.total_debt
        if not math.isfinite(capital) or capital <= 0:
            return 0.0
        return self.total_debt / capital

    @property
    def equity_weight(self) -> float:
        return 1.0 - self.debt_weight

    @property
    def wacc(self) -> float:
        return self.equity_weight * self.cost_of_equity + self.debt_weight * (
            self.after_tax_cost_of_debt
        )

    # ------------------------------------------------------------------- output

    def summary(self) -> dict[str, float]:
        return {
            "risk_free_rate": self.risk_free_rate,
            "equity_risk_premium": self.equity_risk_premium,
            "beta": self.beta,
            "raw_beta": self.raw_beta,
            "size_premium": self.size_premium,
            "country_risk_premium": self.country_risk_premium,
            "cost_of_equity": self.cost_of_equity,
            "cost_of_debt": self.cost_of_debt,
            "tax_rate": self.tax_rate,
            "after_tax_cost_of_debt": self.after_tax_cost_of_debt,
            "market_cap": self.market_cap,
            "total_debt": self.total_debt,
            "cash": self.cash,
            "net_debt": self.net_debt,
            "enterprise_value": self.enterprise_value,
            "equity_weight": self.equity_weight,
            "debt_weight": self.debt_weight,
            "wacc": self.wacc,
        }


def _as_wacc_assumptions(assumptions: Any) -> WACCAssumptions:
    if isinstance(assumptions, DCFAssumptions):
        return assumptions.wacc
    if isinstance(assumptions, WACCAssumptions):
        return assumptions
    return WACCAssumptions()


def _first(*values: float | None) -> float:
    for value in values:
        if value is not None:
            return float(value)
    raise ValueError("no value supplied")


def build_wacc(financials: Financials, assumptions: DCFAssumptions) -> WACCCalculator:
    return WACCCalculator(financials, assumptions)
