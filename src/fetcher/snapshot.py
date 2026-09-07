"""Capture live Yahoo data as committed offline fixtures.

Run once per ticker to refresh `data/offline_sample/<TICKER>/`. The fixtures are
what make the repository runnable by someone who clones it on a day when Yahoo is
down, rate-limiting, or has renamed half its rows.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from datetime import UTC, datetime
from pathlib import Path

from src.fetcher.yfinance_client import (
    DEFAULT_OFFLINE_DIR,
    INFO_FILE,
    PRICES_FILE,
    STATEMENT_FILES,
    YFinanceClient,
    write_info_json,
    write_statement_json,
)

# The three the repository was built around. Named separately because the README's
# headline numbers and the Excel cross-check are pinned to exactly these.
CORE_TICKERS = ("AAPL", "MSFT", "TSLA")

# What `snapshot` fetches when no ticker is named.
SAMPLE_TICKERS = CORE_TICKERS


def peer_universe_tickers() -> tuple[str, ...]:
    """Every ticker the comps fallback maps can request.

    Imported inside the function: `comps_engine` imports this module's siblings, and
    pulling it in at module scope would risk closing that loop.
    """
    from src.comps.comps_engine import peer_universe

    return peer_universe()


def snapshot_ticker(ticker: str, out_dir: Path | str = DEFAULT_OFFLINE_DIR) -> Path:
    """Fetch one ticker live and write its fixture set. Returns the directory written."""
    ticker = ticker.upper().strip()
    target = Path(out_dir) / ticker
    target.mkdir(parents=True, exist_ok=True)

    client = YFinanceClient(ticker, offline_mode=False)
    raw = client._fetch_live_raw()

    write_statement_json(raw["income"], target / STATEMENT_FILES["income"])
    write_statement_json(raw["balance"], target / STATEMENT_FILES["balance"])
    write_statement_json(raw["cashflow"], target / STATEMENT_FILES["cashflow"])
    write_info_json(raw["info"], target / INFO_FILE)

    prices = raw.get("prices")
    if prices is not None and not prices.empty:
        out = prices.copy()
        out.index.name = "Date"
        out.to_csv(target / PRICES_FILE)

    files = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in target.iterdir()
        if p.name != "manifest.json" and p.is_file()
    }
    manifest = dict(
        capture_timestamp=datetime.now(UTC).isoformat(),
        provider="yfinance",
        provider_version=importlib.metadata.version("yfinance"),
        ticker=ticker,
        sha256=files,
        market_timestamp=raw.get("info", {}).get("regularMarketTime"),
    )
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return target


def snapshot_all(
    tickers: tuple[str, ...] = SAMPLE_TICKERS, out_dir: Path | str = DEFAULT_OFFLINE_DIR
) -> list[Path]:
    return [snapshot_ticker(t, out_dir) for t in tickers]
