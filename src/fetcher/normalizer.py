"""Turn whatever the data source hands back into clean float64.

Financial APIs leak presentation formatting into their payloads: thousands
separators, magnitude suffixes, accounting parentheses for negatives, currency
symbols, and a small zoo of null tokens. Anything that cannot be parsed becomes
NaN rather than raising, because a single unparseable cell should not kill a
valuation -- DataQualityGate decides afterwards whether the gaps are fatal.
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd

from src.models.financials import FIELD_MAP

_SUFFIXES = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
_NULL_TOKENS = {"", "-", "--", "—", "n/a", "na", "nan", "none", "null", "#n/a", "invalid"}
_CLEAN_RE = re.compile(r"[,\s$£€¥]")

# Fields Yahoo reports with the opposite sign to this model's convention.
_ABS_FIELDS = ("capex", "buybacks")


def to_float(value: Any) -> float:
    """Parse one cell to float, returning NaN for anything unparseable."""
    if value is None:
        return float("nan")
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)

    text = str(value).strip()
    if text.lower() in _NULL_TOKENS:
        return float("nan")

    # Strip currency symbols and separators BEFORE testing for accounting parentheses.
    # Doing it the other way round meant "$(500)" failed the startswith("(") test, and
    # a currency-prefixed negative was silently lost to NaN.
    text = _CLEAN_RE.sub("", text)

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]

    is_pct = text.endswith("%")
    if is_pct:
        text = text[:-1]
    if not text:
        return float("nan")

    multiplier = 1.0
    if text[-1].upper() in _SUFFIXES:
        multiplier = _SUFFIXES[text[-1].upper()]
        text = text[:-1]

    try:
        result = float(text) * multiplier
    except ValueError:
        return float("nan")

    if is_pct:
        result /= 100.0
    return -result if negative else result


class FinancialNormalizer:
    """Coercion and canonicalisation for raw statement data."""

    @staticmethod
    def clean_series(series: pd.Series) -> pd.Series:
        return pd.Series(
            [to_float(v) for v in series], index=series.index, name=series.name, dtype="float64"
        )

    @staticmethod
    def clean(df: pd.DataFrame) -> pd.DataFrame:
        """Coerce every column to float64. Unparseable cells become NaN."""
        return pd.DataFrame(
            {col: FinancialNormalizer.clean_series(df[col]) for col in df.columns},
            index=df.index,
        )

    @staticmethod
    def canonicalize(
        frames: dict[str, pd.DataFrame],
        field_map: dict[str, list[str]] | None = None,
    ) -> pd.DataFrame:
        """Collapse income/balance/cashflow frames into one canonical statement table.

        Each canonical field takes the first candidate Yahoo row that exists and is
        not entirely null, searched across all three statements. Columns come back
        sorted oldest to newest so `.iloc[-1]` always means "most recent year".
        """
        field_map = field_map or FIELD_MAP

        lookup: dict[str, pd.Series] = {}
        for frame in frames.values():
            if frame is None or frame.empty:
                continue
            for row_label in frame.index:
                key = str(row_label).strip().lower()
                if key not in lookup:
                    lookup[key] = frame.loc[row_label]

        all_columns: set[Any] = set()
        for series in lookup.values():
            all_columns.update(series.index)
        columns = sorted(all_columns, key=_period_sort_key)

        rows: dict[str, list[float]] = {}
        aliases = {}
        for canonical, candidates in field_map.items():
            chosen: pd.Series | None = None
            for candidate in candidates:
                series = lookup.get(candidate.strip().lower())
                if series is None:
                    continue
                cleaned = FinancialNormalizer.clean_series(series)
                if cleaned.notna().any():
                    for period, value in cleaned.items():
                        if pd.notna(value) and (chosen is None or pd.isna(chosen.get(period))):
                            aliases.setdefault(canonical, {})[str(period)] = candidate
                    chosen = cleaned if chosen is None else chosen.combine_first(cleaned)
            if chosen is None:
                continue
            values = [to_float(chosen.get(col, np.nan)) for col in columns]
            if canonical in _ABS_FIELDS:
                values = [abs(v) if pd.notna(v) else v for v in values]
            rows[canonical] = values

        if not rows:
            return pd.DataFrame(dtype="float64")

        frame = pd.DataFrame(rows, index=columns).T.astype("float64")
        frame = _drop_empty_periods(frame)
        frame.attrs["source_aliases"] = aliases
        return frame


def _drop_empty_periods(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop fiscal periods that carry no income statement.

    The three statements rarely cover an identical set of dates, so taking the union
    of their columns leaves stub periods holding a balance sheet and nothing else.
    A period with no revenue is not a usable fiscal year, and leaving it in would
    poison every trailing-average default the projector computes.
    """
    if "revenue" not in frame.index or frame.empty:
        return frame
    keep = [col for col in frame.columns if pd.notna(frame.loc["revenue", col])]
    if not keep:
        return frame
    return frame[keep]


def _period_sort_key(value: Any) -> Any:
    """Sort period labels chronologically, tolerating strings and timestamps alike."""
    ts = pd.to_datetime(value, errors="coerce")
    if pd.notna(ts):
        return (0, ts.value)
    return (1, str(value))
