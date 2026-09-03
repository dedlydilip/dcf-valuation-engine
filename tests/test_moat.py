"""Tests for the Economic Moat and Capital Efficiency analyzer."""

from __future__ import annotations

import pytest

from src.dcf.engine import DCFEngine
from src.dcf.moat import _resolve_invested_capital, analyze_moat
from src.fetcher.yfinance_client import YFinanceClient
from src.models.assumptions import DCFAssumptions


@pytest.fixture
def aapl_financials():
    return YFinanceClient("AAPL", offline_mode=True).get_financials()


@pytest.fixture
def msft_financials():
    return YFinanceClient("MSFT", offline_mode=True).get_financials()


@pytest.fixture
def tsla_financials():
    return YFinanceClient("TSLA", offline_mode=True).get_financials()


def test_resolve_invested_capital_offline_fixtures(
    aapl_financials, msft_financials, tsla_financials
):
    for f in (aapl_financials, msft_financials, tsla_financials):
        ic = _resolve_invested_capital(f)
        assert ic > 0, f"Invested capital should be strictly positive for {f.ticker}"


def test_moat_analysis_aapl(aapl_financials):
    assumptions = DCFAssumptions()
    result = DCFEngine(aapl_financials, assumptions, ticker="AAPL").run()
    moat = analyze_moat(aapl_financials, result)

    assert moat.invested_capital_base > 0
    assert moat.nopat_base > 0
    assert moat.roic_base > 0
    assert len(moat.projected_roics) == assumptions.projection.years
    assert len(moat.projected_spreads) == assumptions.projection.years
    assert moat.moat_rating in [
        "Wide Moat (Exceptional Capital Efficiency)",
        "Narrow Moat (Value-Accretive)",
        "Neutral / Cost of Capital Returns",
        "Value-Destructive (Negative Spread)",
    ]


def test_moat_analysis_value_destructive_trigger(aapl_financials):
    # Set high discount rate parameters within Pydantic bounds so WACC > ROIC
    assumptions = DCFAssumptions()
    assumptions.wacc.risk_free_rate = 0.20
    assumptions.wacc.equity_risk_premium = 0.20
    assumptions.wacc.beta_override = 5.0
    result = DCFEngine(aapl_financials, assumptions, ticker="AAPL").run()
    moat = analyze_moat(aapl_financials, result)

    assert not moat.is_value_accretive
    assert "Value-Destructive" in moat.moat_rating
    assert any("WARNING: Negative economic spread" in d for d in moat.diagnostics)


# ---------------------------------------------------------------------------
# Added after mutation testing: the tests above assert shape, not value.
#
# Pinning `roic_base = 0.15` as a constant in `analyze_moat` left all three of
# them green. `assert moat.roic_base > 0` passes for any positive number, and
# `assert moat.moat_rating in [...]` passes for every member of the enum, so
# nothing in the file actually checked the ROIC arithmetic.
#
# The arithmetic turned out to be correct -- re-derived independently below,
# matching to 1e-9 on all three fixtures -- but it was correct untested.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticker,expected_roic",
    [("AAPL", 0.609719), ("MSFT", 0.254075), ("TSLA", 0.042427)],
)
def test_roic_is_pinned_not_merely_positive(ticker, expected_roic):
    """Kills the constant-ROIC mutant that the original assertions survived."""
    financials = YFinanceClient(ticker, offline_mode=True).get_financials()
    assumptions = DCFAssumptions.from_yaml()
    result = DCFEngine(financials, assumptions, ticker=ticker).run()
    moat = analyze_moat(financials, result)

    assert moat.roic_base == pytest.approx(expected_roic, abs=1e-5), (
        f"{ticker} ROIC is now {moat.roic_base:.6f}, not the pinned {expected_roic:.6f}"
    )


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "TSLA"])
def test_roic_equals_nopat_over_invested_capital(ticker):
    """Re-derive the definition independently rather than trusting the module.

    ROIC = EBIT x (1 - tax) / invested capital. If the implementation ever stops
    computing that, this fails even if the number still looks plausible.
    """
    from src.models.financials import field_value

    financials = YFinanceClient(ticker, offline_mode=True).get_financials()
    assumptions = DCFAssumptions.from_yaml()
    result = DCFEngine(financials, assumptions, ticker=ticker).run()
    moat = analyze_moat(financials, result)

    nopat = field_value(financials, "ebit") * (1.0 - assumptions.projection.tax_rate)
    invested_capital = _resolve_invested_capital(financials)

    assert moat.nopat_base == pytest.approx(nopat, rel=1e-9)
    assert moat.invested_capital_base == pytest.approx(invested_capital, rel=1e-9)
    assert moat.roic_base == pytest.approx(nopat / invested_capital, rel=1e-9)


def test_economic_spread_is_roic_less_wacc():
    """The spread drives the moat rating, so it cannot be left unchecked either."""
    financials = YFinanceClient("TSLA", offline_mode=True).get_financials()
    result = DCFEngine(financials, DCFAssumptions.from_yaml(), ticker="TSLA").run()
    moat = analyze_moat(financials, result)

    assert moat.economic_spread == pytest.approx(moat.roic_base - moat.wacc, rel=1e-9)
    # Tesla earns 4.9% on capital against a 14.1% WACC -- genuinely value-destructive,
    # which is what makes it the right fixture for the negative-spread path.
    assert moat.economic_spread < 0
    assert not moat.is_value_accretive
