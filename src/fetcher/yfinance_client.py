"""Financial data access with a committed offline fallback.

Yahoo Finance is an unversioned, unofficial endpoint: it renames rows, rate-limits,
and occasionally returns empty frames for a ticker that worked yesterday. A model
that only runs when Yahoo cooperates is a model nobody can evaluate.

So the client exposes one interface over two backends. Live mode hits yfinance.
Offline mode reads JSON fixtures committed to the repository, which makes the whole
valuation reproducible with no network at all -- and keeps CI green regardless of
what Yahoo does.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import pandas as pd

from src.fetcher.fx import convert_info, convert_statements, fetch_fx_rate
from src.fetcher.normalizer import FinancialNormalizer
from src.models.assumptions import CurrencyAssumptions
from src.models.errors import OfflineDataMissingError, ValuationError
from src.models.financials import INFO_KEYS, Financials
from src.paths import resolve_path

STATEMENT_FILES = {
    "income": "income_stmt.json",
    "balance": "balance_sheet.json",
    "cashflow": "cashflow.json",
}
INFO_FILE = "info.json"
PRICES_FILE = "prices.csv"

DEFAULT_OFFLINE_DIR = Path("data/offline_sample")


class YFinanceClient:
    """Fetch one company's financials, live or from committed fixtures."""

    def __init__(
        self,
        ticker: str,
        offline_mode: bool = False,
        offline_path: Path | str | None = None,
        price_period: str = "2y",
    ) -> None:
        self.ticker = ticker.upper().strip()
        self.offline_mode = offline_mode
        # Resolve whatever we are handed, not just the default. The CLI passes an
        # explicit relative string built from --offline-dir, so it never reaches the
        # DEFAULT_OFFLINE_DIR branch -- fixing only the constant would leave
        # `run.py value --use-offline` broken outside the repo root while a test that
        # constructs this class directly went green.
        raw = Path(offline_path) if offline_path else DEFAULT_OFFLINE_DIR / self.ticker
        self.offline_path = resolve_path(raw)
        self.price_period = price_period

    # ------------------------------------------------------------------ public

    def get_financials(self, currency: CurrencyAssumptions | None = None) -> Financials:
        """Return canonical financials from whichever backend is configured.

        `currency` decides what happens when the statements and the quote are in
        different units, as they are for any depositary receipt. Left unset, nothing
        is converted and the quality gate refuses -- which is the point, because the
        alternative is a valuation that silently mixes JPY cash flows with a USD price.
        """
        raw = self._load_offline_raw() if self.offline_mode else self._fetch_live_raw()

        statements = FinancialNormalizer.canonicalize(
            {
                "income": raw["income"],
                "balance": raw["balance"],
                "cashflow": raw["cashflow"],
            }
        )
        info = raw.get("info") or {}
        statement_ccy = str(info.get("financialCurrency") or "USD").upper()
        price_ccy = str(info.get("currency") or "").upper()

        rate = self._resolve_fx_rate(currency, statement_ccy, price_ccy)
        if rate is not None:
            statements = convert_statements(statements, rate)
            info = convert_info(info, rate)
            # The statements are now denominated in the price currency, so say so.
            # Everything downstream reads this, including the coherence check in the
            # quality gate, which then passes because the units genuinely do agree.
            info["financialCurrency"] = price_ccy
            statement_ccy = price_ccy

        return Financials(
            ticker=self.ticker,
            statements=statements,
            info=info,
            prices=raw.get("prices"),
            currency=statement_ccy,
            source="offline_sample" if self.offline_mode else "yfinance",
            fx_rate_applied=rate,
            original_currency=price_ccy if rate is not None else None,
        )

    def _resolve_fx_rate(
        self,
        currency: CurrencyAssumptions | None,
        statement_ccy: str,
        price_ccy: str,
    ) -> float | None:
        """The rate to convert statements into the price currency, or None.

        None means "do not convert" -- either nothing was asked for, or there is
        nothing to convert because the two currencies already agree.
        """
        if currency is None or not currency.converts:
            return None
        if not statement_ccy or not price_ccy or statement_ccy == price_ccy:
            return None

        rate = (
            float(currency.fx_rate)
            if currency.fx_rate is not None
            else fetch_fx_rate(statement_ccy, price_ccy)
        )
        source = "supplied" if currency.fx_rate is not None else "spot from Yahoo"
        warnings.warn(
            f"Converting {self.ticker} statements from {statement_ccy} to {price_ccy} at "
            f"{rate:.6g} ({source}), and holding that rate flat across every forecast "
            f"year. Interest-rate parity says a pair with a wide rate differential has a "
            f"forward curve that is not flat, so treat this as the standard shortcut it "
            f"is. The cash flows and the share price now agree, which is what makes the "
            f"valuation meaningful at all.",
            UserWarning,
            stacklevel=3,
        )
        warnings.warn(
            f"Converting amounts does not convert rates. The cost of equity is built "
            f"from the configured risk-free rate and equity risk premium, which are "
            f"{price_ccy} assumptions, while the cost of debt is this company's actual "
            f"{statement_ccy} borrowing rate -- for Toyota that is 0.50% against a 4.20% "
            f"US risk-free rate, inside one WACC. Re-basing the risk-free rate to "
            f"{statement_ccy}, or grossing the cost of debt up to a {price_ccy} "
            f"equivalent, is a judgement this model will not make for you.",
            UserWarning,
            stacklevel=3,
        )
        return rate

    def available_offline(self) -> bool:
        return (self.offline_path / STATEMENT_FILES["income"]).exists()

    # ------------------------------------------------------------------ offline

    def _load_offline_raw(self) -> dict[str, Any]:
        if not self.available_offline():
            available = sorted(
                p.name for p in self.offline_path.parent.glob("*") if p.is_dir()
            ) if self.offline_path.parent.exists() else []
            raise OfflineDataMissingError(
                f"no offline fixture for {self.ticker} at {self.offline_path}. "
                f"Available: {available or 'none'}. "
                f"Create one with: python run.py snapshot --ticker {self.ticker}"
            )

        raw: dict[str, Any] = {}
        for key, filename in STATEMENT_FILES.items():
            raw[key] = _read_statement_json(self.offline_path / filename)

        info_path = self.offline_path / INFO_FILE
        raw["info"] = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}

        prices_path = self.offline_path / PRICES_FILE
        if prices_path.exists():
            raw["prices"] = pd.read_csv(prices_path, parse_dates=["Date"], index_col="Date")
        else:
            raw["prices"] = None
        return raw

    # --------------------------------------------------------------------- live

    def _fetch_live_raw(self) -> dict[str, Any]:
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ValuationError(
                "yfinance is not installed. Install it, or run with --use-offline."
            ) from exc

        try:
            handle = yf.Ticker(self.ticker)
            income = handle.income_stmt
            balance = handle.balance_sheet
            cashflow = handle.cashflow
            info = dict(handle.info or {})
            prices = handle.history(period=self.price_period)
        except Exception as exc:
            raise ValuationError(
                f"live fetch failed for {self.ticker}: {exc}. "
                f"Run with --use-offline to use the committed sample data."
            ) from exc

        if income is None or income.empty:
            raise ValuationError(
                f"Yahoo returned no income statement for {self.ticker}. The ticker may be "
                f"delisted or unsupported. Run with --use-offline for a known-good sample."
            )

        return {
            "income": income,
            "balance": balance if balance is not None else pd.DataFrame(),
            "cashflow": cashflow if cashflow is not None else pd.DataFrame(),
            "info": info,
            "prices": prices[["Close"]] if prices is not None and not prices.empty else None,
        }


# ---------------------------------------------------------------- fixture I/O


def _read_statement_json(path: Path) -> pd.DataFrame:
    """Read a fixture written by `write_statement_json` back into a DataFrame."""
    if not path.exists():
        return pd.DataFrame()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload:
        return pd.DataFrame()
    frame = pd.DataFrame(payload)
    frame.columns = [_maybe_datetime(c) for c in frame.columns]
    return frame


def _maybe_datetime(value: Any) -> Any:
    """Parse a period label to Timestamp, leaving non-dates untouched."""
    parsed = pd.to_datetime(value, errors="coerce")
    return value if pd.isna(parsed) else parsed


def write_statement_json(df: pd.DataFrame, path: Path) -> None:
    """Write a statement as {period: {row_label: value}}, pretty-printed.

    Deliberately human-readable: someone browsing the repo can open the fixture and
    read Apple's revenue history without running anything.
    """
    payload: dict[str, dict[str, float | None]] = {}
    for col in df.columns:
        key = col.date().isoformat() if hasattr(col, "date") else str(col)
        column: dict[str, float | None] = {}
        for idx, val in df[col].items():
            column[str(idx)] = None if pd.isna(val) else float(val)
        payload[key] = column
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_info_json(info: dict[str, Any], path: Path, keys: tuple[str, ...] = INFO_KEYS) -> None:
    """Write the subset of Yahoo's info blob the model actually reads."""
    trimmed = {k: info.get(k) for k in keys if info.get(k) is not None}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trimmed, indent=2, default=str), encoding="utf-8")
