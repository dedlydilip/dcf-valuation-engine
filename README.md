# DCF Valuation Engine

A discounted-cash-flow and comparable-company scenario model with Python calculations, live-formula Excel exports and an interactive dashboard. Outputs show modeled values under stated assumptions, with warnings and source fingerprints. They are not verified fair values or investment recommendations.

The reliability revision corrects accounting and cross-feature inconsistencies identified in an audit of commit `8c866ad`. Read [RELIABILITY.md](RELIABILITY.md) for the accounting contract, supported scope and remaining limitations. Validation evidence is supplied with the release; no claim of superiority over competing models or forecasting accuracy is made.

## Run locally

Requires Python 3.11 or newer.

```bash
pip install -e ".[dev,validation]"
python run.py value --ticker AAPL --use-offline
python run.py reverse --ticker AAPL --use-offline
python run.py scenarios --ticker AAPL --use-offline
python run.py dashboard --use-offline
pytest
```

Offline runs use bundled sample data. Legacy fixtures have unknown capture dates and must not be treated as current market observations. Refresh inputs separately and check material figures against company filings.

```bash
python run.py snapshot --ticker NVDA
python run.py value --ticker MSFT --sbc-method dilute
python run.py value --ticker SAP --auto-fx
```

Those commands use the network. For an offline currency conversion, provide an explicit `--fx-rate` in units of quote currency per statement-currency unit. Automatic FX lookup is prohibited in offline mode. Conversion of money amounts does not reconcile borrowing-rate currencies, ADR ratios or future exchange rates.

Run `python run.py --help` and each command's `--help` for available options. Configuration lives in `config/assumptions.yaml`; scenario and command overrides merge recursively.

## Calculations

The forecast projects revenue, operating earnings, cash taxes, depreciation, capex and operating working capital. Historical opening working capital is preserved when forecast ratios change. Both stock-compensation treatments use cash taxes after SBC. The expense method holds the opening share count plus overhang fixed. The dilution method adds back gross noncash SBC and accounts for modeled future issuance and cash used for repurchases. Its issuance-price assumption permits an exact closed-form solution. The methods are alternative approximations and are not guaranteed to produce identical values.

Default margins are after SBC. Set `projection.margin_basis: before_sbc` when an incremental SBC forecast should reduce operating profit. `projection.starting_nol` supplies opening tax losses; the simplified carryforward schedule has no expiry or annual utilization limits. Terminal cash flow does not perpetuate temporary NOL benefits.

Terminal choices are FCF5 Gordon, a reinvestment-aware value-driver formula, and exit multiples. The selected method and substitutions are reported. Value driver uses earnings after SBC and an explicit RONIC assumption, defaulting to WACC. Dynamic multiple decay is a configurable heuristic, not an empirically estimated law. Capex fading to D&A is optional and is not sufficient by itself to justify long-run growth.

WACC supports current and target capital weights, optional cost-of-equity flooring and a direct discount-rate override. Historical book debt cost is a proxy, not a current market yield. Supply a verified `wacc.cost_of_debt_override` for decision use. Market data, currency basis, debt and all equity-bridge adjustments require review.

Monte Carlo uses the same accounting, terminal selection and dilution policy as the base valuation. Invalid draws are counted; reported probabilities are conditional on valid draws and assumed distributions. Reverse DCF verifies each inferred assumption by repricing through the forward engine. Sensitivities vary WACC directly while preserving the other assumptions.

## Outputs and traceability

Excel exports include Summary, Inputs, WACC, DCF, Sensitivity, SensitivityCalc, Comps, FootballField, Historicals and Provenance. Core valuation formulas recalculate when supported inputs change. Supporting reports such as reverse DCF, historical ROIC, peer observations and simulation summaries are snapshots: rebuild to refresh them. The manifest describes the workbook at generation, not after manual edits.

JSON and dashboard outputs preserve warnings and identify failed tickers. The dashboard clamps negative common-equity scenarios at zero and updates its blended value and illustrative discounts together. These discounts and scenario weights are analyst assumptions, not buy signals. Missing invested capital produces unavailable ROIC; accounting returns never establish a moat.

Manifests contain generation time, code/data/configuration hashes, fiscal periods, source metadata, currency conversion and available quote timestamps. Undated input data remains undated. Dated stub-period valuation and point-in-time backtesting are not implemented; specifying `valuation_date` is refused.

## Validation

```bash
pytest
pytest tests/test_reliability.py tests/test_workbook_calculation.py
```

The optional `validation` dependencies enable an independent interpreter of the actual exported Excel formulas. This is distinct from native Microsoft Excel recalculation. Core tests include hand-calculated financial cases, method reconciliation, loss-to-profit tax transitions, simulation draw comparisons, reverse-DCF round trips, input validation and source-contract checks. Live network tests are excluded by default.

The model refuses unsupported lending/insurance businesses and invalid calculation states. It still requires company-specific judgment, filing reconciliation and plausible forecasts. Passing tests demonstrates specified implementation behavior; it cannot establish perfect predictions or eliminate unknown defects.
