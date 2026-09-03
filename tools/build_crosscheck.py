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
from src.dcf.projector import terminal_year_ebitda  # noqa: E402
from src.excel.builder import build_excel_model  # noqa: E402
from src.fetcher.yfinance_client import YFinanceClient  # noqa: E402
from src.models.assumptions import DCFAssumptions  # noqa: E402

TICKERS = ("AAPL", "MSFT", "TSLA")
METHODS = ("expense", "dilute")
OUT_DIR = Path("outputs/crosscheck")


def sensitivity_expectations(result) -> dict:
    """Reproduce the Sensitivity sheet's formulas independently, in Python.

    The workbook's grid is self-contained: it holds the forecast cash flows fixed and
    re-discounts them at each rate, rather than re-running the model. So the honest
    comparison is a second implementation of that same definition -- which is what a
    cross-check is for. `src/dcf/sensitivity.py` answers a different question (it
    solves for the beta that produces each WACC and re-runs the engine), so comparing
    Excel against it would be comparing two different calculations.

    Grid layout mirrors `ExcelModelBuilder._sensitivity`: growth across the columns,
    WACC down the rows, then a second block against the exit multiple.
    """
    cfg = result.assumptions.sensitivity
    fcf = [float(v) for v in result.projection.unlevered_fcf]
    mid_year = result.assumptions.projection.mid_year_convention
    offset = 0.5 if mid_year else 0.0
    exponents = [t - offset for t in range(1, len(fcf) + 1)]
    last_exp = exponents[-1]
    last_adj = float(result.projection.adjusted_fcf.iloc[-1])

    bridge = result.bridge
    net_bridge = (
        bridge.total_debt
        - bridge.cash
        + bridge.minority_interest
        + bridge.preferred_equity
        - bridge.investments
    )
    shares = bridge.shares

    def pv_explicit(wacc: float) -> float:
        return sum(f / (1.0 + wacc) ** e for f, e in zip(fcf, exponents, strict=True))

    base_wacc = result.wacc.wacc
    base_growth = result.assumptions.terminal.perpetuity_growth
    waccs = [base_wacc + d for d in cfg.wacc_deltas]
    growths = [base_growth + d for d in cfg.growth_deltas]

    growth_grid = []
    for wacc in waccs:
        row = []
        for growth in growths:
            if wacc <= growth:
                row.append(None)  # the workbook writes "n/a" here
                continue
            terminal = last_adj * (1.0 + growth) / (wacc - growth) / (1.0 + wacc) ** last_exp
            row.append((pv_explicit(wacc) + terminal - net_bridge) / shares)
        growth_grid.append(row)

    exit_result = result.terminal_all.get("exit_multiple")
    base_multiple = (
        exit_result.multiple_used
        if exit_result and exit_result.multiple_used
        else result.assumptions.terminal.static_exit_multiple
    )
    multiples = [max(base_multiple + d, 0.5) for d in cfg.multiple_deltas]
    term_ebitda = terminal_year_ebitda(result.projection)

    # Full period, not the mid-year one. An exit multiple is a sale price at a point in
    # time. Both this and the workbook formula previously used `last_exp` (4.5 under a
    # 5-year forecast), so the cross-check compared Excel against a Python
    # reimplementation of the same error, agreed to 0.000%, and reported 324 passing
    # comparisons as evidence of correctness. It was evidence of consistency. The
    # exponent here is derived from the convention rather than copied from the sheet.
    exit_exponent = float(len(fcf))

    multiple_grid = []
    for wacc in waccs:
        multiple_grid.append(
            [
                (
                    pv_explicit(wacc)
                    + term_ebitda * m / (1.0 + wacc) ** exit_exponent
                    - net_bridge
                )
                / shares
                for m in multiples
            ]
        )

    return {
        "waccs": waccs,
        "growths": growths,
        "multiples": multiples,
        "growth_grid": growth_grid,
        "multiple_grid": multiple_grid,
        "net_bridge": net_bridge,
    }


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
