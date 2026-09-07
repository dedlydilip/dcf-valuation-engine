"""Immutable run identification; unknown dates stay unknown."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return json_safe(value.item())
    return value


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(json_safe(value), sort_keys=True, default=str).encode()
    ).hexdigest()


def run_manifest(financials, assumptions):
    root = Path(__file__).resolve().parents[2]
    sources = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "src").rglob("*.py")
    }
    data = financials.to_dict() if hasattr(financials, "to_dict") else str(financials)
    info = getattr(financials, "info", {})
    info = info if isinstance(info, dict) else {}
    provenance = getattr(financials, "provenance", {})
    return dict(
        schema_version=1,
        generated_at=datetime.now(UTC).isoformat(),
        code_sha256=fingerprint(sources),
        data_sha256=fingerprint(data),
        assumptions=assumptions.model_dump(),
        assumptions_sha256=fingerprint(assumptions.model_dump()),
        source=getattr(financials, "source", "unknown"),
        capture=provenance,
        fiscal_periods=[str(p) for p in getattr(financials, "periods", [])],
        market_timestamp=info.get("regularMarketTime"),
        valuation_basis="undated snapshot; annual forecast periods, no as-of/stub adjustment",
    )
