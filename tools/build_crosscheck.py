"""Build a workbook for every ticker and SBC method, and record what Python computed.

Paired with tools/crosscheck.ps1, which opens each workbook in Excel, forces a full
recalculation, and compares Excel's answer against the numbers written here.

Two independent implementations of the same valuation -- one in Python, one in Excel
formulas -- have to agree. When they do not, one of them is wrong, and the
disagreement says which cells to look at. This is how the double-dilution bug in the
share-count input was found.

    python tools/build_crosscheck.py
    pwsh -File tools/crosscheck.ps1
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.comps.comps_engine import CompsEngine  # noqa: E402
from src.dcf.bridge import base_share_count  # noqa: E402
from src.dcf.engine import DCFEngine  # noqa: E402
from src.excel.builder import build_excel_model  # noqa: E402
from src.fetcher.yfinance_client import YFinanceClient  # noqa: E402
from src.models.assumptions import DCFAssumptions  # noqa: E402

TICKERS = ("AAPL", "MSFT", "TSLA")
METHODS = ("expense", "dilute")
OUT_DIR = Path("outputs/crosscheck")


def sensitivity_expectations(result) -> dict:
    """Expected values from full engine repricing, not copied Excel expressions."""
    from src.dcf.sensitivity import wacc_vs_exit_multiple, wacc_vs_growth
    from src.models.provenance import json_safe

    fin, a, peers = result.financials, result.assumptions, result.comps_inputs
    return json_safe(
        {
            "growth_grid": wacc_vs_growth(fin, a, result, peers).frame.to_numpy().tolist(),
            "multiple_grid": wacc_vs_exit_multiple(fin, a, result, peers).frame.to_numpy().tolist(),
        }
    )


def main() -> int:
    warnings.simplefilter("ignore")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    expected = []

    for ticker in TICKERS:
        financials = YFinanceClient(ticker, offline_mode=True).get_financials()
        for method in METHODS:
            assumptions = DCFAssumptions.from_yaml(overrides={"sbc": {"method": method}})
            comps = CompsEngine(
                ticker, assumptions, offline_mode=True, target_financials=financials
            ).run()
            result = DCFEngine(
                financials, assumptions, comps.terminal_inputs(), ticker=ticker
            ).run()
            path = OUT_DIR / f"{ticker}_{method}.xlsx"
            build_excel_model(
                result,
                path,
                financials=financials,
                comps=comps,
            )

            expected.append(
                {
                    "file": str(path).replace("\\", "/"),
                    "ticker": ticker,
                    "method": method,
                    "value_per_share": result.value_per_share,
                    "equity_value": result.bridge.equity_value,
                    "enterprise_value": result.enterprise_value,
                    "shares": result.bridge.shares,
                    "base_shares": base_share_count(financials),
                    "wacc": result.wacc.wacc,
                    "terminal_value": result.terminal.value,
                    "sensitivity": sensitivity_expectations(result),
                }
            )
            print(
                f"{ticker:5s} {method:8s} vps=${result.value_per_share:9.4f} "
                f"shares={result.bridge.shares / 1e9:7.4f}bn -> {path}"
            )

    (OUT_DIR / "expected.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_DIR / 'expected.json'} with {len(expected)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
