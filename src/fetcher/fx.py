"""Currency conversion for companies that report and trade in different units.

Yahoo gives the income statement and balance sheet in the company's reporting currency
and the price, market capitalisation and share count in the listing currency. For any
depositary receipt these differ, and nothing downstream reconciles them:

    TM   JPY statements against a USD quote  ->  $79,467 per share against $198
    SAP  EUR statements against a USD quote  ->  $176.90 against $210

Toyota is obviously wrong. SAP is the dangerous one -- at a EUR/USD rate near 1.16 the
number looks entirely reasonable. The WACC is corrupted the same way, weighing a USD
market capitalisation against JPY debt, which is how Toyota returned a 0.43% discount
rate.

Converting the statements into the price currency fixes both, and it also makes the
discount rate coherent: the configured risk-free rate and equity risk premium are USD
assumptions, and they belong against USD cash flows.

The rate is spot, and applying it to every forecast year holds it flat forever. That is
the ordinary practitioner shortcut rather than a theoretically complete answer --
interest-rate parity says a currency pair with a wide rate differential has a forward
curve that is anything but flat. It is documented as an assumption rather than presented
as a solution, and `fx_rate` is there for anyone who would rather supply their own.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.models.errors import ValuationError
from src.models.financials import FIELD_MAP

# Canonical fields that are counts, not money. Everything else in FIELD_MAP is a
# currency amount and gets scaled. Scaling a share count by an FX rate would be a
# spectacular error -- Toyota's count would move by a factor of 159 -- so these are
# named explicitly rather than inferred, and pinned by a test.
NON_MONETARY_FIELDS: frozenset[str] = frozenset({"diluted_shares", "ordinary_shares"})

MONETARY_FIELDS: frozenset[str] = frozenset(FIELD_MAP) - NON_MONETARY_FIELDS


def fetch_fx_rate(from_currency: str, to_currency: str) -> float:
    """Spot rate: one unit of `from_currency` in units of `to_currency`.

    JPY -> USD returns about 0.0063. Yahoo quotes these as `JPYUSD=X`.

    `yfinance` is imported inside the function, exactly as the live statement fetch
    does, so that importing this module never pulls in a network client. `--use-offline`
    must remain provably network-free and there is a test asserting it.
    """
    source = (from_currency or "").upper()
    target = (to_currency or "").upper()
    if not source or not target:
        raise ValuationError(
            f"cannot fetch an exchange rate without both currencies "
            f"(got {source or '?'} -> {target or '?'})"
        )
    if source == target:
        return 1.0

    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ValuationError(
            "yfinance is not installed, so the exchange rate cannot be fetched. "
            "Supply one with --fx-rate instead."
        ) from exc

    pair = f"{source}{target}=X"
    try:
        history = yf.Ticker(pair).history(period="5d")
    except Exception as exc:
        raise ValuationError(
            f"could not fetch the {source}/{target} exchange rate ({pair}): {exc}. "
            f"Supply one with --fx-rate instead."
        ) from exc

    if history is None or history.empty or "Close" not in history:
        raise ValuationError(
            f"Yahoo returned no data for the {source}/{target} exchange rate ({pair}). "
            f"Supply one with --fx-rate instead."
        )

    rate = float(history["Close"].dropna().iloc[-1])
    if not rate > 0:
        raise ValuationError(
            f"the {source}/{target} exchange rate came back as {rate}, which cannot be "
            f"used. Supply one with --fx-rate instead."
        )
    return rate


def convert_statements(statements: pd.DataFrame, rate: float) -> pd.DataFrame:
    """Scale every monetary row by `rate`, leaving share counts alone.

    Operates on the canonical frame, so it runs after normalisation and knows exactly
    which rows are money. Rows outside FIELD_MAP are left untouched rather than guessed
    at.
    """
    if rate == 1.0:
        return statements
    converted = statements.copy()
    for name in converted.index:
        if name in MONETARY_FIELDS:
            converted.loc[name] = pd.to_numeric(converted.loc[name], errors="coerce") * rate
    return converted


def convert_info(info: dict[str, Any], rate: float) -> dict[str, Any]:
    """Statement-derived figures in Yahoo's info payload, scaled to match.

    Only the ones sourced from the filings. `marketCap`, `currentPrice` and
    `sharesOutstanding` are already in the price currency and must not be touched --
    that is the whole point of converting towards them rather than away.
    """
    if rate == 1.0:
        return info
    out = dict(info)
    for key in ("totalDebt", "totalCash", "enterpriseValue"):
        value = out.get(key)
        if value:
            out[key] = float(value) * rate
    return out
