"""Tests for the Reverse DCF / Expectations Investing solver."""

from __future__ import annotations

import pytest

from src.dcf.engine import DCFEngine
from src.dcf.reverse_dcf import solve_reverse_dcf
from src.fetcher.yfinance_client import YFinanceClient
from src.models.assumptions import DCFAssumptions


@pytest.fixture
def aapl_financials():
    return YFinanceClient("AAPL", offline_mode=True).get_financials()


def test_reverse_dcf_round_trip(aapl_financials):
    assumptions = DCFAssumptions()
    base_res = DCFEngine(aapl_financials, assumptions, ticker="AAPL").run()
    target_price = base_res.value_per_share

    # When target price equals base DCF value, implied assumptions should match inputs!
    rev = solve_reverse_dcf(
        aapl_financials,
        assumptions,
        comps_terminal=None,
        ticker="AAPL",
        target_price=target_price,
    )

    assert rev.current_price == target_price
    # Perpetuity growth should match base assumption (2.5%)
    assert rev.implied_perpetuity_growth == pytest.approx(
        assumptions.terminal.perpetuity_growth, rel=0.01
    )

    # Implied revenue CAGR should match base input (5.0%)
    assert rev.implied_revenue_growth_cagr == pytest.approx(0.05, abs=0.005)


def test_reverse_dcf_with_market_price(aapl_financials):
    assumptions = DCFAssumptions()
    market_price = float(aapl_financials.info.get("currentPrice", 300.0))

    rev = solve_reverse_dcf(
        aapl_financials,
        assumptions,
        comps_terminal=None,
        ticker="AAPL",
        target_price=market_price,
    )

    assert rev.current_price == market_price
    assert rev.wacc > 0
    # Higher price should imply higher revenue growth or margin
    assert rev.implied_revenue_growth_cagr is not None or "200%" in rev.implied_revenue_status


def test_reverse_dcf_handles_missing_price(aapl_financials):
    assumptions = DCFAssumptions()
    rev = solve_reverse_dcf(
        aapl_financials,
        assumptions,
        comps_terminal=None,
        ticker="AAPL",
        target_price=0.0,
    )
    assert len(rev.warnings) > 0
    assert "No valid positive market price" in rev.warnings[0]
