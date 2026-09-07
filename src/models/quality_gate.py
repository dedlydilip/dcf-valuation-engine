"""Pre-flight data validation.

Runs before any valuation maths. The purpose is to fail loudly on garbage rather
than quietly return a share price computed from NaNs -- a delisted shell, a SPAC
with no operating history, or a ticker Yahoo simply has nothing for will all
produce a number if you let them.

Two severities:
  critical -- raises DataQualityError, the valuation cannot proceed
  warning  -- surfaces as a UserWarning and may change model behaviour, e.g.
              negative EBITDA disables the exit-multiple terminal value
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.models.errors import DataQualityError
from src.models.financials import (
    BALANCE_SHEET_FIELDS,
    REQUIRED_FIELDS,
    ZERO_DEFAULT_FIELDS,
    Financials,
    field_series,
    field_value,
    info_dict,
    price_currency,
    share_count_basis_gap,
    statement_currency,
)

# Industries an unlevered DCF cannot describe, matched case-insensitively as
# substrings of Yahoo's `industry` field. Verified against the live taxonomy:
# "Banks - Diversified" (JPM), "Banks - Regional" (USB), "Insurance - Property &
# Casualty" (PGR), "Insurance - Diversified" (BRK-B), "Capital Markets" (GS),
# "Asset Management" (BLK, KKR).
UNSUITABLE_INDUSTRIES: tuple[str, ...] = (
    "banks",
    "insurance",
    "capital markets",
    "asset management",
    "mortgage finance",
    "credit services",
)

# Checked first, and exempt. All three are filed under "Financial Services" but are
# ordinary fee businesses with no underwriting or lending balance sheet: "Credit
# Services" (V, MA, AXP), "Financial Data & Stock Exchanges" (SPGI, CME, ICE) and
# "Insurance Brokers" (AJG) -- the last of which the "insurance" rule would
# otherwise catch.
SUITABLE_FINANCIAL_INDUSTRIES: tuple[str, ...] = (
    "financial data",
    "stock exchanges",
    "insurance brokers",
)


@dataclass
class QualityReport:
    critical: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.critical

    def emit_warnings(self) -> None:
        for message in self.warnings:
            warnings.warn(message, UserWarning, stacklevel=3)


class DataQualityGate:
    """Validate input financials before the DCF engine touches them."""

    REQUIRED_FIELDS: tuple[str, ...] = REQUIRED_FIELDS

    def __init__(
        self,
        financials: Any,
        min_history_years: int = 1,
        max_sbc_pct_revenue: float = 0.60,
        currency_reconciled: bool = False,
        allow_unsuitable_sector: bool = False,
    ) -> None:
        self.financials = financials
        self.min_history_years = min_history_years
        self.max_sbc_pct_revenue = max_sbc_pct_revenue
        # True once the caller has either converted the statements into the price
        # currency or explicitly accepted the mismatch. The gate never converts
        # anything itself; it only refuses to proceed on incoherent units.
        self.currency_reconciled = currency_reconciled
        self.allow_unsuitable_sector = allow_unsuitable_sector
        self.report = QualityReport()

    # ------------------------------------------------------------------ helpers

    @property
    def flags(self) -> list[str]:
        """Critical flags, kept as a plain list for readable assertions in tests."""
        return self.report.critical

    def _present(self, name: str) -> bool:
        series = field_series(self.financials, name)
        if series is None or len(series) == 0:
            return False
        return bool(pd.to_numeric(series, errors="coerce").notna().any())

    # --------------------------------------------------------------- validation

    def validate(self, raise_on_critical: bool = True) -> QualityReport:
        self.report = QualityReport()

        source = self.financials
        if isinstance(source, Financials):
            values = source.statements.to_numpy(dtype=float)
            import numpy as np

            if np.isinf(values).any():
                self.report.critical.append("CRITICAL: infinite financial input")
            for key in ("marketCap", "sharesOutstanding", "currentPrice", "beta"):
                val = source.info.get(key)
                if val is not None and not math.isfinite(float(val)):
                    self.report.critical.append(f"CRITICAL: non-finite {key}")
            if not source.info.get("currency"):
                self.report.warnings.append(
                    "Quote currency is unknown; verify units before interpreting per-share value."
                )
        for name in self.REQUIRED_FIELDS:
            if not self._present(name):
                self.report.critical.append(f"CRITICAL: missing or entirely null field '{name}'")

        # Absent debt or cash means zero, not a data failure. A company that has never
        # borrowed has no "Total Debt" row at all, and refusing to value it would rule
        # out most debt-free software companies.
        for name in ZERO_DEFAULT_FIELDS:
            if not self._present(name):
                self.report.warnings.append(
                    f"No '{name}' reported; treating it as zero in the enterprise-value "
                    f"bridge. Confirm the company genuinely has none."
                )

        revenue = field_value(self.financials, "revenue")
        if pd.notna(revenue):
            if revenue < 0:
                self.report.critical.append("CRITICAL: negative revenue")
            elif revenue == 0:
                self.report.critical.append("CRITICAL: zero revenue, nothing to forecast from")

        n_periods = self._period_count()
        if n_periods is not None and n_periods < self.min_history_years:
            self.report.critical.append(
                f"CRITICAL: only {n_periods} usable fiscal period(s), need {self.min_history_years}"
            )

        self._check_currency_coherence()
        self._check_sector_suitability()
        self._check_period_vintage()
        self._check_warnings()

        if self.report.critical and raise_on_critical:
            raise DataQualityError(
                "Insufficient data quality to run a valuation:\n  "
                + "\n  ".join(self.report.critical)
            )
        return self.report

    def _period_count(self) -> int | None:
        if isinstance(self.financials, Financials):
            return self.financials.n_years
        return None

    def _check_period_vintage(self) -> None:
        """Catch an income statement bolted onto an older balance sheet.

        Providers routinely publish the income statement for a new fiscal year before
        the balance sheet lands. The newest column then carries revenue but no debt or
        cash, so the period survives the empty-period filter while every balance-sheet
        lookup silently falls back a year. The result is FY(N) earnings bridged against
        an FY(N-1) balance sheet, with a plausible number and no complaint.
        """
        if not isinstance(self.financials, Financials):
            return
        statements = self.financials.statements
        if statements.empty or len(statements.columns) == 0:
            return

        latest = statements.columns[-1]
        present = [
            name
            for name in BALANCE_SHEET_FIELDS
            if name in statements.index and pd.notna(statements.loc[name, latest])
        ]
        available = [name for name in BALANCE_SHEET_FIELDS if name in statements.index]
        if available and not present:
            self.report.warnings.append(
                f"The most recent period ({str(latest)[:10]}) carries an income statement but "
                f"no balance sheet. Debt, cash and working capital fall back to the prior "
                f"year, so the valuation mixes fiscal vintages. Re-run once the balance "
                f"sheet is published, or drop the stub period."
            )

        # Individually stale fields: present in history, missing in the newest period.
        stale = [
            name
            for name in (*BALANCE_SHEET_FIELDS, "interest_expense", "sbc", "da", "capex", "ebit")
            if name in statements.index
            and pd.isna(statements.loc[name, latest])
            and statements.loc[name].notna().any()
        ]
        if stale and present:
            self.report.warnings.append(
                f"Not reported in the most recent period, so an earlier year is used: "
                f"{', '.join(sorted(stale))}."
            )

    def _check_currency_coherence(self) -> None:
        """Refuse when the statements and the quote are in different currencies.

        Yahoo reports the statements in the company's reporting currency and the
        price, market cap and share count in the listing currency. For any ADR these
        differ, and nothing downstream reconciles them, so every figure the model
        produces mixes units:

            TM   JPY statements, USD quote  ->  $79,467 per share against $198
            SAP  EUR statements, USD quote  ->  $176.90 against $210

        Toyota is obvious. SAP is the reason this is a critical rather than a warning:
        at a EUR/USD rate near 1.16 the answer looks entirely reasonable and is still
        wrong. The WACC goes with it -- a USD market cap weighed against JPY debt
        returned a 0.43% discount rate.
        """
        if self.currency_reconciled:
            return
        statement = statement_currency(self.financials)
        price = price_currency(self.financials)
        if not statement or not price or statement == price:
            return
        self.report.critical.append(
            f"CRITICAL: the statements are reported in {statement} but the share price, "
            f"market capitalisation and share count are quoted in {price}. Every figure "
            f"downstream would mix the two -- value per share against the wrong price, "
            f"and a WACC weighing a {price} market cap against {statement} debt. Supply "
            f"--fx-rate, or use --auto-fx to fetch the spot rate, or set "
            f"currency.allow_mismatch if you have already reconciled the units yourself."
        )

    def _check_sector_suitability(self) -> None:
        """Refuse businesses an unlevered DCF cannot describe.

        Unlevered free cash flow deliberately excludes financing so that the operating
        business can be valued independently of how it is funded. For a bank, insurer,
        asset manager or broker-dealer that is exactly backwards: financing IS the
        business. Deposits, float and leverage are the product, not the funding.

        Banks already fail structurally -- they report no EBIT line, because interest
        is revenue rather than a financing cost. Insurers slipped through, because they
        do report something EBIT-shaped: Progressive returned $837.92 against a $221.38
        price, Berkshire $1,378.52 against $505.24.

        Keyed on industry rather than sector, because Yahoo files Visa, Mastercard,
        S&P Global and the exchanges under "Financial Services" too, and those are
        ordinary fee businesses that an unlevered DCF handles perfectly well.
        """
        if self.allow_unsuitable_sector:
            return
        info = info_dict(self.financials)
        industry = str(info.get("industry") or "").strip().lower()
        if not industry:
            return  # absent for synthetic financials and occasionally for real ones

        # Brokerage is a fee business: commission income, no underwriting balance
        # sheet. Checked before the "insurance" match, which would otherwise catch it.
        if str(info.get("symbol") or getattr(self.financials, "ticker", "")).upper() in {"V", "MA"}:
            return
        if any(allowed in industry for allowed in SUITABLE_FINANCIAL_INDUSTRIES):
            return
        for unsuitable in UNSUITABLE_INDUSTRIES:
            if unsuitable in industry:
                self.report.critical.append(
                    f"CRITICAL: '{info.get('industry')}' is not a business an unlevered "
                    f"DCF can describe. Unlevered free cash flow excludes financing so "
                    f"the operating business can be valued on its own, but for a lender "
                    f"or an underwriter financing is the business -- deposits, float and "
                    f"leverage are the product. Use a dividend-discount, excess-returns "
                    f"or embedded-value model instead, or set "
                    f"quality.allow_unsuitable_sector to force this one through."
                )
                return

    def _check_share_count_basis(self) -> None:
        """`sharesOutstanding` and `marketCap / price` must describe the same thing.

        For an ADR the count has to be on the listed basis rather than the ordinary
        basis, or value per share is wrong by the ADR ratio -- Toyota's is 10:1, so
        the error would be an order of magnitude with no symptom at all. Measured at
        exactly 1.00 for TM, BABA, TSM, SAP and AAPL, which is what makes converting
        the currency sufficient. This notices if that ever stops holding.
        """
        gap = share_count_basis_gap(self.financials)
        if pd.notna(gap) and gap > 0.05:
            self.report.warnings.append(
                f"Share count and market capitalisation disagree by {gap:.1%}: "
                f"sharesOutstanding is not on the same basis as marketCap / price. For "
                f"a depositary receipt this usually means the count is ordinary shares "
                f"while the price is per ADR, which makes value per share wrong by the "
                f"ADR ratio."
            )

    def _check_warnings(self) -> None:
        self._check_share_count_basis()
        ebitda = field_value(self.financials, "ebitda")
        if pd.notna(ebitda) and ebitda < 0:
            self.report.warnings.append(
                "Negative EBITDA: exit-multiple terminal value is meaningless on a negative "
                "base, so the model falls back to Gordon Growth for the terminal value."
            )

        ebit = field_value(self.financials, "ebit")
        if pd.notna(ebit) and ebit < 0:
            self.report.warnings.append(
                "Negative EBIT: unlevered free cash flow starts negative. The DCF still "
                "runs, but the valuation rests almost entirely on the terminal value."
            )

        revenue = field_value(self.financials, "revenue")
        sbc = field_value(self.financials, "sbc")
        if pd.notna(sbc) and pd.notna(revenue) and revenue > 0:
            ratio = sbc / revenue
            if ratio > self.max_sbc_pct_revenue:
                self.report.warnings.append(
                    f"Stock-based compensation is {ratio:.0%} of revenue, above the "
                    f"{self.max_sbc_pct_revenue:.0%} threshold. Per-share value is highly "
                    f"sensitive to the SBC treatment."
                )
        if pd.notna(sbc) and pd.notna(ebit) and ebit > 0 and sbc > ebit:
            self.report.warnings.append(
                "SBC is material relative to remaining profit; EBIT already includes SBC. Do not subtract it again. "
                "Review compensation relative to revenue and margins."
            )

        if not self._present("sbc"):
            self.report.warnings.append(
                "No stock-based compensation reported: SBC is treated as zero, which "
                "understates the true cost of employees if the company in fact grants equity."
            )

        interest = field_value(self.financials, "interest_expense")
        debt = field_value(self.financials, "total_debt")
        if pd.notna(debt) and debt > 0 and not pd.notna(interest):
            self.report.warnings.append(
                "Debt on the balance sheet but no interest expense reported: cost of debt "
                "falls back to the risk-free rate."
            )


def run_quality_gate(
    financials: Any,
    min_history_years: int = 1,
    max_sbc_pct_revenue: float = 0.60,
    emit: bool = True,
    currency_reconciled: bool = False,
    allow_unsuitable_sector: bool = False,
) -> QualityReport:
    """Convenience wrapper: validate, emit warnings, return the report."""
    gate = DataQualityGate(
        financials,
        min_history_years,
        max_sbc_pct_revenue,
        currency_reconciled=currency_reconciled,
        allow_unsuitable_sector=allow_unsuitable_sector,
    )
    report = gate.validate()
    if emit:
        report.emit_warnings()
    return report
