"""Enterprise value to equity value to value per share.

Short module, easy to get wrong. Cash and short-term investments come off because
the operating forecast never counted the interest they earn. Minority interest and
preferred stock come off because enterprise value belongs to all capital providers,
not just common shareholders, and both have a claim ahead of the common.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.models.assumptions import DCFAssumptions
from src.models.errors import DataQualityError
from src.models.financials import field_value, info_dict, total_cash_position


@dataclass
class BridgeResult:
    enterprise_value: float
    total_debt: float
    cash: float
    minority_interest: float
    preferred_equity: float
    investments: float
    equity_value: float
    shares: float
    value_per_share: float
    current_price: float | None = None

    @property
    def net_debt(self) -> float:
        return self.total_debt - self.cash

    @property
    def upside(self) -> float | None:
        if not self.current_price or self.current_price <= 0:
            return None
        return self.value_per_share / self.current_price - 1.0

    def to_rows(self) -> list[tuple[str, float]]:
        """Ordered line items for the Excel bridge and terminal output."""
        return [
            ("Enterprise Value", self.enterprise_value),
            ("(-) Total Debt", -self.total_debt),
            ("(+) Cash & Short-Term Investments", self.cash),
            ("(-) Minority Interest", -self.minority_interest),
            ("(-) Preferred Equity", -self.preferred_equity),
            ("(+) Non-Operating Investments", self.investments),
            ("= Equity Value", self.equity_value),
            ("(/) Diluted Shares", self.shares),
            ("= Implied Value per Share", self.value_per_share),
        ]


def build_bridge(
    enterprise_value: float,
    financials: Any,
    assumptions: DCFAssumptions,
    shares: float,
    current_price: float | None = None,
) -> BridgeResult:
    cfg = assumptions.bridge

    total_debt = _resolve(None, field_value(financials, "total_debt"), 0.0)
    cash = _resolve(None, total_cash_position(financials), 0.0)
    minority = _resolve(cfg.minority_interest, _minority_from_balance(financials), 0.0)
    # Preferred had a line in the bridge and no way to populate it: the derived value
    # was hardcoded NaN, so `_resolve` fell through to zero unless config named a
    # figure. A company with preferred stock outstanding therefore valued at zero
    # preferred, silently, and the line item printed 0 as though that were reported.
    preferred = _resolve(cfg.preferred_equity, field_value(financials, "preferred_equity"), 0.0)
    reported_inv = field_value(financials, "long_term_investments")
    if cfg.include_investments and pd.notna(reported_inv) and reported_inv > 0:
        investments = _resolve(cfg.investments, reported_inv, 0.0)
    else:
        investments = _resolve(cfg.investments, float("nan"), 0.0)

    # Non-operating investments stay config-driven on purpose, and this warns rather
    # than adding them. Whether a long-term securities portfolio is a claim available
    # to the common or capital tied up in the business is a judgement about that
    # company -- Apple reports $77.7bn, worth +4.4% on value per share, Microsoft
    # +2.6%. Moving a headline number that far on a field-mapping decision is exactly
    # what this model is supposed to put in front of the analyst rather than do quietly.
    if (
        cfg.investments is None
        and not cfg.include_investments
        and pd.notna(reported_inv)
        and reported_inv > 0
        and enterprise_value > 0
    ):
        share = reported_inv / enterprise_value
        if share > 0.01:
                warnings.warn(
                    f"Balance sheet reports {reported_inv:,.0f} of long-term investments "
                    f"({share:.1%} of enterprise value) that the bridge does not count. "
                    f"If they are non-operating they belong in equity value: set "
                    f"bridge.investments or pass --include-investments to include them.",
                    UserWarning,
                    stacklevel=2,
                )

    equity_value = enterprise_value - total_debt + cash - minority - preferred + investments

    if not shares or shares <= 0 or pd.isna(shares):
        raise ValueError("a positive share count is required to compute value per share")

    info = info_dict(financials)
    if current_price is None:
        raw = info.get("currentPrice")
        current_price = float(raw) if raw else None

    return BridgeResult(
        enterprise_value=enterprise_value,
        total_debt=total_debt,
        cash=cash,
        minority_interest=minority,
        preferred_equity=preferred,
        investments=investments,
        equity_value=equity_value,
        shares=float(shares),
        value_per_share=equity_value / float(shares),
        current_price=current_price,
    )


def _market_implied_share_count(info: dict[str, Any]) -> float | None:
    """Total shares across every class, from market capitalisation and price.

    The one figure that must cover the whole company, because market capitalisation
    does. Returns None when either input is missing rather than guessing.
    """
    market_cap = info.get("marketCap")
    price = info.get("currentPrice")
    if not market_cap or not price:
        return None
    try:
        total = float(market_cap) / float(price)
    except (TypeError, ZeroDivisionError):
        return None
    return total if total > 0 else None


def base_share_count(financials: Any) -> float:
    """Shares outstanding today, which is the denominator a per-share value needs.

    Order matters, and the previous order was wrong. "Diluted Average Shares" is a
    weighted average across the fiscal year -- a backward-looking figure that is stale
    the moment the year closes. Dividing a present-value equity number by last year's
    average overstates value per share for any company that has issued stock since
    (Tesla: 3.53bn average against 3.95bn actual, a 12% overstatement) and understates
    it for one that has bought stock back.

    So: current shares outstanding first, then the period-end balance-sheet count,
    and the weighted average only as a last resort. Dilution from options and future
    grants is layered on separately via `option_overhang_shares` or the dilute method.
    """
    info = info_dict(financials)
    implied = info.get("impliedSharesOutstanding")
    basic = info.get("sharesOutstanding")

    # Dual-class companies break `sharesOutstanding`.
    #
    # Yahoo reports it for the listed class only, while market capitalisation covers
    # every class -- and the equity value being divided here is the whole company's,
    # so the denominator has to be every class too. Dividing by one class overstates
    # value per share by the ratio of the classes:
    #
    #   GOOGL  5.867bn reported against 12.230bn total  ->  2.08x overstatement
    #   NKE    1.202bn against 1.483bn (Class A + B)    ->  $44.05 not $35.70,
    #                                                        turning -7.9% into +13.6%
    #   META   2.205bn against 2.548bn
    #
    # `marketCap / price` recovers the total, and the balance-sheet "Ordinary Shares
    # Number" agrees with it on all three. Only trust `sharesOutstanding` when it is
    # consistent with the market's own arithmetic.
    market_total = _market_implied_share_count(info)
    for candidate in (implied, basic):
        if candidate and pd.notna(candidate) and float(candidate) > 0:
            count = float(candidate)
            if market_total is None or abs(count / market_total - 1.0) <= 0.02:
                return count
            ordinary = field_value(financials, "ordinary_shares")
            chosen = (
                float(ordinary)
                if pd.notna(ordinary)
                and ordinary > 0
                and abs(float(ordinary) / market_total - 1.0) <= 0.02
                else market_total
            )
            warnings.warn(
                f"Reported share count ({count:,.0f}) disagrees with market "
                f"capitalisation divided by price ({market_total:,.0f}) by "
                f"{abs(count / market_total - 1.0):.1%}. For a dual-class company the "
                f"reported figure covers only the listed class while equity value "
                f"covers every class, so {chosen:,.0f} is used instead. Set the count "
                f"explicitly if that is wrong.",
                UserWarning,
                stacklevel=2,
            )
            return chosen

    ordinary = field_value(financials, "ordinary_shares")
    if pd.notna(ordinary) and ordinary > 0:
        return float(ordinary)

    diluted = field_value(financials, "diluted_shares")
    if pd.notna(diluted) and diluted > 0:
        warnings.warn(
            "No current share count available; falling back to the weighted-average "
            "diluted share count, which is backward-looking and will misstate value "
            "per share if the count has moved since the last fiscal year end.",
            UserWarning,
            stacklevel=2,
        )
        return float(diluted)

    raise DataQualityError(
        "no share count available: the financials carry neither 'Ordinary Shares Number' "
        "nor 'Diluted Average Shares', and the info payload has no sharesOutstanding. "
        "A per-share valuation is impossible without a denominator."
    )


def _minority_from_balance(financials: Any) -> float:
    """Minority interest as total equity including minorities less common equity."""
    total = field_value(financials, "total_equity_incl_minority")
    common = field_value(financials, "stockholders_equity")
    if pd.isna(total) or pd.isna(common):
        return float("nan")
    diff = total - common
    return diff if diff > 0 else 0.0


def _resolve(override: float | None, derived: float, default: float) -> float:
    if override is not None:
        return float(override)
    if pd.notna(derived):
        return float(derived)
    return float(default)
