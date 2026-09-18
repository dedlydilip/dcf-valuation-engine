"""The live risk-free-rate fetch: parsing, error paths, and the CLI wiring around it.

Mirrors how `src/fetcher/fx.py` would be tested if it were -- a fake `yfinance.Ticker`
stands in so nothing here touches the network, yet every branch of `fetch_risk_free_rate`
runs for real.
"""

from __future__ import annotations

import sys
import types

import pandas as pd
import pytest
from click.testing import CliRunner

from src.cli import cli
from src.models.errors import ValuationError


def _fake_yfinance(closes: list[float] | None, *, raises: Exception | None = None):
    """A stand-in `yfinance` module whose `Ticker(...).history(...)` is scripted."""

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, period: str = "5d") -> pd.DataFrame:
            if raises is not None:
                raise raises
            if closes is None:
                return pd.DataFrame()
            return pd.DataFrame({"Close": closes})

    module = types.SimpleNamespace(Ticker=_FakeTicker)
    return module


@pytest.fixture
def patched_yfinance(monkeypatch):
    """Installs a fake `yfinance` module and returns a setter for its behaviour."""

    def _install(closes: list[float] | None = None, *, raises: Exception | None = None):
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yfinance(closes, raises=raises))

    return _install


class TestFetchRiskFreeRate:
    def test_parses_the_close_as_a_percentage(self, patched_yfinance):
        from src.fetcher.rates import fetch_risk_free_rate

        patched_yfinance([4.90, 4.92, 4.947])
        assert fetch_risk_free_rate() == pytest.approx(0.04947)

    def test_uses_the_most_recent_close(self, patched_yfinance):
        """A 5-day window can carry stale rows; the last one is today's."""
        from src.fetcher.rates import fetch_risk_free_rate

        patched_yfinance([5.10, 5.05, 5.00, 4.98, 4.947])
        assert fetch_risk_free_rate() == pytest.approx(0.04947)

    def test_empty_history_raises_with_actionable_guidance(self, patched_yfinance):
        from src.fetcher.rates import fetch_risk_free_rate

        patched_yfinance([])
        with pytest.raises(ValuationError, match="--risk-free"):
            fetch_risk_free_rate()

    def test_network_failure_raises_with_actionable_guidance(self, patched_yfinance):
        from src.fetcher.rates import fetch_risk_free_rate

        patched_yfinance(None, raises=ConnectionError("no route to host"))
        with pytest.raises(ValuationError, match="--risk-free"):
            fetch_risk_free_rate()

    def test_an_implausible_rate_is_refused_rather_than_trusted(self, patched_yfinance):
        """A quote of 495 (not 4.95) would silently produce a 495% discount rate."""
        from src.fetcher.rates import fetch_risk_free_rate

        patched_yfinance([495.0])
        with pytest.raises(ValuationError, match="plausible range"):
            fetch_risk_free_rate()

    def test_a_negative_quote_is_refused(self, patched_yfinance):
        from src.fetcher.rates import fetch_risk_free_rate

        patched_yfinance([-0.5])
        with pytest.raises(ValuationError, match="plausible range"):
            fetch_risk_free_rate()


class TestCLIRiskFreeFlags:
    """The flags are opt-in -- the pinned config value is untouched unless asked."""

    def _run(self, *args):
        return CliRunner().invoke(cli, list(args))

    def test_explicit_override_changes_the_result(self):
        low = self._run(
            "value", "-t", "AAPL", "--use-offline", "--no-excel", "--no-monte-carlo",
            "--risk-free", "0.02",
        )
        high = self._run(
            "value", "-t", "AAPL", "--use-offline", "--no-excel", "--no-monte-carlo",
            "--risk-free", "0.08",
        )
        assert low.exit_code == 0 and high.exit_code == 0
        assert "Implied value per share" in low.output
        # A higher risk-free rate raises WACC, which lowers the discounted value.
        assert "8.00%" not in low.output  # sanity: the flag actually changed something
        low_wacc = float(low.output.split("WACC")[1].split("%")[0].strip())
        high_wacc = float(high.output.split("WACC")[1].split("%")[0].strip())
        assert high_wacc > low_wacc

    def test_auto_risk_free_refuses_alongside_use_offline(self):
        result = self._run(
            "value", "-t", "AAPL", "--use-offline", "--auto-risk-free",
            "--no-excel", "--no-monte-carlo",
        )
        assert result.exit_code != 0
        assert "cannot fetch the risk-free rate" in result.output

    def test_supplying_both_flags_is_a_contradiction(self):
        result = self._run(
            "value", "-t", "AAPL", "--use-offline", "--risk-free", "0.05",
            "--auto-risk-free", "--no-excel", "--no-monte-carlo",
        )
        assert result.exit_code != 0
        assert "both set" in result.output

    def test_auto_risk_free_resolves_to_the_fetched_rate(self, monkeypatch):
        """Unit-level, not end-to-end: --auto-risk-free needs network for the fetch, and
        online mode would also fetch AAPL's statements for real. Patching the imported
        name isolates the flag's wiring from that unrelated network dependency."""
        import src.cli as cli_module

        monkeypatch.setattr(cli_module, "fetch_risk_free_rate", lambda: 0.04947)
        override = cli_module._risk_free_override(None, auto_risk_free=True, use_offline=False)
        assert override == {"wacc": {"risk_free_rate": 0.04947}}

    def test_neither_flag_uses_the_pinned_config_value(self):
        """No flags at all -- the config's pinned rate drives the WACC line unchanged."""
        result = self._run(
            "value", "-t", "AAPL", "--use-offline", "--no-excel", "--no-monte-carlo",
        )
        assert result.exit_code == 0
        assert "Fetched risk-free rate" not in result.output
