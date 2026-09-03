"""Share-count forecasting under the dilute treatment of SBC.

Only used when `sbc.method == "dilute"`. Under the expense treatment SBC is already
charged against cash flow, so growing the share count as well would double-count --
see SBCAssumptions for the guard that enforces this.

The circularity: shares issued depend on the price they are issued at, which depends
on equity value per share, which depends on the share count. This class is pure --
it takes a price and returns a share path. Resolving the fixed point is the engine's
job (`src/dcf/engine.py`), which keeps the iteration in one place instead of hidden
behind a property.

Issuance price grows at the cost of equity rather than staying flat. Employees
receiving stock in year 5 receive it at year-5 prices, and holding the price flat
would overstate the share count and understate the value.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.models.assumptions import SBCAssumptions


@dataclass
class DilutionPath:
    shares: list[float]
    new_shares: list[float]
    issue_prices: list[float]

    @property
    def ending_shares(self) -> float:
        return self.shares[-1]

    def to_frame(self, years: list[int] | None = None) -> pd.DataFrame:
        idx = years or list(range(1, len(self.shares) + 1))
        return pd.DataFrame(
            {
                "new_shares_issued": self.new_shares,
                "issue_price": self.issue_prices,
                "ending_shares": self.shares,
            },
            index=idx,
        )


class DilutionTracker:
    """Forecast diluted share count from SBC dollars and an issuance price path."""

    def __init__(self, current_shares: float, sbc_assumptions: SBCAssumptions) -> None:
        if current_shares is None or current_shares <= 0:
            raise ValueError("current_shares must be positive to forecast dilution")
        self.current_shares = float(current_shares)
        self.sbc = sbc_assumptions

    def forecast(
        self,
        sbc_dollars: list[float],
        base_price: float,
        price_growth: float = 0.0,
    ) -> DilutionPath:
        """Grow the share count by the stock issued to employees each year.

        `buyback_offset_pct` models a company that repurchases part of its issuance
        to hold the count flat -- real cash spent, so the offset belongs in the
        cash-flow bridge too, not only here.
        """
        if base_price is None or base_price <= 0:
            raise ValueError("base_price must be positive to convert SBC dollars into shares")

        shares = self.current_shares
        out_shares: list[float] = []
        out_new: list[float] = []
        out_price: list[float] = []

        net_issuance = 1.0 - self.sbc.buyback_offset_pct
        for year, sbc in enumerate(sbc_dollars, start=1):
            price = base_price * ((1.0 + price_growth) ** year)
            issued = 0.0 if (sbc is None or pd.isna(sbc) or sbc <= 0) else (sbc * net_issuance) / price
            shares += issued
            out_shares.append(shares)
            out_new.append(issued)
            out_price.append(price)

        return DilutionPath(shares=out_shares, new_shares=out_new, issue_prices=out_price)


def expense_method_share_count(
    current_diluted_shares: float, sbc_assumptions: SBCAssumptions
) -> float:
    """Share count under the expense treatment.

    The count is not literally static: options and RSUs already granted will vest
    regardless of anything the model assumes about the future. That existing overhang
    is added via the treasury-stock method when supplied. What does NOT get added is
    future grants -- those are already paid for in the cash flow.
    """
    base = float(current_diluted_shares)
    overhang = sbc_assumptions.option_overhang_shares
    if overhang:
        base += float(overhang)
    return base
