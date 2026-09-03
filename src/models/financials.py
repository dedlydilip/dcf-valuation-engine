"""Canonical financial-statement schema.

Yahoo's row labels are title-cased English prose that changes between releases
("Depreciation And Amortization" vs "Depreciation Amortization Depletion"). Every
downstream module reads the snake_case canonical names defined here instead, so a
Yahoo rename is a one-line fix in FIELD_MAP rather than a hunt through the engine.

Sign conventions, fixed once so no module has to guess:

  capex             positive = cash spent on capex (Yahoo reports it negative)
  nwc_investment    positive = cash consumed by a build in working capital
  every other field carries its natural reporting sign
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

# canonical name -> candidate Yahoo row labels, first match wins
FIELD_MAP: dict[str, list[str]] = {
    # income statement
    "revenue": ["Total Revenue", "Operating Revenue"],
    "cost_of_revenue": ["Cost Of Revenue", "Reconciled Cost Of Revenue"],
    "gross_profit": ["Gross Profit"],
    "sga": ["Selling General And Administration"],
    "rnd": ["Research And Development"],
    "operating_income": ["Operating Income", "Total Operating Income As Reported"],
    "ebit": ["EBIT", "Operating Income"],
    "ebitda": ["EBITDA", "Normalized EBITDA"],
    "interest_expense": ["Interest Expense", "Interest Expense Non Operating"],
    "pretax_income": ["Pretax Income"],
    "tax_provision": ["Tax Provision"],
    "net_income": ["Net Income", "Net Income Common Stockholders"],
    "diluted_shares": ["Diluted Average Shares", "Basic Average Shares"],
    # cash flow statement
    "cfo": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
    "capex": ["Capital Expenditure", "Purchase Of PPE"],
    "da": ["Depreciation And Amortization", "Depreciation Amortization Depletion"],
    "sbc": ["Stock Based Compensation"],
    "change_in_working_capital": ["Change In Working Capital"],
    "fcf_reported": ["Free Cash Flow"],
    "buybacks": ["Repurchase Of Capital Stock"],
    # balance sheet
    "total_debt": ["Total Debt"],
    "current_debt": [
        "Current Debt",
        "Current Debt And Capital Lease Obligation",
        "Short Term Debt",
    ],
    # Kept strictly separate. The combined row ALREADY includes short-term investments,
    # so letting `cash` fall back to it and then adding `short_term_investments` again
    # double-counts the investments in both the WACC and the equity bridge. Read the
    # position through `total_cash_position()` rather than summing these by hand.
    "cash": ["Cash And Cash Equivalents"],
    "cash_and_sti_combined": ["Cash Cash Equivalents And Short Term Investments"],
    "short_term_investments": ["Other Short Term Investments"],
    # Not distributable and not available to the operating business, so it does not
    # belong in the cash that comes off enterprise value.
    "restricted_cash": ["Restricted Cash"],
    "current_assets": ["Current Assets", "Total Current Assets"],
    "current_liabilities": ["Current Liabilities", "Total Current Liabilities"],
    "stockholders_equity": ["Stockholders Equity"],
    "total_equity_incl_minority": ["Total Equity Gross Minority Interest"],
    # Ahead of the common in the capital structure, so it comes off enterprise value
    # the same way debt and minorities do. The bridge has always had a line for this,
    # but no way to populate it: the derived value was hardcoded NaN, so a company
    # with preferred stock valued at zero preferred unless someone set it by hand.
    "preferred_equity": ["Preferred Stock", "Preferred Securities Outside Stock Equity"],
    # Non-operating. Reported so `build_bridge` can warn, rather than added
    # automatically -- see the note there on why this stays the analyst's call.
    "long_term_investments": ["Investments And Advances", "Long Term Equity Investment"],
    "ordinary_shares": ["Ordinary Shares Number", "Share Issued"],
    "invested_capital": ["Invested Capital"],
    "net_ppe": ["Net PPE"],
    "working_capital": ["Working Capital"],
}

# Fields the engine genuinely cannot proceed without.
#
# Debt and cash are deliberately NOT here. A company with no borrowings has no
# "Total Debt" row at all, and rejecting it outright would refuse most debt-free
# software companies. An absent balance is zero, not a data failure -- the quality
# gate warns instead.
REQUIRED_FIELDS: tuple[str, ...] = ("revenue", "ebit")

# Balance-sheet fields whose absence means zero, with a warning.
ZERO_DEFAULT_FIELDS: tuple[str, ...] = ("total_debt", "cash")

# Fields whose absence is survivable; the engine substitutes a documented default.
OPTIONAL_FIELDS: tuple[str, ...] = (
    "sbc",
    "current_debt",
    "short_term_investments",
    "cash_and_sti_combined",
    "restricted_cash",
    "preferred_equity",
    "long_term_investments",
    "buybacks",
    "interest_expense",
    "ebitda",
)

# Balance-sheet fields used to detect a period that carries an income statement but
# no balance sheet -- the mixed-vintage trap.
BALANCE_SHEET_FIELDS: tuple[str, ...] = (
    "total_debt",
    "cash",
    "cash_and_sti_combined",
    "current_assets",
    "current_liabilities",
    "stockholders_equity",
)

INFO_KEYS: tuple[str, ...] = (
    "beta",
    "marketCap",
    "sharesOutstanding",
    "currentPrice",
    "sector",
    "industry",
    "enterpriseValue",
    "totalDebt",
    "totalCash",
    "trailingPE",
    "enterpriseToEbitda",
    "revenueGrowth",
    # Two different currencies, and the difference is the whole point. Yahoo reports
    # the statements in `financialCurrency` and the price, market cap and share count
    # in `currency`. For an ADR they diverge -- Toyota files in JPY and trades in USD
    # -- and reading one as though it were the other makes every downstream figure
    # incoherent. `currency` was missing here, so fixtures could not even record the
    # mismatch, which is why no offline test could have caught it.
    "financialCurrency",
    "currency",
    "longName",
)


@dataclass
class Financials:
    """One company's history in canonical form.

    `statements` is indexed by canonical field name with one column per fiscal
    period, ordered oldest to newest so `.iloc[-1]` is always the latest year.
    """

    ticker: str
    statements: pd.DataFrame
    info: dict[str, Any] = field(default_factory=dict)
    prices: pd.DataFrame | None = None
    # The currency the statements are in *now*, which is not necessarily the one the
    # company files in: converting an ADR rewrites this to the quote currency.
    currency: str = "USD"
    source: str = "yfinance"
    # Set only when a conversion happened, so the workbook and the CLI can say which
    # rate produced the numbers. A valuation that quietly changed units is worse than
    # one that refused.
    fx_rate_applied: float | None = None
    original_currency: str | None = None

    @property
    def converted(self) -> bool:
        return self.fx_rate_applied is not None

    @property
    def periods(self) -> list[Any]:
        return list(self.statements.columns)

    @property
    def n_years(self) -> int:
        return len(self.statements.columns)

    def series(self, field_name: str) -> pd.Series:
        """Full history for one field, oldest to newest. Missing field -> all-NaN."""
        if field_name not in self.statements.index:
            return pd.Series(
                [float("nan")] * self.n_years, index=self.statements.columns, name=field_name
            )
        return self.statements.loc[field_name]

    def latest(self, field_name: str, default: float | None = None) -> float:
        """Most recent non-null value for a field, else `default`."""
        s = self.series(field_name).dropna()
        if s.empty:
            if default is None:
                return float("nan")
            return float(default)
        return float(s.iloc[-1])

    def mean_pct_revenue(self, field_name: str, years: int = 3) -> float:
        """Trailing average of a field as a share of revenue.

        Used to default forecast ratios (capex, D&A, SBC) off history instead of
        asking the user to invent a number.
        """
        rev = self.series("revenue")
        val = self.series(field_name)
        ratio = (val / rev.replace(0, pd.NA)).dropna()
        if ratio.empty:
            return float("nan")
        return float(ratio.tail(years).mean())

    def net_working_capital(self) -> pd.Series:
        """Operating net working capital, excluding cash and current debt.

        Cash and debt are financing items and belong in the EV-to-equity bridge,
        not in the operating capital the business ties up.
        """
        ca = self.series("current_assets")
        cl = self.series("current_liabilities")
        cash = self.series("cash").fillna(0.0)
        sti = self.series("short_term_investments").fillna(0.0)
        total_cash_sti = cash + sti
        combined = self.series("cash_and_sti_combined").fillna(0.0)
        # Use combined row if separate cash/sti are zero or missing across the series
        cash_deduction = (
            total_cash_sti if (total_cash_sti.abs().sum() > 0 or combined.empty) else combined
        )
        cur_debt = self.series("current_debt").fillna(0.0)
        return (ca - cash_deduction) - (cl - cur_debt)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "currency": self.currency,
            "source": self.source,
            "info": self.info,
            "statements": self.statements.to_dict(),
        }


def field_series(source: Any, name: str) -> pd.Series:
    """Read one field from a Financials, or from a DataFrame in either orientation.

    The engine's own data arrives as a `Financials` (fields on the index). Tests and
    ad-hoc analysis are far more natural to write with fields as DataFrame columns.
    Supporting both here keeps that convenience from leaking a second code path into
    every calculator.
    """
    if isinstance(source, Financials):
        return source.series(name)
    if isinstance(source, pd.DataFrame):
        if name in source.index:
            return source.loc[name]
        if name in source.columns:
            return source[name]
        return pd.Series(dtype="float64", name=name)
    if isinstance(source, dict):
        val = source.get(name)
        if val is None:
            return pd.Series(dtype="float64", name=name)
        if isinstance(val, (list, tuple, pd.Series)):
            return pd.Series(list(val), name=name, dtype="float64")
        return pd.Series([float(val)], name=name, dtype="float64")
    return pd.Series(dtype="float64", name=name)


def total_cash_position(source: Any, default: float | None = None) -> float:
    """Unrestricted cash plus short-term investments, counted exactly once.

    Providers report this two ways: a pure cash row alongside a separate investments
    row, or a single combined row. Summing whatever is present double-counts the
    investments whenever the combined row is the one that survived. The rule is
    therefore explicit: prefer pure cash plus investments, and fall back to the
    combined row only when pure cash is unavailable.

    Restricted cash then comes off. It is pledged against something -- collateral, an
    escrow, a regulatory reserve -- so it is neither distributable to shareholders nor
    available to the business, and crediting it against enterprise value overstates
    equity value by its full amount. Providers include it in the headline balance for
    companies that report it separately.
    """
    cash = field_value(source, "cash")
    sti = field_value(source, "short_term_investments")
    combined = field_value(source, "cash_and_sti_combined")

    gross = float("nan")
    if pd.notna(cash):
        gross = float(cash + (sti if pd.notna(sti) else 0.0))
    elif pd.notna(combined):
        gross = float(combined)
    elif pd.notna(sti):
        gross = float(sti)
    else:
        info = info_dict(source)
        if info.get("totalCash"):
            gross = float(info["totalCash"])

    if pd.isna(gross):
        return float("nan") if default is None else float(default)

    restricted = field_value(source, "restricted_cash")
    if pd.isna(restricted) or restricted <= 0:
        return gross

    net = gross - float(restricted)
    # Worth saying out loud rather than silently shrinking the number: the deduction
    # flows straight through to equity value, and a reader comparing against the
    # provider's headline cash figure needs to know why the two differ.
    if gross > 0 and restricted / gross > 0.05:
        warnings.warn(
            f"Restricted cash of {restricted:,.0f} ({restricted / gross:.1%} of the "
            f"reported balance) is excluded from the cash that comes off enterprise "
            f"value: it is pledged, so it is not distributable.",
            UserWarning,
            stacklevel=2,
        )
    return net


def info_dict(source: Any) -> dict[str, Any]:
    """Metadata payload for a source, or an empty dict.

    Not simply `getattr(source, "info", {})`: `DataFrame.info` is a *method*, so the
    naive version hands back a bound function that every caller then tries to call
    `.get` on. Checking the type is what makes DataFrame inputs safe.
    """
    info = getattr(source, "info", None)
    return info if isinstance(info, dict) else {}


def statement_currency(source: Any) -> str:
    """Currency the income statement and balance sheet are reported in."""
    info = info_dict(source)
    raw = info.get("financialCurrency") or getattr(source, "currency", None)
    return str(raw).upper() if raw else ""


def price_currency(source: Any) -> str:
    """Currency the share price, market cap and share count are quoted in.

    Different from `statement_currency` for any ADR or cross-listing. Toyota files in
    JPY and trades in USD; comparing a JPY-derived value per share against a USD price
    is meaningless, and weighing a USD market cap against JPY debt in the WACC is worse
    -- it returned a 0.43% discount rate.
    """
    info = info_dict(source)
    raw = info.get("currency")
    return str(raw).upper() if raw else ""


def share_count_basis_gap(source: Any) -> float:
    """How far `sharesOutstanding` sits from `marketCap / price`.

    These must describe the same thing. For an ADR the count has to be on the listed
    basis, not the ordinary-share basis, or per-share value is wrong by the ADR ratio
    -- Toyota's is 10:1, so the error would be an order of magnitude with no symptom.

    Verified at exactly 1.00 for TM, BABA, TSM, SAP and AAPL, so the ADR ratio is
    already inside Yahoo's number and needs no separate lookup. This exists to notice
    if that ever stops being true. Returns NaN when the inputs are not all present.
    """
    info = info_dict(source)
    shares = info.get("sharesOutstanding")
    market_cap = info.get("marketCap")
    price = info.get("currentPrice")
    if not shares or not market_cap or not price:
        return float("nan")
    implied = float(market_cap) / float(price)
    if implied <= 0:
        return float("nan")
    return abs(float(shares) / implied - 1.0)


def field_value(source: Any, name: str, default: float | None = None) -> float:
    """Latest non-null value of a field from any supported container."""
    series = field_series(source, name)
    if series is None or len(series) == 0:
        return float("nan") if default is None else float(default)
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return float("nan") if default is None else float(default)
    return float(clean.iloc[-1])
