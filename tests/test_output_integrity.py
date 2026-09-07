import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.cli import cli
from src.dcf.dashboard import generate_dashboard_html
from src.fetcher.yfinance_client import YFinanceClient
from src.models.errors import ValuationError


def test_dashboard_javascript_updates_all_derived_values(tmp_path):
    node = os.environ.get("NODE_BIN") or shutil.which("node")
    if not node:
        pytest.skip("Node is required for the dashboard runtime test")
    file = tmp_path / "dashboard.html"
    generate_dashboard_html(
        {
            "TEST": {
                "bear_value": -100,
                "base_value": 20,
                "bull_value": 40,
                "current_price": 10,
                "wacc": 0.1,
                "currency": "USD",
                "warnings": ["Fixture warning"],
                "blended": {
                    "expected_value": 20,
                    "discount_to_expected": 0.5,
                    "verdict": "Assumed scenario",
                },
            }
        },
        file,
    )
    run = subprocess.run(
        [node, str(Path(__file__).with_name("dashboard_runtime.cjs")), str(file)],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_empty_dashboard_fails_without_publishing(tmp_path):
    output = tmp_path / "empty.html"
    result = CliRunner().invoke(
        cli, ["dashboard", "--tickers", "NO_SUCH_FIXTURE", "--use-offline", "--out", str(output)]
    )
    assert result.exit_code != 0
    assert not output.exists()


def test_tampered_snapshot_refused(tmp_path):
    folder = tmp_path / "TEST"
    folder.mkdir()
    for name in ["income_stmt.json", "balance_sheet.json", "cashflow.json", "info.json"]:
        (folder / name).write_text("{}", encoding="utf-8")
    manifest = {
        "ticker": "TEST",
        "sha256": {
            name: "wrong"
            for name in ["income_stmt.json", "balance_sheet.json", "cashflow.json", "info.json"]
        },
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValuationError, match="integrity check failed"):
        YFinanceClient("TEST", offline_mode=True, offline_path=folder).get_financials()
