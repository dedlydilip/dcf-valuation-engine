"""Shared fixtures.

Synthetic companies are built with round numbers so expected results can be worked
out by hand, which is the point: a test whose expected value came out of the code it
is testing proves nothing.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.models.assumptions import DCFAssumptions
from src.models.financials import Financials

PERIODS = [pd.Timestamp("2024-12-31"), pd.Timestamp("2025-12-31")]


def make_financials(
    ticker: str = "TEST",
    periods: list | None = None,
    info: dict | None = None,
    **fields: list[float] | float,
) -> Financials:
    """Build a Financials from canonical field names.

    Scalars are broadcast across every period; lists are used as given.
    """
    periods = periods or PERIODS
    rows: dict[str, list[float]] = {}
    for name, value in fields.items():
        if isinstance(value, (list, tuple)):
            rows[name] = [float(v) for v in value]
        else:
            rows[name] = [float(value)] * len(periods)
    statements = pd.DataFrame(rows, index=periods).T.astype("float64")
    return Financials(ticker=ticker, statements=statements, info=info or {})


@pytest.fixture
def golden_financials() -> Financials:
    """A company chosen so the whole DCF resolves to exact decimals.

    Revenue 1,000 growing 10%, EBIT margin 20%, tax 25%, D&A and capex both 5% of
    revenue so they cancel, zero working capital, zero debt, zero cash, 100 shares.
    Every discounted cash flow lands on exactly 150.
    """
    return make_financials(
        ticker="GOLD",
        revenue=1000.0,
        ebit=200.0,
        ebitda=250.0,
        da=50.0,
        capex=50.0,
        sbc=0.0,
        cfo=250.0,
        total_debt=0.0,
        cash=0.0,
        short_term_investments=0.0,
        current_assets=100.0,
        current_liabilities=100.0,
        diluted_shares=100.0,
        net_income=150.0,
        pretax_income=200.0,
        tax_provision=50.0,
        info={"sharesOutstanding": 100, "currentPrice": 20.0, "marketCap": 2000.0},
    )


@pytest.fixture
def golden_assumptions() -> DCFAssumptions:
    """Assumptions pinned so the golden case has one exact answer."""
    return DCFAssumptions.model_validate(
        {
            "projection": {
                "years": 3,
                "revenue_growth": 0.10,
                "ebit_margin": 0.20,
                "tax_rate": 0.25,
                "da_pct_revenue": 0.05,
                "capex_pct_revenue": 0.05,
                "nwc_pct_revenue": 0.0,
                "mid_year_convention": False,
            },
            "wacc": {
                "risk_free_rate": 0.04,
                "equity_risk_premium": 0.05,
                "beta_override": 1.2,
            },
            "sbc": {"method": "expense", "sbc_pct_revenue": 0.0},
            "terminal": {
                "method": "gordon",
                "perpetuity_growth": 0.02,
                "exit_multiple_mode": "static",
                "static_exit_multiple": 12.0,
                "mature_industry_multiple": 10.0,
            },
        }
    )


@pytest.fixture
def tech_financials() -> Financials:
    """A profitable company with material stock compensation."""
    return make_financials(
        ticker="TECH",
        revenue=[900.0, 1000.0],
        ebit=[180.0, 200.0],
        ebitda=[230.0, 250.0],
        da=[50.0, 50.0],
        capex=[45.0, 50.0],
        sbc=[90.0, 100.0],
        cfo=[240.0, 250.0],
        sga=[200.0, 220.0],
        total_debt=[100.0, 100.0],
        cash=[200.0, 200.0],
        short_term_investments=[0.0, 0.0],
        current_assets=[400.0, 400.0],
        current_liabilities=[200.0, 200.0],
        current_debt=[0.0, 0.0],
        diluted_shares=[100.0, 100.0],
        net_income=[140.0, 150.0],
        pretax_income=[180.0, 200.0],
        tax_provision=[40.0, 50.0],
        interest_expense=[5.0, 5.0],
        info={"beta": 1.1, "sharesOutstanding": 100, "currentPrice": 30.0, "marketCap": 3000.0},
    )


@pytest.fixture
def base_assumptions() -> DCFAssumptions:
    return DCFAssumptions.from_yaml()
