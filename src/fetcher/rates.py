"""The risk-free rate: CAPM's other input that moves, fetched live.

`config/assumptions.yaml` pins a risk-free rate the way it pins everything else --
as a dated, reproducible input. The problem is that this one number tracks a real
market that moves every trading day, and nothing warned when the pin went stale.
75 basis points of drift between the pinned 4.20% and the actual 10-year Treasury
yield moved AAPL's implied value by 8.9% -- a bigger swing than most of the
per-share deltas this model treats as a genuine finding.

`--auto-risk-free` fetches the current yield instead of trusting the pin, following
the same shape as `fetch_fx_rate` in `src/fetcher/fx.py`: opt-in, not the default,
because a valuation has to reproduce exactly when someone re-runs it later, and a
silently time-varying default would break that. `--risk-free` supplies one directly,
same as `--fx-rate` does for currency.
"""

from __future__ import annotations

from src.models.errors import ValuationError

# CBOE 10-Year Treasury Note Yield Index. Yahoo quotes this directly in percentage
# points (4.95 means 4.95%, not 495 basis points and not a price to back a yield out
# of), so the only conversion needed is dividing by 100.
TEN_YEAR_TREASURY_PROXY = "^TNX"


def fetch_risk_free_rate(proxy: str = TEN_YEAR_TREASURY_PROXY) -> float:
    """Current risk-free rate as a decimal, e.g. 0.0495 for a 4.95% 10-year yield.

    `yfinance` is imported inside the function, exactly as `fetch_fx_rate` does, so
    importing this module never pulls in a network client -- `--use-offline` must stay
    provably network-free.
    """
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ValuationError(
            "yfinance is not installed, so the risk-free rate cannot be fetched. "
            "Supply one with --risk-free instead."
        ) from exc

    try:
        history = yf.Ticker(proxy).history(period="5d")
    except Exception as exc:
        raise ValuationError(
            f"could not fetch the risk-free rate ({proxy}): {exc}. "
            f"Supply one with --risk-free instead."
        ) from exc

    if history is None or history.empty or "Close" not in history:
        raise ValuationError(
            f"Yahoo returned no data for the risk-free rate proxy ({proxy}). "
            f"Supply one with --risk-free instead."
        )

    quote = float(history["Close"].dropna().iloc[-1])
    rate = quote / 100.0
    if not 0.0 < rate < 0.25:
        raise ValuationError(
            f"the fetched risk-free rate came back as {rate:.4%}, which is outside "
            f"the plausible range and cannot be used. Supply one with --risk-free instead."
        )
    return rate
