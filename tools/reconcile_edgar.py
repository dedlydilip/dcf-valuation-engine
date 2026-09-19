"""Compare what EDGAR filed against what Yahoo reported, field by field.

Two independent renderings of the same filing have to agree. When they do not, one of
them is wrong, and the disagreement says which field to look at -- the same argument
`tools/build_crosscheck.py` makes for Excel against Python.

Run against the committed fixtures so the Yahoo side is the exact data the pinned
numbers reproduce from:

    python tools/reconcile_edgar.py AAPL MSFT PG

Three categories of difference are known and explained. Anything else is a finding.

  A US filer reconciles to 0.00% on every field both sources carry. Apple does, across
  four fiscal years and twelve fields, with one documented exception below.

  A foreign filer differs on the most recent period, because EDGAR lags. Honda's latest
  20-F fact is FY2025-03-31 while Yahoo already carries FY2026-03-31. That is a real
  trade-off between authority and recency, not a bug in either source.

  Apple FY2022 `total_debt` disagrees by 9.37%, and the cause is on Yahoo's side. Yahoo
  reports 132,480, which is borrowings (120,069) plus finance leases (941) plus operating
  leases (11,470) to the dollar. In FY2023, FY2024 and FY2025 the same Yahoo field is
  borrowings alone. One definition in one year and a different one in the next three is
  not something to reproduce, so this tool reports it rather than absorbing it -- see the
  note on COMPOSED_FIELDS in src/fetcher/edgar.py for why EDGAR stays on borrowings.

A period pair is matched by proximity rather than equality because the two sources date
fiscal years differently: Yahoo rounds to month-end, EDGAR carries the real 52/53-week
date. Apple's FY2024 is 2024-09-28 at EDGAR and 2024-09-30 at Yahoo. Requiring exact
equality compared one year in four and called the silence agreement.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.fetcher.edgar import fetch_edgar_statements  # noqa: E402
from src.fetcher.normalizer import FinancialNormalizer  # noqa: E402
from src.fetcher.yfinance_client import YFinanceClient  # noqa: E402

# Fields worth reconciling: the ones that actually drive the valuation. A difference in
# `gross_profit` is interesting; a difference in `ebit` changes the answer.
COMPARED = (
    "revenue",
    "ebit",
    "da",
    "capex",
    "sbc",
    "cfo",
    "cash",
    "total_debt",
    "net_income",
    "tax_provision",
    "pretax_income",
    "diluted_shares",
)

TOLERANCE = 0.0005  # 0.05%, to absorb nothing more than float formatting


def _canonical_from_edgar(ticker: str) -> tuple[pd.DataFrame, dict]:
    frames, meta = fetch_edgar_statements(ticker)
    canonical = FinancialNormalizer.canonicalize(
        {"income": frames["income"], "balance": frames["balance"], "cashflow": frames["cashflow"]}
    )
    return canonical, meta


def _canonical_from_yahoo(ticker: str) -> pd.DataFrame:
    """The committed fixture, normalised through the same path the engine uses."""
    return YFinanceClient(ticker, offline_mode=True).get_financials().statements


def reconcile(ticker: str) -> int:
    print(f"\n{'=' * 78}\n{ticker}\n{'=' * 78}")
    try:
        edgar, meta = _canonical_from_edgar(ticker)
    except Exception as exc:
        print(f"  EDGAR unavailable: {exc}")
        return 0

    try:
        yahoo = _canonical_from_yahoo(ticker)
    except Exception as exc:
        print(f"  Yahoo fixture unavailable: {exc}")
        return 0

    print(f"  taxonomy={meta['taxonomy']}  cik={meta['cik']:010d}  accn={meta['latest_accession']}")

    pairs = _pair_periods(edgar.columns, yahoo.columns)
    if not pairs:
        print(
            f"  NO COMPARABLE PERIODS. edgar={[str(c.date()) for c in sorted(edgar.columns)][-3:]} "
            f"yahoo={[str(c.date()) for c in sorted(yahoo.columns)][-3:]}"
        )
        return 0

    mismatches = 0
    for e_period, y_period in pairs:
        label = f"{e_period.date()}"
        if e_period != y_period:
            label += f"  (Yahoo dates it {y_period.date()})"
        print(f"  --- {label}")
        print(f"  {'field':18s} {'EDGAR':>20s} {'Yahoo':>20s} {'diff':>10s}")
        for field in COMPARED:
            e = edgar.loc[field, e_period] if field in edgar.index else float("nan")
            y = yahoo.loc[field, y_period] if field in yahoo.index else float("nan")
            if pd.isna(e) and pd.isna(y):
                continue
            if pd.isna(e) or pd.isna(y):
                missing = "EDGAR" if pd.isna(e) else "Yahoo"
                print(
                    f"  {field:18s} {_fmt(e):>20s} {_fmt(y):>20s} {'—':>10s}  (absent: {missing})"
                )
                continue
            diff = float("inf") if y == 0 else abs(e / y - 1.0)
            flag = "" if diff <= TOLERANCE else "   <-- MISMATCH"
            if diff > TOLERANCE:
                mismatches += 1
            print(f"  {field:18s} {_fmt(e):>20s} {_fmt(y):>20s} {diff:>9.2%}{flag}")
        print()

    if mismatches:
        print(f"  {mismatches} field-period(s) disagree beyond {TOLERANCE:.2%}")
    else:
        print(f"  every compared field agrees within {TOLERANCE:.2%} across {len(pairs)} period(s)")
    return mismatches


# Yahoo rounds a fiscal period end to month-end; EDGAR carries the real 52/53-week date
# the company filed. Apple's FY2024 is 2024-09-28 at EDGAR and 2024-09-30 at Yahoo, so
# requiring exact equality compared one year out of five and called it agreement.
PERIOD_TOLERANCE_DAYS = 20


def _pair_periods(edgar_cols, yahoo_cols) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Pair each EDGAR period with the Yahoo period naming the same fiscal year."""
    pairs = []
    remaining = sorted(yahoo_cols)
    for e in sorted(edgar_cols):
        nearest = min(remaining, key=lambda y: abs((y - e).days), default=None)
        if nearest is None:
            continue
        if abs((nearest - e).days) <= PERIOD_TOLERANCE_DAYS:
            pairs.append((e, nearest))
            remaining.remove(nearest)
    return pairs


def _fmt(v: float) -> str:
    return "—" if pd.isna(v) else f"{v:,.0f}"


def main(argv: list[str]) -> int:
    warnings.simplefilter("ignore")
    tickers = [t.upper() for t in argv[1:]] or ["AAPL", "MSFT", "PG"]
    total = sum(reconcile(t) for t in tickers)
    print(f"\n{'=' * 78}")
    print(f"TOTAL MISMATCHES: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
