"""Command-line interface.

    python run.py value --ticker AAPL --use-offline
    python run.py value --ticker MSFT --scenario bear --sbc-method dilute
    python run.py scenarios --ticker TSLA --use-offline
    python run.py snapshot --ticker NVDA

`--use-offline` is the flag that matters: it runs the entire model against fixtures
committed to the repository, so the valuation reproduces with no network at all.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import click
import pandas as pd
from pydantic import ValidationError

from src.comps.comps_engine import CompsEngine
from src.dcf.engine import DCFEngine, ValuationResult
from src.dcf.monte_carlo import run_monte_carlo
from src.dcf.sensitivity import football_field, wacc_vs_growth
from src.excel.builder import build_excel_model
from src.fetcher.snapshot import SAMPLE_TICKERS, snapshot_ticker
from src.fetcher.yfinance_client import DEFAULT_OFFLINE_DIR, YFinanceClient
from src.models.assumptions import DCFAssumptions
from src.models.errors import ValuationError

SCENARIOS = ["base", "bull", "bear"]


@click.group()
@click.version_option("0.1.0", prog_name="dcf-valuation-engine")
def cli() -> None:
    """Institutional-grade DCF and comparable-company valuation."""


# --------------------------------------------------------------------- options


def _common(function):
    function = click.option("--ticker", "-t", required=True, help="Ticker to value.")(function)
    function = click.option(
        "--use-offline",
        is_flag=True,
        help="Use committed sample data instead of the live API. Runs with no network.",
    )(function)
    function = click.option(
        "--offline-dir",
        default=str(DEFAULT_OFFLINE_DIR),
        type=click.Path(),
        show_default=True,
        help="Directory holding offline fixtures.",
    )(function)
    function = click.option(
        "--config",
        default="config/assumptions.yaml",
        type=click.Path(),
        show_default=True,
        help="Base assumptions file.",
    )(function)
    # Companies that report and trade in different currencies -- any ADR. Without one
    # of these the quality gate refuses, rather than silently mixing JPY cash flows
    # with a USD share price.
    function = click.option(
        "--fx-rate",
        type=float,
        default=None,
        help="Statement-to-price exchange rate, e.g. 0.0063 for JPY->USD.",
    )(function)
    function = click.option(
        "--auto-fx",
        is_flag=True,
        help="Fetch the spot rate from Yahoo instead of supplying one. Needs network.",
    )(function)
    function = click.option(
        "--force-sector",
        is_flag=True,
        help="Value a bank, insurer or asset manager anyway. An unlevered DCF cannot "
        "describe one -- for a lender, financing IS the business.",
    )(function)
    return function



def _currency_overrides(
    fx_rate: float | None, auto_fx: bool, force_sector: bool
) -> dict[str, Any]:
    """Turn the currency and sector flags into config overrides.

    Kept in one place because `value` and `scenarios` must behave identically -- a
    scenario comparison that silently refused to convert while `value` converted would
    print three numbers on a different basis from the one beside it.
    """
    out: dict[str, Any] = {}
    currency: dict[str, Any] = {}
    if fx_rate is not None:
        currency["fx_rate"] = fx_rate
    if auto_fx:
        currency["auto_fx"] = True
    if currency:
        out["currency"] = currency
    if force_sector:
        out["quality"] = {"allow_unsuitable_sector": True}
    return out


# ---------------------------------------------------------------------- value


@cli.command()
@_common
@click.option(
    "--scenario",
    type=click.Choice(SCENARIOS),
    default="base",
    show_default=True,
    help="Scenario overrides applied over the base assumptions.",
)
@click.option(
    "--sbc-method",
    type=click.Choice(["expense", "dilute"]),
    default=None,
    help="Override the stock-compensation treatment. Never both -- that double-counts.",
)
@click.option("--years", type=int, default=None, help="Length of the explicit forecast.")
@click.option(
    "--peers", default=None, help="Comma-separated peer tickers for the comps analysis."
)
@click.option(
    "--out",
    default="outputs",
    type=click.Path(),
    show_default=True,
    help="Directory for the generated model.",
)
@click.option("--no-excel", is_flag=True, help="Skip the workbook and print results only.")
@click.option("--no-monte-carlo", is_flag=True, help="Skip the simulation.")
@click.option("--json-out", is_flag=True, help="Also write a JSON summary next to the model.")
def value(
    ticker: str,
    use_offline: bool,
    offline_dir: str,
    config: str,
    scenario: str,
    sbc_method: str | None,
    years: int | None,
    peers: str | None,
    out: str,
    no_excel: bool,
    no_monte_carlo: bool,
    json_out: bool,
    fx_rate: float | None,
    auto_fx: bool,
    force_sector: bool,
) -> None:
    """Value one company and build the Excel model."""
    ticker = ticker.upper().strip()
    overrides: dict[str, Any] = {}
    if sbc_method:
        overrides["sbc"] = {"method": sbc_method}
    if years:
        # The horizon and the growth series must move together. Setting only `years`
        # leaves the config's 5-element revenue_growth list against a different horizon
        # and the length validator rejects it. `_clean` strips None, so nulling the list
        # never worked -- resample it to the requested length instead, holding the final
        # configured growth rate flat when extending.
        overrides.setdefault("projection", {})["years"] = years
        overrides["projection"]["revenue_growth"] = _resize_growth(config, scenario, years)
    if peers:
        overrides["comps"] = {"peers": [p.strip().upper() for p in peers.split(",") if p.strip()]}
    overrides.update(_currency_overrides(fx_rate, auto_fx, force_sector))

    try:
        assumptions = DCFAssumptions.from_yaml(config, scenario=scenario, overrides=_clean(overrides))
        financials = YFinanceClient(
            ticker, offline_mode=use_offline, offline_path=Path(offline_dir) / ticker
        ).get_financials(currency=assumptions.currency)

        comps = CompsEngine(
            ticker,
            assumptions,
            offline_mode=use_offline,
            offline_dir=Path(offline_dir),
            target_financials=financials,
        ).run()

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = DCFEngine(
                financials, assumptions, comps.terminal_inputs(), ticker=ticker
            ).run()
    except (ValuationError, ValidationError, ValueError, FileNotFoundError) as exc:
        raise click.ClickException(_explain(exc)) from exc

    # Only the WACC/growth table is needed, and only for the football field. The
    # Excel Sensitivity sheet builds its own live formulas, so computing an exit-multiple
    # table here was ~25 full engine re-runs whose result was discarded.
    sensitivity = wacc_vs_growth(financials, assumptions, result, comps.terminal_inputs())
    monte = None
    if assumptions.monte_carlo.enabled and not no_monte_carlo:
        monte = run_monte_carlo(result, assumptions)

    table = result.projection.table
    metrics = {
        "ebitda": float(table.loc["ebit"].iloc[-1] + table.loc["da"].iloc[-1]),
        "ebit": float(table.loc["ebit"].iloc[-1]),
        "revenue": float(table.loc["revenue"].iloc[-1]),
    }
    implied = comps.implied_values(metrics) if comps.usable_for_terminal else None
    field = football_field(result, sensitivity, implied, monte.stats if monte else None)

    _print_report(result, comps, monte, field)

    if not no_excel:
        out_dir = Path(out)
        path = out_dir / f"{ticker}_{scenario}_dcf.xlsx"
        build_excel_model(
            result,
            path,
            financials=financials,
            comps=comps,
            monte_carlo=monte.stats if monte else None,
            football=field,
        )
        click.secho(f"\nModel written to {path}", fg="green")

        if json_out:
            json_path = path.with_suffix(".json")
            payload = {
                "summary": _jsonable(result.summary()),
                "comps_medians": _jsonable(comps.medians),
                "monte_carlo": _jsonable(monte.stats) if monte else None,
                "warnings": result.warnings,
                "comps_notes": comps.notes(),
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            click.secho(f"Summary written to {json_path}", fg="green")


# ------------------------------------------------------------------ scenarios


@cli.command()
@_common
@click.option(
    "--sbc-method",
    type=click.Choice(["expense", "dilute"]),
    default=None,
    help="Override the stock-compensation treatment.",
)
def scenarios(
    ticker: str,
    use_offline: bool,
    offline_dir: str,
    config: str,
    sbc_method: str | None,
    fx_rate: float | None,
    auto_fx: bool,
    force_sector: bool,
) -> None:
    """Run bear, base and bull side by side."""
    ticker = ticker.upper().strip()
    rows = []
    shared = _currency_overrides(fx_rate, auto_fx, force_sector)
    try:
        # The statements are fetched once and shared across all three scenarios, so
        # the currency settings have to be resolved before the fetch rather than per
        # scenario -- otherwise bear, base and bull could end up on different bases.
        base_assumptions = DCFAssumptions.from_yaml(
            config, scenario="base", overrides=_clean(dict(shared))
        )
        financials = YFinanceClient(
            ticker, offline_mode=use_offline, offline_path=Path(offline_dir) / ticker
        ).get_financials(currency=base_assumptions.currency)

        for scenario in ("bear", "base", "bull"):
            overrides: dict[str, Any] = dict(shared)
            if sbc_method:
                overrides["sbc"] = {"method": sbc_method}
            assumptions = DCFAssumptions.from_yaml(
                config, scenario=scenario, overrides=_clean(overrides) or None
            )
            comps = CompsEngine(
                ticker,
                assumptions,
                offline_mode=use_offline,
                offline_dir=Path(offline_dir),
                target_financials=financials,
            ).run()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = DCFEngine(
                    financials, assumptions, comps.terminal_inputs(), ticker=ticker
                ).run()
            rows.append(
                {
                    "scenario": scenario,
                    "value per share": result.value_per_share,
                    "WACC": result.wacc.wacc,
                    "TV % of EV": result.terminal_value_share,
                    "upside": result.bridge.upside,
                }
            )
    except (ValuationError, ValidationError, ValueError, FileNotFoundError) as exc:
        raise click.ClickException(_explain(exc)) from exc

    frame = pd.DataFrame(rows).set_index("scenario")
    click.secho(f"\n{ticker} - scenario comparison", fg="cyan", bold=True)
    click.echo(
        frame.to_string(
            formatters={
                "value per share": "${:,.2f}".format,
                "WACC": "{:.2%}".format,
                "TV % of EV": "{:.1%}".format,
                "upside": lambda v: "n/a" if v is None or pd.isna(v) else f"{v:+.1%}",
            }
        )
    )
    price = rows and financials.info.get("currentPrice")
    if price:
        click.echo(f"\nMarket price: ${float(price):,.2f}")


# ------------------------------------------------------------------- snapshot


@cli.command()
@click.option("--ticker", "-t", default=None, help="Ticker to snapshot. Omit for the sample set.")
@click.option(
    "--offline-dir", default=str(DEFAULT_OFFLINE_DIR), type=click.Path(), show_default=True
)
def snapshot(ticker: str | None, offline_dir: str) -> None:
    """Fetch live data and write it as committed offline fixtures."""
    targets = [ticker.upper().strip()] if ticker else list(SAMPLE_TICKERS)
    for name in targets:
        click.echo(f"Fetching {name} ...")
        try:
            path = snapshot_ticker(name, offline_dir)
            click.secho(f"  wrote {path}", fg="green")
        except Exception as exc:
            click.secho(f"  failed: {exc}", fg="red")


# ------------------------------------------------------------------- printing


def _print_report(result: ValuationResult, comps, monte, field: pd.DataFrame) -> None:
    summary = result.summary()
    ticker = summary["ticker"]

    click.secho(f"\n{'=' * 68}", fg="cyan")
    click.secho(
        f"{ticker} - {summary['scenario']} case - SBC treated as {summary['sbc_method']}",
        fg="cyan",
        bold=True,
    )
    click.secho("=" * 68, fg="cyan")

    price = summary["current_price"]
    upside = summary["upside"]
    click.echo(f"\n  Implied value per share   ${summary['value_per_share']:>12,.2f}")
    if price:
        click.echo(f"  Current market price      ${price:>12,.2f}")
        colour = "green" if (upside or 0) > 0 else "red"
        click.secho(f"  Upside / (downside)        {upside:>12.1%}", fg=colour)

    click.echo(f"\n  WACC                       {summary['wacc']:>12.2%}")
    click.echo(f"    cost of equity           {summary['cost_of_equity']:>12.2%}")
    click.echo(f"    cost of debt             {summary['cost_of_debt']:>12.2%}")
    click.echo(f"    beta                     {summary['beta']:>12.2f}")

    click.echo(f"\n  Enterprise value          {_bn(summary['enterprise_value']):>13}")
    click.echo(f"  Equity value              {_bn(summary['equity_value']):>13}")
    click.echo(f"  Diluted shares            {summary['shares'] / 1e9:>12,.3f}bn")
    click.echo(f"  Terminal value % of EV     {summary['terminal_value_pct_ev']:>12.1%}")
    if summary["iterations"] > 1:
        click.echo(f"  Dilution solver converged in {summary['iterations']} iterations")

    click.secho("\n  Terminal value cross-check", bold=True)
    if summary["gordon_terminal_value"]:
        click.echo(f"    perpetuity growth method {_bn(summary['gordon_terminal_value']):>13}")
    if summary["exit_multiple_terminal_value"]:
        click.echo(
            f"    exit multiple method     {_bn(summary['exit_multiple_terminal_value']):>13}"
            f"  at {summary['exit_multiple_used']:.1f}x"
        )
    if summary["implied_exit_multiple"]:
        click.echo(f"    perpetuity implies       {summary['implied_exit_multiple']:>12.1f}x")
    if summary["implied_perpetuity_growth"] is not None:
        click.echo(f"    exit multiple implies    {summary['implied_perpetuity_growth']:>12.2%} growth")

    if monte is not None:
        click.secho("\n  Monte Carlo", bold=True)
        click.echo(
            f"    P10 / P50 / P90          ${monte.stats['p10']:,.2f} / "
            f"${monte.stats['p50']:,.2f} / ${monte.stats['p90']:,.2f}"
        )
        if "prob_above_market" in monte.stats:
            click.echo(
                f"    P(value > market price)  {monte.stats['prob_above_market']:>12.1%}"
            )

    if field is not None and not field.empty:
        click.secho("\n  Valuation range", bold=True)
        for _, row in field.iterrows():
            low, high = row["low"], row["high"]
            if pd.isna(low) or pd.isna(high):
                continue
            span = f"${low:,.2f} - ${high:,.2f}" if low != high else f"${low:,.2f}"
            click.echo(f"    {row['method']:<34} {span}")

    notes = comps.notes() if comps is not None else []
    if notes or result.warnings:
        click.secho("\n  Warnings and notes", fg="yellow", bold=True)
        for message in [*result.warnings, *notes]:
            click.secho(f"    - {message}", fg="yellow")


def _explain(exc: Exception) -> str:
    """Turn an internal exception into something a user can act on.

    Pydantic's ValidationError in particular is several screens of traceback that says
    nothing about which config file or flag caused it.
    """
    if isinstance(exc, ValidationError):
        lines = [
            f"  {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        ]
        return "invalid assumptions:\n" + "\n".join(lines)
    return str(exc)


def _resize_growth(config: str, scenario: str, years: int) -> list[float]:
    """Resample the configured revenue-growth series to a new horizon.

    Truncates when shortening; extends by holding the final configured rate flat, which
    is the conservative reading of a fade profile that has run out of explicit years.
    """
    source = DCFAssumptions.from_yaml(config, scenario=scenario)
    growth = source.projection.revenue_growth
    if not isinstance(growth, list):
        return [float(growth)] * years
    if len(growth) >= years:
        return [float(g) for g in growth[:years]]
    return [float(g) for g in growth] + [float(growth[-1])] * (years - len(growth))


def _bn(value: float) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"${value / 1e9:,.1f}bn"


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """Drop None leaves so they do not overwrite real configured values."""
    out: dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, dict):
            nested = _clean(val)
            if nested:
                out[key] = nested
        elif val is not None:
            out[key] = val
    return out


def _jsonable(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: _jsonable(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_jsonable(v) for v in data]
    if data is None or isinstance(data, (str, bool, int)):
        return data
    try:
        value = float(data)
    except (TypeError, ValueError):
        return str(data)
    return None if pd.isna(value) else value


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
