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
import math
import time
import warnings
import webbrowser
from pathlib import Path
from typing import Any

import click
import pandas as pd
from pydantic import ValidationError

from src.comps.comps_engine import CompsEngine
from src.dcf.dashboard import generate_dashboard_html
from src.dcf.engine import DCFEngine, ValuationResult
from src.dcf.moat import analyze_moat
from src.dcf.monte_carlo import run_monte_carlo
from src.dcf.reverse_dcf import ReverseDCFResult, solve_reverse_dcf
from src.dcf.scenario_blender import blend_scenarios
from src.dcf.sensitivity import football_field, wacc_vs_growth
from src.excel.builder import build_excel_model
from src.fetcher.rates import fetch_risk_free_rate
from src.fetcher.snapshot import SAMPLE_TICKERS, peer_universe_tickers, snapshot_ticker
from src.fetcher.yfinance_client import DEFAULT_OFFLINE_DIR, YFinanceClient
from src.models.assumptions import DCFAssumptions, deep_merge
from src.models.errors import ValuationError
from src.models.financials import field_value
from src.models.provenance import json_safe

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
    # The risk-free rate is a CAPM input, not company data, so it does not ride along
    # with the statements fetch the way beta does. It has to be asked for explicitly,
    # same as the FX rate above -- and for the same reason: the pinned config value
    # keeps a valuation reproducible, and a silently time-varying default would break
    # that guarantee for anyone re-running an old result.
    function = click.option(
        "--risk-free",
        type=float,
        default=None,
        help="Risk-free rate as a decimal, e.g. 0.0495 for 4.95%.",
    )(function)
    function = click.option(
        "--auto-risk-free",
        is_flag=True,
        help="Fetch the current 10-year Treasury yield from Yahoo instead of using the "
        "pinned config value. Needs network.",
    )(function)
    function = click.option(
        "--include-investments",
        is_flag=True,
        help="Include non-operating marketable investments (e.g. securities portfolios) in equity value.",
    )(function)
    function = click.option(
        "--value-driver",
        is_flag=True,
        help="Use McKinsey Value Driver formula (steady-state RONIC) for terminal value.",
    )(function)
    function = click.option(
        "--fade-capex",
        is_flag=True,
        help="Linearly move CapEx toward the configured D&A ratio over the forecast horizon.",
    )(function)
    return function


def _currency_overrides(fx_rate: float | None, auto_fx: bool, force_sector: bool) -> dict[str, Any]:
    """Turn the currency and sector flags into config overrides."""
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


def _risk_free_override(
    risk_free: float | None, auto_risk_free: bool, use_offline: bool
) -> dict[str, Any]:
    """Resolve --risk-free / --auto-risk-free into a wacc.risk_free_rate override.

    Mirrors `_currency_overrides`: both flags set is a contradiction (supply a rate or
    fetch one, not both), auto-fetching offline is a contradiction (there is no network
    to fetch from), and neither flag leaves the pinned config value untouched.
    """
    if risk_free is not None and auto_risk_free:
        raise click.ClickException(
            "--risk-free and --auto-risk-free are both set. Supply a rate or fetch one."
        )
    if auto_risk_free:
        if use_offline:
            raise click.ClickException(
                "--use-offline cannot fetch the risk-free rate. Supply a dated --risk-free "
                "instead, or drop --auto-risk-free to use the pinned config value."
            )
        try:
            rate = fetch_risk_free_rate()
        except ValuationError as exc:
            raise click.ClickException(str(exc)) from exc
        click.secho(
            f"Fetched risk-free rate: {rate:.3%} (10-year Treasury, live)", fg="yellow"
        )
        return {"wacc": {"risk_free_rate": rate}}
    if risk_free is not None:
        return {"wacc": {"risk_free_rate": risk_free}}
    return {}


def _shared_overrides(
    fx_rate: float | None,
    auto_fx: bool,
    force_sector: bool,
    include_investments: bool = False,
    value_driver: bool = False,
    fade_capex: bool = False,
    risk_free: float | None = None,
    auto_risk_free: bool = False,
    use_offline: bool = False,
) -> dict[str, Any]:
    out = _currency_overrides(fx_rate, auto_fx, force_sector)
    if include_investments:
        out.setdefault("bridge", {})["include_investments"] = True
    if value_driver:
        out.setdefault("terminal", {})["terminal_fcf_mode"] = "value_driver"
    if fade_capex:
        out.setdefault("projection", {})["fade_capex_to_da"] = True
    rf_override = _risk_free_override(risk_free, auto_risk_free, use_offline)
    if rf_override:
        out = deep_merge(out, rf_override)
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
@click.option("--peers", default=None, help="Comma-separated peer tickers for the comps analysis.")
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
    include_investments: bool,
    value_driver: bool,
    fade_capex: bool,
    risk_free: float | None,
    auto_risk_free: bool,
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
    overrides = deep_merge(
        overrides,
        _shared_overrides(
            fx_rate,
            auto_fx,
            force_sector,
            include_investments,
            value_driver,
            fade_capex,
            risk_free,
            auto_risk_free,
            use_offline,
        ),
    )

    try:
        assumptions = DCFAssumptions.from_yaml(
            config, scenario=scenario, overrides=_clean(overrides)
        )
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

    metrics = {
        "ebitda": field_value(financials, "ebit") + field_value(financials, "da"),
        "ebit": field_value(financials, "ebit"),
        "revenue": field_value(financials, "revenue"),
    }
    implied = comps.implied_values(metrics) if comps.usable_for_terminal else None
    field = football_field(result, sensitivity, implied, monte.stats if monte else None)
    moat = analyze_moat(financials, result)

    _print_report(result, comps, monte, field, moat=moat)

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
        json_path = Path(out) / f"{ticker}_{scenario}_dcf.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "summary": _jsonable(result.summary()),
            "moat": _jsonable(moat.summary()),
            "comps_medians": _jsonable(comps.medians),
            "monte_carlo": _jsonable({**monte.stats, "diagnostics": monte.diagnostics})
            if monte
            else None,
            "warnings": result.warnings,
            "comps_notes": comps.notes(),
        }
        json_path.write_text(
            json.dumps(json_safe(payload), indent=2, allow_nan=False), encoding="utf-8"
        )
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
@click.option(
    "--weights",
    default=None,
    help="Comma-separated scenario weights (bear,base,bull), e.g. '0.25,0.50,0.25'.",
)
def scenarios(
    ticker: str,
    use_offline: bool,
    offline_dir: str,
    config: str,
    sbc_method: str | None,
    weights: str | None,
    fx_rate: float | None,
    auto_fx: bool,
    force_sector: bool,
    include_investments: bool,
    value_driver: bool,
    fade_capex: bool,
    risk_free: float | None,
    auto_risk_free: bool,
) -> None:
    """Run bear, base and bull side by side with probability weighting and margin of safety."""
    ticker = ticker.upper().strip()
    rows = []
    shared = _shared_overrides(
        fx_rate,
        auto_fx,
        force_sector,
        include_investments,
        value_driver,
        fade_capex,
        risk_free,
        auto_risk_free,
        use_offline,
    )
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

    # Probability weighting & Margin of Safety
    weights_dict = None
    if weights:
        try:
            parts = [float(p.strip()) for p in weights.split(",") if p.strip()]
            if len(parts) != 3:
                raise ValueError
            weights_dict = {"bear": parts[0], "base": parts[1], "bull": parts[2]}
        except ValueError as exc:
            raise click.ClickException(
                "Invalid --weights format: expected 3 comma-separated numbers (e.g. '0.25,0.50,0.25')"
            ) from exc

    scenario_vals = {r["scenario"]: r["value per share"] for r in rows}
    price_val = float(price) if price else None
    blend = blend_scenarios(scenario_vals, weights_dict, current_price=price_val)

    click.secho("\nProbability-weighted Valuation & Margin of Safety", fg="cyan", bold=True)
    click.echo(
        f"  Weights applied: Bear {blend.normalized_weights['bear']:.0%}, "
        f"Base {blend.normalized_weights['base']:.0%}, Bull {blend.normalized_weights['bull']:.0%}"
    )
    click.echo(f"  Expected Fair Value:       ${blend.expected_value:>10,.2f}")
    if blend.discount_to_expected is not None:
        disc_color = "green" if blend.discount_to_expected >= 0 else "red"
        click.secho(
            f"  Margin of Safety vs Tape:  {blend.discount_to_expected:>10.1%}", fg=disc_color
        )
        click.echo(f"  Verdict:                   {blend.verdict}")
        if blend.asymmetry_ratio is not None:
            if math.isinf(blend.asymmetry_ratio):
                click.echo(
                    "  Risk/Reward Asymmetry:     Pure Upside (Current price below bear case)"
                )
            else:
                click.echo(
                    f"  Risk/Reward Asymmetry:     {blend.asymmetry_ratio:>10.2f}x (Bull upside / Bear downside)"
                )

    click.echo("\n  Target Buy Prices (Margin of Safety Hurdles):")
    for tier, target in blend.target_buy_prices.items():
        click.echo(f"    {tier:<38} ${target:>8,.2f}")


# --------------------------------------------------------------------- reverse


@cli.command()
@_common
@click.option("--price", type=float, default=None, help="Target stock price to back-solve for.")
def reverse(
    ticker: str,
    use_offline: bool,
    offline_dir: str,
    config: str,
    price: float | None,
    fx_rate: float | None,
    auto_fx: bool,
    force_sector: bool,
    include_investments: bool,
    value_driver: bool,
    fade_capex: bool,
    risk_free: float | None,
    auto_risk_free: bool,
) -> None:
    """Reverse DCF: extract market-implied growth, margins, and terminal assumptions."""
    ticker = ticker.upper().strip()
    shared = _shared_overrides(
        fx_rate,
        auto_fx,
        force_sector,
        include_investments,
        value_driver,
        fade_capex,
        risk_free,
        auto_risk_free,
        use_offline,
    )
    try:
        assumptions = DCFAssumptions.from_yaml(
            config, scenario="base", overrides=_clean(dict(shared))
        )
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

        result = solve_reverse_dcf(
            financials,
            assumptions,
            comps.terminal_inputs(),
            ticker=ticker,
            target_price=price,
        )
    except (ValuationError, ValidationError, ValueError, FileNotFoundError) as exc:
        raise click.ClickException(_explain(exc)) from exc

    _print_reverse_report(result)


# ----------------------------------------------------------------- dashboard


@cli.command()
@click.option(
    "--tickers",
    "-t",
    default="AAPL,MSFT,TSLA,SAP,TSM",
    show_default=True,
    help="Comma-separated tickers to include in the dashboard.",
)
@click.option(
    "--use-offline",
    is_flag=True,
    help="Use committed sample data instead of the live API.",
)
@click.option(
    "--offline-dir",
    default=str(DEFAULT_OFFLINE_DIR),
    type=click.Path(),
    show_default=True,
    help="Directory holding offline fixtures.",
)
@click.option(
    "--config",
    default="config/assumptions.yaml",
    type=click.Path(),
    show_default=True,
    help="Base assumptions file.",
)
@click.option(
    "--out",
    default="outputs/dashboard.html",
    type=click.Path(),
    show_default=True,
    help="Path to write the interactive HTML dashboard.",
)
@click.option(
    "--open-browser",
    is_flag=True,
    help="Automatically open the generated dashboard in your default browser.",
)
@click.option(
    "--include-investments",
    is_flag=True,
    help="Include non-operating marketable investments in the equity value bridge.",
)
@click.option(
    "--value-driver",
    is_flag=True,
    help="Use McKinsey Value Driver formula (steady-state RONIC) for terminal value.",
)
@click.option(
    "--fade-capex",
    is_flag=True,
    help="Linearly move CapEx toward the configured D&A ratio over the forecast horizon.",
)
@click.option(
    "--risk-free",
    type=float,
    default=None,
    help="Risk-free rate as a decimal, e.g. 0.0495 for 4.95%.",
)
@click.option(
    "--auto-risk-free",
    is_flag=True,
    help="Fetch the current 10-year Treasury yield from Yahoo instead of using the "
    "pinned config value. Needs network.",
)
def dashboard(
    tickers: str,
    use_offline: bool,
    offline_dir: str,
    config: str,
    out: str,
    open_browser: bool,
    include_investments: bool,
    value_driver: bool,
    fade_capex: bool,
    risk_free: float | None,
    auto_risk_free: bool,
) -> None:
    """Generate multi-company valuation dashboard (terminal table + interactive HTML)."""
    t_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    click.secho(
        f"\nGenerating Valuation Dashboard for {len(t_list)} companies...", fg="cyan", bold=True
    )

    # Resolved once, ahead of the per-ticker loop -- one dashboard run should use one
    # rate for every company, not fetch (or fail fetching) 5 separate times.
    rf_override = _risk_free_override(risk_free, auto_risk_free, use_offline)

    data_payload: dict[str, Any] = {}
    failed_tickers: dict[str, str] = {}
    table_rows: list[dict[str, Any]] = []

    for ticker in t_list:
        try:
            shared: dict[str, Any] = {}
            if include_investments:
                shared.setdefault("bridge", {})["include_investments"] = True
            if value_driver:
                shared.setdefault("terminal", {})["terminal_fcf_mode"] = "value_driver"
            if fade_capex:
                shared.setdefault("projection", {})["fade_capex_to_da"] = True
            shared = deep_merge(shared, rf_override)

            base_assump = DCFAssumptions.from_yaml(
                config, scenario="base", overrides=_clean(shared) or None
            )
            client = YFinanceClient(
                ticker, offline_mode=use_offline, offline_path=Path(offline_dir) / ticker
            )
            financials = client.get_financials(currency=base_assump.currency)
            comps = CompsEngine(
                ticker,
                base_assump,
                offline_mode=use_offline,
                offline_dir=Path(offline_dir),
                target_financials=financials,
            ).run()

            scenario_vals: dict[str, float] = {}
            scenario_warnings = {}
            scenario_manifests = {}
            for sc in ("bear", "base", "bull"):
                sc_assump = DCFAssumptions.from_yaml(
                    config, scenario=sc, overrides=_clean(shared) or None
                )
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    sc_res = DCFEngine(
                        financials, sc_assump, comps.terminal_inputs(), ticker=ticker
                    ).run()
                scenario_vals[sc] = sc_res.value_per_share
                scenario_warnings[sc] = sc_res.warnings
                scenario_manifests[sc] = sc_res.manifest

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                base_res = DCFEngine(
                    financials, base_assump, comps.terminal_inputs(), ticker=ticker
                ).run()
                moat = analyze_moat(financials, base_res)
                rev = solve_reverse_dcf(
                    financials, base_assump, comps.terminal_inputs(), ticker=ticker
                )

            price = base_res.bridge.current_price
            blend = blend_scenarios(scenario_vals, current_price=price)

            data_payload[ticker] = {
                "ticker": ticker,
                "warnings": list(
                    dict.fromkeys(
                        base_res.warnings
                        + comps.notes()
                        + [f"{sc}: {w}" for sc, notes in scenario_warnings.items() for w in notes]
                    )
                ),
                "scenario_manifests": scenario_manifests,
                "manifest": base_res.manifest,
                "current_price": price,
                "currency": financials.currency,
                "base_value": base_res.value_per_share,
                "bear_value": scenario_vals["bear"],
                "bull_value": scenario_vals["bull"],
                "upside": base_res.bridge.upside,
                "wacc": base_res.wacc.wacc,
                "cost_of_equity": base_res.wacc.cost_of_equity,
                "cost_of_debt": base_res.wacc.cost_of_debt,
                "beta": base_res.wacc.beta,
                "tv_pct_ev": base_res.terminal_value_share,
                "ev": base_res.enterprise_value,
                "equity_value": base_res.bridge.equity_value,
                "shares": base_res.bridge.shares,
                "moat": {
                    "invested_capital": moat.invested_capital_base,
                    "nopat": moat.nopat_base,
                    "roic": moat.roic_base,
                    "spread": moat.economic_spread,
                    "rating": moat.moat_rating,
                    "diagnostics": moat.diagnostics,
                },
                "reverse_dcf": {
                    "implied_rev_growth": rev.implied_revenue_growth_cagr,
                    "implied_rev_status": rev.implied_revenue_status,
                    "implied_margin": rev.implied_ebit_margin,
                    "implied_margin_status": rev.implied_margin_status,
                    "implied_perpetuity_g": rev.implied_perpetuity_growth,
                    "implied_growth_status": rev.implied_growth_status,
                    "fcf_yield_trailing": rev.fcf_yield_trailing,
                    "fcf_yield_forward": rev.fcf_yield_forward,
                    "warnings": rev.warnings,
                },
                "blended": {
                    "expected_value": blend.expected_value,
                    "discount_to_expected": blend.discount_to_expected,
                    "asymmetry_ratio": blend.asymmetry_ratio,
                    "verdict": blend.verdict,
                    "target_buys": blend.target_buy_prices,
                },
            }

            table_rows.append(
                {
                    "Ticker": ticker,
                    "Market Price": f"${price:,.2f}" if price else "n/a",
                    "Base DCF": f"${base_res.value_per_share:,.2f}",
                    "Blended Fair": f"${blend.expected_value:,.2f}",
                    "WACC": f"{base_res.wacc.wacc:.1%}",
                    "ROIC (Spread)": f"{moat.roic_base:.1%} ({moat.economic_spread:+.1%})",
                    "Implied CAGR": f"{rev.implied_revenue_growth_cagr:.1%}"
                    if rev.implied_revenue_growth_cagr is not None
                    else rev.implied_revenue_status,
                    "Verdict": blend.verdict,
                }
            )
        except Exception as exc:
            failed_tickers[ticker] = str(exc)
            click.secho(f"  Warning: skipping {ticker}: {exc}", fg="yellow")

    if table_rows:
        df = pd.DataFrame(table_rows).set_index("Ticker")
        click.echo()
        click.echo(df.to_string())

    if not data_payload:
        raise click.ClickException(
            "No requested companies could be valued: "
            + "; ".join(f"{t}: {e}" for t, e in failed_tickers.items())
        )
    for item in data_payload.values():
        item["failed_companies"] = failed_tickers
    out_path = Path(out)
    generate_dashboard_html(json_safe(data_payload), out_path)
    out_path.with_suffix(".json").write_text(
        json.dumps(json_safe(data_payload), indent=2, allow_nan=False), encoding="utf-8"
    )
    click.secho(
        f"\nInteractive HTML dashboard generated at: {out_path.resolve()}", fg="green", bold=True
    )

    if open_browser:
        webbrowser.open(out_path.resolve().as_uri())


# ------------------------------------------------------------------- snapshot


@cli.command()
@click.option("--ticker", "-t", default=None, help="One ticker to snapshot.")
@click.option("--tickers", default=None, help="Comma-separated tickers to snapshot.")
@click.option(
    "--peer-universe",
    is_flag=True,
    help="Snapshot every ticker the comps fallback maps can request.",
)
@click.option(
    "--delay",
    type=float,
    default=1.5,
    show_default=True,
    help="Seconds between fetches. Yahoo is an unofficial endpoint and rate-limits.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite fixtures that already exist. Off by default: a refresh moves market "
    "data, and the committed fixtures are what the pinned numbers reproduce from.",
)
@click.option(
    "--offline-dir", default=str(DEFAULT_OFFLINE_DIR), type=click.Path(), show_default=True
)
@click.option(
    "--source",
    type=click.Choice(["yahoo", "edgar"]),
    default="yahoo",
    show_default=True,
    help="Where the STATEMENTS come from. 'edgar' reads the company's own SEC filings "
    "(10-K/20-F XBRL facts); the info payload stays on Yahoo either way, because EDGAR "
    "carries no price, market cap or beta.",
)
def snapshot(
    ticker: str | None,
    tickers: str | None,
    peer_universe: bool,
    delay: float,
    force: bool,
    offline_dir: str,
    source: str,
) -> None:
    """Fetch live data and write it as committed offline fixtures.

    Existing fixtures are left alone unless `--force` is passed. Re-fetching is not
    free: `--peer-universe` includes AAPL, MSFT and TSLA, and refreshing those moved
    Apple's market capitalisation 2.7%, which shifted the WACC capital-structure
    weights and the reported value per share -- invalidating figures pinned across the
    README, the changelog, three test files and the Excel cross-check. Refreshing a
    reference fixture should be a decision, not a side effect.
    """
    if peer_universe:
        targets = list(peer_universe_tickers())
    elif tickers:
        targets = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    elif ticker:
        targets = [ticker.upper().strip()]
    else:
        targets = list(SAMPLE_TICKERS)

    written: list[str] = []
    failed: dict[str, str] = {}
    skipped: list[str] = []

    if not force:
        existing = (
            {p.name for p in Path(offline_dir).glob("*") if p.is_dir()}
            if Path(offline_dir).exists()
            else set()
        )
        skipped = [t for t in targets if t in existing]
        targets = [t for t in targets if t not in existing]
        if skipped:
            click.secho(
                f"Skipping {len(skipped)} existing fixture(s); pass --force to refresh: "
                f"{', '.join(skipped)}",
                fg="yellow",
            )

    for index, name in enumerate(targets, start=1):
        click.echo(f"[{index}/{len(targets)}] Fetching {name} ...")
        # One retry, because a single throttled request should not cost a fixture.
        # A half-fetched set is worse than a short one: the gaps are invisible later.
        for attempt in (1, 2):
            try:
                path = snapshot_ticker(name, offline_dir, source)
                click.secho(f"  wrote {path}", fg="green")
                written.append(name)
                break
            except Exception as exc:
                if attempt == 1:
                    click.secho(f"  retrying after error: {exc}", fg="yellow")
                    time.sleep(delay * 3)
                else:
                    click.secho(f"  failed: {exc}", fg="red")
                    failed[name] = str(exc)
        if index < len(targets):
            time.sleep(delay)

    click.echo()
    click.secho(f"Wrote {len(written)} fixture(s).", fg="green", bold=True)
    if failed:
        click.secho(f"{len(failed)} failed and were NOT written:", fg="red", bold=True)
        for name, reason in failed.items():
            click.secho(f"  {name}: {reason[:100]}", fg="red")


# ------------------------------------------------------------------- printing


def _print_report(result: ValuationResult, comps, monte, field: pd.DataFrame, moat=None) -> None:
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

    if moat is not None:
        click.secho("\n  Economic Moat & Capital Efficiency (ROIC vs. WACC)", bold=True)
        click.echo(f"    Base Invested Capital    {_bn(moat.invested_capital_base):>13}")
        click.echo(f"    Base ROIC                {moat.roic_base:>12.1%}")
        spread_color = "green" if moat.economic_spread >= 0 else "red"
        click.secho(f"    Economic Spread (vs WACC){moat.economic_spread:>12.1%}", fg=spread_color)
        click.echo(f"    Moat Assessment          {moat.moat_rating}")
        for diag in moat.diagnostics:
            click.echo(f"      * {diag}")

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
        click.echo(
            f"    exit multiple implies    {summary['implied_perpetuity_growth']:>12.2%} growth"
        )

    if monte is not None:
        click.secho("\n  Monte Carlo", bold=True)
        click.echo(
            f"    P10 / P50 / P90          ${monte.stats['p10']:,.2f} / "
            f"${monte.stats['p50']:,.2f} / ${monte.stats['p90']:,.2f}"
        )
        click.echo(
            f"    Valid draws: {int(monte.stats['n_valid'])}; rejected: {int(monte.stats['n_rejected'])}. Probabilities are conditional on assumptions and valid draws."
        )
        if "prob_above_market" in monte.stats:
            click.echo(f"    P(value > market price)  {monte.stats['prob_above_market']:>12.1%}")

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


def _print_reverse_report(result: ReverseDCFResult) -> None:
    click.secho(f"\n{'=' * 68}", fg="cyan")
    click.secho(
        f"{result.ticker} - Reverse DCF / Market Expectations Analysis",
        fg="cyan",
        bold=True,
    )
    click.secho("=" * 68, fg="cyan")

    click.echo(f"\n  Current Market Price:       ${result.current_price:>12,.2f}")
    click.echo(f"  Base Model DCF Value:       ${result.base_value_per_share:>12,.2f}")
    spread = (
        (result.current_price / result.base_value_per_share - 1.0)
        if result.base_value_per_share > 0
        else 0.0
    )
    color = "yellow" if abs(spread) > 0.3 else "green"
    click.secho(f"  Market Premium / (Discount): {spread:>12.1%}", fg=color)
    click.echo(f"  Discount Rate (WACC):        {result.wacc:>12.2%}")

    click.secho("\n  Market-Implied Expectations Baked into Current Price:", bold=True)

    if result.implied_revenue_growth_cagr is not None:
        click.echo(f"    Implied 5-Year Revenue CAGR: {result.implied_revenue_growth_cagr:>10.2%}")
    else:
        click.echo(f"    Implied 5-Year Revenue CAGR: {result.implied_revenue_status}")

    if result.implied_ebit_margin is not None:
        click.echo(f"    Implied Operating Margin:   {result.implied_ebit_margin:>10.2%}")
    else:
        click.echo(f"    Implied Operating Margin:   {result.implied_margin_status}")

    if result.implied_perpetuity_growth is not None:
        growth_str = f"{result.implied_perpetuity_growth:>10.2%}"
        if result.implied_perpetuity_growth > 0.035:
            click.secho(
                f"    Implied Perpetuity Growth:  {growth_str}  (Above 3.5% GDP ceiling!)", fg="red"
            )
        else:
            click.echo(f"    Implied Perpetuity Growth:  {growth_str}")
    else:
        click.echo(f"    Implied Perpetuity Growth:  {result.implied_growth_status}")

    click.secho("\n  Cash Flow Yields at Current Market Price:", bold=True)
    if result.fcf_yield_trailing is not None:
        click.echo(f"    Trailing Reported FCF Yield: {result.fcf_yield_trailing:>10.2%}")
    if result.fcf_yield_forward is not None:
        click.echo(f"    Forward Year-1 FCF Yield:    {result.fcf_yield_forward:>10.2%}")

    if result.warnings:
        click.secho("\n  Expectation Diagnostics:", fg="yellow", bold=True)
        for warn in result.warnings:
            click.secho(f"    - {warn}", fg="yellow")


def _explain(exc: Exception) -> str:
    """Turn an internal exception into something a user can act on.

    Pydantic's ValidationError in particular is several screens of traceback that says
    nothing about which config file or flag caused it.
    """
    if isinstance(exc, ValidationError):
        lines = [f"  {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()]
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
