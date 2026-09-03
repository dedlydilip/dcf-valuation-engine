"""Comparable-company multiples.

Feeds two things: a standalone relative valuation, and the peer median that the
dynamic exit multiple decays away from in `src/dcf/terminal_value.py`.

Peer selection is the honest weak point of any comps analysis, and it is the first
thing an interviewer pushes on. There is no defensible automatic answer -- a peer set
is a judgement call about which businesses face the same demand, cost and regulatory
conditions. So peers come from config, with a small curated sector map as a fallback,
and the source of the peer set is recorded on the output rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from src.fetcher.yfinance_client import DEFAULT_OFFLINE_DIR, YFinanceClient
from src.models.assumptions import DCFAssumptions
from src.models.financials import (
    Financials,
    field_value,
    info_dict,
    price_currency,
    statement_currency,
)
from src.paths import resolve_path

# Checked before SECTOR_PEERS, on Yahoo's finer-grained `industry` field. Yahoo files
# every one of these under the single broad sector "Technology" alongside AAPL, MSFT,
# GOOGL and META, which is not a peer group for a cyclical, capital-intensive chip
# maker. Found on Micron: falling back to the Technology sector bucket pulled in
# NVDA's growth rate as the peer median (16.2%), decayed the exit multiple to -6.9x
# against Micron's own 4% terminal growth, and only the mature-industry floor kept the
# number from being reported raw.
INDUSTRY_PEERS: dict[str, list[str]] = {
    "Semiconductors": ["NVDA", "AMD", "TSM", "QCOM", "INTC", "AVGO", "TXN", "MU"],
    "Semiconductor Equipment & Materials": ["ASML", "AMAT", "LRCX", "KLAC"],
}

# Fallback peer sets, used only when config supplies none and no industry-level bucket
# above matched. Deliberately small and obvious rather than pretending to be a
# screening engine.
SECTOR_PEERS: dict[str, list[str]] = {
    "Technology": ["AAPL", "MSFT", "GOOGL", "META", "NVDA", "ORCL", "CRM", "ADBE"],
    "Consumer Cyclical": ["TSLA", "AMZN", "HD", "MCD", "NKE", "SBUX"],
    "Communication Services": ["GOOGL", "META", "NFLX", "DIS", "CMCSA"],
    "Healthcare": ["JNJ", "UNH", "LLY", "ABBV", "MRK", "PFE"],
    "Financial Services": ["JPM", "BAC", "WFC", "GS", "MS", "BLK"],
    "Industrials": ["CAT", "HON", "GE", "UNP", "BA", "LMT"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG"],
    "Consumer Defensive": ["PG", "KO", "PEP", "COST", "WMT"],
}

MULTIPLE_COLUMNS = ["ev_ebitda", "ev_ebit", "ev_revenue", "pe"]

# Sanity bands for trading multiples. A company at 114x EV/EBITDA is being priced on
# a story the multiple cannot express, and including it in a median does not make the
# median more informative -- it makes it wrong. Screened names stay visible in the
# table and are excluded only from the medians.
MULTIPLE_BOUNDS: dict[str, tuple[float, float]] = {
    "ev_ebitda": (0.0, 50.0),
    "ev_ebit": (0.0, 60.0),
    "ev_revenue": (0.0, 30.0),
    "pe": (0.0, 100.0),
}


@dataclass
class CompsResult:
    table: pd.DataFrame
    medians: dict[str, float]
    target: str
    peer_source: str
    skipped: dict[str, str] = field(default_factory=dict)
    min_peers: int = 3
    screened_out: dict[str, list[str]] = field(default_factory=dict)

    @property
    def peers(self) -> list[str]:
        return [t for t in self.table.index if t != self.target]

    @property
    def peer_count(self) -> int:
        return int(self.medians.get("peer_count", 0))

    @property
    def usable_for_terminal(self) -> bool:
        """Whether the peer set is deep enough to anchor the terminal multiple."""
        return self.peer_count >= self.min_peers and "ev_ebitda_median" in self.medians

    def terminal_inputs(self) -> dict[str, float]:
        """Medians to feed the exit-multiple decay, or nothing if the set is too thin.

        Returning an empty dict makes the terminal value fall back to the configured
        static multiple, which is a stated assumption rather than a median of two.
        """
        return dict(self.medians) if self.usable_for_terminal else {}

    def notes(self) -> list[str]:
        out: list[str] = []
        if not self.usable_for_terminal:
            out.append(
                f"Only {self.peer_count} screened peer(s) available, below the minimum of "
                f"{self.min_peers}. Comps medians are reported for reference but do not "
                f"drive the terminal multiple, which falls back to the configured static "
                f"assumption."
            )
        for column, names in self.screened_out.items():
            if names:
                out.append(
                    f"Excluded from the {column} median as outside the plausible band: "
                    f"{', '.join(names)}."
                )
        for ticker, reason in self.skipped.items():
            out.append(f"No data for peer {ticker}: {reason}")
        return out

    def implied_values(self, target_metrics: dict[str, float]) -> pd.DataFrame:
        """Enterprise value implied by applying each peer-median multiple to the target."""
        rows = []
        for multiple, metric_key in (
            ("ev_ebitda", "ebitda"),
            ("ev_ebit", "ebit"),
            ("ev_revenue", "revenue"),
        ):
            median = self.medians.get(f"{multiple}_median")
            metric = target_metrics.get(metric_key)
            if median is None or metric is None or pd.isna(median) or pd.isna(metric) or metric <= 0:
                continue
            rows.append(
                {
                    "multiple": multiple,
                    "peer_median": median,
                    "target_metric": metric,
                    "implied_ev": median * metric,
                }
            )
        return pd.DataFrame(rows)


class CompsEngine:
    """Build a peer multiples table."""

    def __init__(
        self,
        target: str,
        assumptions: DCFAssumptions | None = None,
        offline_mode: bool = False,
        offline_dir: Path | str = DEFAULT_OFFLINE_DIR,
        target_financials: Financials | None = None,
    ) -> None:
        self.target = target.upper().strip()
        self.assumptions = assumptions or DCFAssumptions()
        self.offline_mode = offline_mode
        # Resolved the same way as the fixture path in YFinanceClient. Unresolved, the
        # glob in resolve_peers() below silently returns an empty peer set from any
        # directory but the repo root -- no error, just comps that quietly vanish.
        self.offline_dir = resolve_path(offline_dir)
        self.target_financials = target_financials

    # ----------------------------------------------------------- peer selection

    def resolve_peers(self) -> tuple[list[str], str]:
        configured = [p.upper() for p in self.assumptions.comps.peers if p]
        if configured:
            peers = [p for p in configured if p != self.target]
            return peers[: self.assumptions.comps.max_peers], "config"

        if self.offline_mode:
            # Only tickers with committed fixtures can be priced without a network.
            available = sorted(
                p.name for p in self.offline_dir.glob("*") if p.is_dir() and p.name != self.target
            )
            return available[: self.assumptions.comps.max_peers], "offline_fixtures"

        info = (self.target_financials.info or {}) if self.target_financials is not None else {}
        industry = info.get("industry")
        sector = info.get("sector")

        # Industry first: it is the finer-grained field, and the sector-level bucket
        # would otherwise mix a cyclical chip maker into the same peer set as a
        # software platform because Yahoo files both under "Technology".
        if industry and industry in INDUSTRY_PEERS:
            candidates = INDUSTRY_PEERS[industry]
            peers = [p for p in candidates if p != self.target]
            return peers[: self.assumptions.comps.max_peers], f"industry_map:{industry}"

        candidates = SECTOR_PEERS.get(sector or "", [])
        peers = [p for p in candidates if p != self.target]
        return peers[: self.assumptions.comps.max_peers], f"sector_map:{sector or 'unknown'}"

    # -------------------------------------------------------------------- build

    def run(self) -> CompsResult:
        peers, source = self.resolve_peers()
        tickers = [self.target] + peers

        rows: dict[str, dict[str, float]] = {}
        skipped: dict[str, str] = {}

        for ticker in tickers:
            try:
                financials = (
                    self.target_financials
                    if ticker == self.target and self.target_financials is not None
                    else YFinanceClient(
                        ticker,
                        offline_mode=self.offline_mode,
                        offline_path=self.offline_dir / ticker,
                    ).get_financials()
                )
                incoherent = _currency_mismatch(financials)
                if incoherent:
                    skipped[ticker] = incoherent
                    continue
                rows[ticker] = compute_multiples(financials)
            except Exception as exc:
                skipped[ticker] = f"{type(exc).__name__}: {exc}"

        table = pd.DataFrame(rows).T if rows else pd.DataFrame()
        medians, screened = _medians(
            table,
            exclude=self.target,
            screen_outliers=self.assumptions.comps.screen_outliers,
        )

        return CompsResult(
            table=table,
            medians=medians,
            target=self.target,
            peer_source=source,
            skipped=skipped,
            min_peers=self.assumptions.comps.min_peers,
            screened_out=screened,
        )


def _currency_mismatch(financials: Financials) -> str | None:
    """Reason to exclude a peer whose statements and quote are in different units.

    The same defect as the one the quality gate catches for the target company, one
    level over, and it is quieter here because a single bad peer just moves a median
    rather than producing an obviously absurd share price. Unconverted, TSM computed
    at 0.03x EV/EBITDA -- TWD earnings against a USD market capitalisation -- and 0.03
    sits comfortably inside the 0-50 plausibility band, so the outlier screen let it
    straight through into the median.

    Note this excludes only peers that are internally incoherent. A peer that reports
    and trades in euros throughout is perfectly comparable to a dollar company: a
    multiple is a ratio, so the currency cancels.
    """
    statement = statement_currency(financials)
    price = price_currency(financials)
    if statement and price and statement != price:
        return (
            f"statements in {statement} but quoted in {price}; every multiple would mix "
            f"the two. Re-snapshot with a converted fixture, or name a different peer."
        )
    return None


def compute_multiples(financials: Financials) -> dict[str, float]:
    """Trading multiples for one company. Negative denominators become NaN.

    A negative EV/EBITDA is not a cheap stock, it is a meaningless number, and letting
    one into a median drags the whole peer set somewhere untrue.
    """
    info = info_dict(financials)

    market_cap = _info_float(info, "marketCap")
    if pd.isna(market_cap):
        shares = _info_float(info, "sharesOutstanding")
        price = _info_float(info, "currentPrice")
        market_cap = shares * price if pd.notna(shares) and pd.notna(price) else float("nan")

    debt = field_value(financials, "total_debt", 0.0)
    cash = field_value(financials, "cash", 0.0) + field_value(
        financials, "short_term_investments", 0.0
    )
    enterprise_value = market_cap + debt - cash

    revenue = field_value(financials, "revenue")
    ebitda = field_value(financials, "ebitda")
    ebit = field_value(financials, "ebit")
    net_income = field_value(financials, "net_income")

    revenue_series = pd.to_numeric(financials.series("revenue"), errors="coerce").dropna()
    growth = (
        float(revenue_series.iloc[-1] / revenue_series.iloc[-2] - 1.0)
        if len(revenue_series) >= 2 and revenue_series.iloc[-2] > 0
        else float("nan")
    )

    return {
        "market_cap": market_cap,
        "enterprise_value": enterprise_value,
        "revenue": revenue,
        "ebitda": ebitda,
        "ebit": ebit,
        "ev_ebitda": _ratio(enterprise_value, ebitda),
        "ev_ebit": _ratio(enterprise_value, ebit),
        "ev_revenue": _ratio(enterprise_value, revenue),
        "pe": _ratio(market_cap, net_income),
        "revenue_growth": growth,
        "ebitda_margin": _ratio(ebitda, revenue, allow_negative_numerator=True),
    }


def _medians(
    table: pd.DataFrame, exclude: str | None = None, screen_outliers: bool = True
) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Peer medians, excluding the target so it cannot anchor its own benchmark.

    Outliers are screened per multiple rather than per company: a name can be a fair
    EV/Revenue comparable while its P/E is meaningless because earnings are near zero.
    """
    if table.empty:
        return {}, {}
    peers = table.drop(index=exclude, errors="ignore")
    if peers.empty:
        peers = table

    out: dict[str, float] = {}
    screened: dict[str, list[str]] = {}

    for column in (*MULTIPLE_COLUMNS, "revenue_growth", "ebitda_margin"):
        if column not in peers.columns:
            continue
        values = pd.to_numeric(peers[column], errors="coerce").dropna()
        if screen_outliers and column in MULTIPLE_BOUNDS:
            low, high = MULTIPLE_BOUNDS[column]
            mask = (values > low) & (values <= high)
            dropped = [str(name) for name in values.index[~mask]]
            if dropped:
                screened[column] = dropped
            values = values[mask]
        if not values.empty:
            out[f"{column}_median"] = float(values.median())

    # Peer count is the number with a usable EV/EBITDA, the multiple that drives the
    # terminal value -- not the number of tickers that returned any data at all.
    if "ev_ebitda" in peers.columns:
        ev_ebitda = pd.to_numeric(peers["ev_ebitda"], errors="coerce").dropna()
        if screen_outliers:
            low, high = MULTIPLE_BOUNDS["ev_ebitda"]
            ev_ebitda = ev_ebitda[(ev_ebitda > low) & (ev_ebitda <= high)]
        out["peer_count"] = float(len(ev_ebitda))
    else:
        out["peer_count"] = 0.0
    return out, screened


def _ratio(
    numerator: float, denominator: float, allow_negative_numerator: bool = False
) -> float:
    if pd.isna(numerator) or pd.isna(denominator) or denominator <= 0:
        return float("nan")
    if numerator < 0 and not allow_negative_numerator:
        return float("nan")
    return float(numerator / denominator)


def _info_float(info: dict[str, Any], key: str) -> float:
    value = info.get(key)
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")
