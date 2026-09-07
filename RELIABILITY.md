# Reliability and modeling contract

This is a scenario valuation engine for operating businesses. It calculates the consequences of stated assumptions. It does not establish a uniquely correct price, identify a moat, calibrate investment probabilities, or guarantee investment performance.

## Accounting

The default operating margin is **after stock-based compensation (SBC)**. An explicit SBC forecast is a memo within that margin under the expense method. Set `projection.margin_basis: before_sbc` to hold profit before compensation fixed and make additional compensation reduce reported profit. Changing the margin basis changes the meaning of the input.

Both SBC methods calculate cash taxes on operating earnings after SBC. The simplified tax model assumes full deductibility and unrestricted carryforward of operating losses. `projection.starting_nol` supplies opening losses. Negative operating earnings create losses rather than an immediate tax refund. Future positive earnings use those losses before cash tax is charged. Terminal cash flow removes temporary loss benefits. Expiry, utilization limits, grant settlement differences, deferred taxes and jurisdiction-specific rules are outside this schedule.

Adjusted unlevered FCF = after-SBC EBIT − cash taxes + D&A − capex − change in operating working capital. The dilution method adds back gross noncash SBC, preserving its cash-tax deduction. Repurchases used to offset issuance consume cash and do not create another tax deduction. Opening working capital comes from the historical balance sheet, even when the forecast working-capital ratio differs.

The dilution method assumes the issuance price grows at the cost of equity. Under this assumption its fixed point has an exact solution: `price = (equity value − PV of net future SBC at cost of equity) / opening shares`. Opening shares include existing option overhang. No positive solution is reported when the compensation claim exhausts equity. This is a stylized issuance policy, not a prediction of market prices. The two SBC approaches need not agree: their discount rates and cash-flow timing differ. Their gap is reconciled algebraically in tests.

Both terminal approaches use earnings **after** SBC. FCF5 Gordon perpetuates final adjusted FCF. Value driver uses `NOPAT × (1+g) × (1−g/RONIC) / (WACC−g)`, with RONIC defaulting to WACC. Exit multiples use operating EBIT plus D&A. The model reports the method actually selected and warns when the configured method is unusable. A finite negative distress estimate is not a negative realizable common-stock price; scenario blending applies limited liability at zero.

## Data and discount rates

Aliases are resolved separately for each period, and selected source aliases are recorded. A provider's EBIT alias may include non-operating items: reconcile it to filings. Comparable multiples use current operating metrics, not the target's terminal forecast. Target and peers use consistently defined cash, debt and minority interests; unsupported peer populations do not become a self-comparison.

Cash and investments are resolved per period. Restricted cash is deducted only when `bridge.cash_includes_restricted` explicitly states that the reported cash field includes it. Statement debt and the metadata fallback use the same basis in WACC and the equity bridge. Non-operating investments are included only according to the configured bridge policy; review overlaps and excluded items.

WACC can use current or target capital weights. Target weights unlever and relever beta. Sensitivity and simulation shocks move the discount rate directly, preserving other assumptions, including issuance-price growth and debt cost. `wacc.discount_rate_override` is explicit and traceable. The historical interest/book-debt ratio is only a default proxy; it uses the debt balance corresponding to the reported interest period and is not a current borrowing yield. Use `wacc.cost_of_debt_override` with a verified, currency-consistent marginal rate for decision use. Missing debt interest and other fallbacks are disclosed.

FX conversion scales statement amounts and metadata known to be in statement currency. It preserves the original currency and conversion rate. It does not convert rates, ADR share ratios, or future FX exposure. Offline execution never fetches FX: supply an explicit rate if conversion is needed.

## Cross-feature behavior

Monte Carlo uses the same tax schedule, terminal selection and dilution solution as the base engine. Zero uncertainty must reproduce the base value. Rejected draws are counted and the remaining distribution is explicitly conditional; its probabilities are not empirically calibrated. Reverse DCF uses bounded search and accepts a solution only after repricing it through the forward engine. Multiple roots are disclosed; an unavailable solution is not replaced with a plausible value.

Excel contains live forecast, tax-loss, WACC, terminal and dilution formulas. Peer observations and supporting reports are snapshots at generation. Rebuild to refresh market observations, reverse-DCF results, historical capital-efficiency analysis, Monte Carlo or comparable-company reports. Generation manifests describe the original inputs; they are not signatures of a workbook after manual editing.

Dashboard weighting clamps negative equity consistently with Python and refreshes derived discounts and illustrative values. Scenario weights are assumptions. ROIC is an accounting ratio, not proof of durable competitive advantage. Missing invested capital produces unavailable ROIC, never invented capital or a moat label.

## Provenance and refusal

Outputs include code, data and assumption fingerprints, generation time, source metadata, fiscal periods and available market timestamps. Legacy fixtures without capture dates remain explicitly undated. The supported valuation basis is an undated snapshot with annual forecast periods. `valuation_date` is refused because dated stub-period valuation is not implemented. Do not interpret these snapshots as current recommendations or point-in-time backtests.

New snapshots include file hashes, which are verified when loaded. These hashes detect inconsistency or changes relative to the manifest; they do not independently authenticate the provider or prove the financial statements correct. Verify material inputs against filings and document that reconciliation.

Non-finite assumptions, impossible correlation matrices, unsupported financial businesses and unusable valuation states are rejected. A dashboard with no successful valuations exits with an error. Partial dashboard failures are reported. Unknown inputs and rejected outcomes must remain visible rather than silently becoming zeros or successful artifacts.

## Validation standard

Required checks include hand-calculated accounting examples, regression tests for the audit findings, simulation-to-forward comparisons, reverse-DCF residuals, and independent evaluation of exported spreadsheet formulas. Passing these supports a narrower claim: fewer demonstrated implementation errors than the audited baseline. It does not demonstrate superiority over competing models or forecasting accuracy.

Growth, margins, capex, working capital and terminal reinvestment still require an economically consistent business forecast. Capex converging to D&A is an optional assumption, not a universal steady-state requirement. The decay of comparable multiples is a heuristic. Banks, deposit-taking lenders and insurers require a different valuation framework. Lease, pension, acquisition, minority and multi-class-share adjustments may require analyst-supplied inputs.
