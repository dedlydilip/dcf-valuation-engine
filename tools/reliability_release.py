"""Rebuild sample outputs and record reproducible regression measurements."""

# ruff: noqa: E402
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.comps.comps_engine import CompsEngine
from src.dcf.engine import DCFEngine
from src.dcf.monte_carlo import run_monte_carlo
from src.dcf.reverse_dcf import solve_reverse_dcf
from src.dcf.sensitivity import football_field, wacc_vs_growth
from src.excel.builder import ExcelModelBuilder
from src.fetcher.yfinance_client import DEFAULT_OFFLINE_DIR, YFinanceClient
from src.models.assumptions import DCFAssumptions
from src.models.errors import ValuationError
from src.models.financials import Financials
from src.models.provenance import json_safe


def synthetic():
    fields = dict(
        revenue=1000,
        ebit=200,
        ebitda=250,
        da=50,
        capex=50,
        sbc=100,
        cfo=250,
        total_debt=100,
        cash=200,
        current_assets=400,
        current_liabilities=200,
        current_debt=0,
        diluted_shares=100,
        stockholders_equity=400,
        interest_expense=5,
    )
    return Financials(
        "TEST",
        pd.DataFrame({k: [v, v] for k, v in fields.items()}, index=["2024-12-31", "2025-12-31"]).T,
        dict(beta=1, sharesOutstanding=100, currentPrice=30, marketCap=3000),
    )


def probes():
    fin = synthetic()
    config = {
        "projection": {
            "revenue_growth": 0.05,
            "tax_rate": 0.25,
            "da_pct_revenue": 0.05,
            "capex_pct_revenue": 0.05,
            "nwc_pct_revenue": 0,
        },
        "terminal": {"method": "value_driver"},
        "monte_carlo": {
            "iterations": 100,
            "wacc_std": 0,
            "terminal_growth_std": 0,
            "ebit_margin_std": 0,
        },
    }
    output = {}
    terminals = {}
    for method in ("expense", "dilute"):
        a = DCFAssumptions.model_validate({**config, "sbc": {"method": method}})
        base = DCFEngine(fin, a).run()
        sim = run_monte_carlo(base)
        reverse = solve_reverse_dcf(fin, a, None, "TEST", target_price=base.value_per_share)
        output[method] = {
            "base": base.value_per_share,
            "zero_uncertainty_mean": sim.stats["mean"],
            "absolute_mc_error": abs(sim.stats["mean"] - base.value_per_share),
            "reverse_growth": reverse.implied_perpetuity_growth,
            "configured_growth": a.terminal.perpetuity_growth,
            "terminal_value": base.terminal.value,
        }
        terminals[method] = base.terminal.value
    output["terminal_sbc_ratio"] = terminals["dilute"] / terminals["expense"]
    a = DCFAssumptions.model_validate(
        {**config, "wacc": {"capital_structure": "target", "target_debt_weight": 0.35}}
    )
    base = DCFEngine(fin, a).run()
    grid = wacc_vs_growth(fin, a, base)
    output["sensitivity"] = {
        "base": base.value_per_share,
        "center": float(grid.frame.iloc[2, 2]),
        "wacc": base.wacc.wacc,
    }
    return output


def main():
    warnings.simplefilter("ignore")
    out = Path("outputs/reliable-release")
    out.mkdir(parents=True, exist_ok=True)
    measurements = {"probes": probes(), "fixtures": [], "sample_outputs": []}
    assumptions = DCFAssumptions.from_yaml()
    for folder in sorted(Path(DEFAULT_OFFLINE_DIR).iterdir()):
        if not folder.is_dir():
            continue
        try:
            fin = YFinanceClient(folder.name, offline_mode=True).get_financials()
            res = DCFEngine(fin, assumptions).run()
            row = {
                "ticker": folder.name,
                "status": "valued",
                "value": res.value_per_share,
                "warnings": res.warnings,
                "data_sha256": res.manifest["data_sha256"],
            }
        except (ValuationError, ValueError) as exc:
            row = {"ticker": folder.name, "status": "refused", "reason": str(exc)}
        measurements["fixtures"].append(row)
    for ticker in ("AAPL", "MSFT", "TSLA"):
        a = DCFAssumptions.from_yaml(scenario="base")
        fin = YFinanceClient(ticker, offline_mode=True).get_financials()
        comps = CompsEngine(ticker, a, offline_mode=True, target_financials=fin).run()
        result = DCFEngine(fin, a, comps.terminal_inputs()).run()
        mc = run_monte_carlo(result)
        sensitivity = wacc_vs_growth(fin, a, result, comps.terminal_inputs())
        football = football_field(result, sensitivity, monte_carlo=mc.stats)
        builder = ExcelModelBuilder(
            result, financials=fin, comps=comps, monte_carlo=mc.stats, football=football
        )
        file = out / f"{ticker}_base_dcf.xlsx"
        builder.build(file)
        summary = {
            "summary": result.summary(),
            "monte_carlo": {**mc.stats, "diagnostics": mc.diagnostics},
            "workbook_refs": builder.refs,
        }
        file.with_suffix(".json").write_text(
            json.dumps(json_safe(summary), indent=2, allow_nan=False), encoding="utf-8"
        )
        measurements["sample_outputs"].append(
            {
                "ticker": ticker,
                "value": result.value_per_share,
                "file": file.name,
                "manifest": result.manifest,
            }
        )
        print(ticker, "written", flush=True)
    (out / "validation-measurements.json").write_text(
        json.dumps(json_safe(measurements), indent=2, allow_nan=False), encoding="utf-8"
    )
    print(
        "Fixture counts:",
        {
            status: sum(r["status"] == status for r in measurements["fixtures"])
            for status in ("valued", "refused")
        },
        flush=True,
    )


if __name__ == "__main__":
    main()
