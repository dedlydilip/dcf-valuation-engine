# Reliability validation — 7 September 2026

The repaired engine is demonstrably more internally consistent than the audited baseline, commit `8c866adf24dd0deaf492e84fd518811d7e2247c0`. This is evidence of corrected implementation and output integrity, not proof of predictive accuracy, perfection, or superiority to every other valuation model.

## Verified results

- Final offline suite: **358 passed, 1 skipped**, in 373.12 seconds. The skipped peer-selection test requires unavailable network access. Live integration tests are excluded by the repository's default configuration. The run emitted 492 warnings; model warnings are retained rather than suppressed. Baseline suite: 302 passed, 1 skipped. Passing the old tests had not detected the audited defects.
- All 57 stored company fixtures were attempted: 48 valuations and 9 explicit refusals. This verifies execution and refusal behavior, not the accuracy of the 48 estimates.
- Actual exported Excel formulas were independently evaluated using the `formulas` interpreter. Tests cover six terminal/SBC combinations, 300 sensitivity cells, a hand-calculated 23.625 valuation, eight live input changes and rejection of an invalid input.
- The three delivered AAPL, MSFT and TSLA workbooks each passed six headline comparisons against Python and had zero formula errors in that evaluator. Maximum per-share difference was below 1.5e-13. Native Microsoft Excel recalculation was unavailable because COM activation failed with a logon-session error; interpreter checks do not establish complete Excel compatibility.
- Dashboard JavaScript was executed in Node with a simulated document, checking negative-equity treatment, refreshed derived values and zero-weight reset. This is a runtime logic check, not a full browser interaction test.
- Ten workbook sheets were rendered for visual review. Year indices, discount periods, discount factors, currency labels and snapshot notes were corrected and checked.
- The JSON-only command with Excel and Monte Carlo disabled produced the requested summary successfully.

## Before and after

| Check | Audited behavior | Repaired behavior |
|---|---|---|
| Zero-uncertainty Monte Carlo, expense case | 26.5369 versus base 21.6447 | Difference from base 3.55e-15 |
| Zero-uncertainty Monte Carlo, dilution case | Shared base dilution distorted draw values | Difference from base 7.11e-15; sampled draws checked against full engine |
| Value-driver terminal SBC | Dilution terminal value inflated by 50% in audit probe | Both methods use after-SBC terminal earnings; terminal ratio 1.0 |
| Reverse growth, dilution case | Inferred -0.6781% for a 2.5% configured growth rate | Recovers 2.5% and verifies the forward residual |
| Target-leverage sensitivity | Displayed rate differed from rate used | Revised probe center equals base, 22.9521471889, at the actual 8.93884146% WACC |
| Dashboard negative-equity blend | JavaScript -5 versus Python 20 | Both apply limited liability and return 20 in regression case |
| Saved reports | Stale and inconsistent with source | Rebuilt outputs identify source, data and assumptions with fingerprints |

The sensitivity probe configuration differs from the earlier audit's configuration; its absolute price is not a before/after comparison. It tests the required center-equals-base invariant.

## Disposition of all 30 audit findings

Numbers correspond to the original `dcf-model-audit.md`. “Corrected” means the specified defect was addressed within the documented modeling contract; it does not imply exhaustive verification of every possible input.

| # | Repair | Evidence |
|---|---|---|
| 1 | Regenerate saved reports and attach run fingerprints | Delivered workbooks, JSON, dashboard and manifests |
| 2 | Use after-SBC NOPAT for value-driver terminal value | Terminal ratio probe; SBC/terminal regression tests |
| 3 | Implement value driver and RONIC in Excel | Independently calculated workbook combinations and live RONIC edit |
| 4 | Wire buyback, floor, target leverage and other core controls into formulas | Eight live-edit workbook cases |
| 5 | Align terminal selection and dilution refusal; reject invalid workbook inputs | Formula comparisons and invalid-edit regression |
| 6 | Preserve selected terminal policy in Monte Carlo | Zero-uncertainty combinations and sampled full-engine comparisons |
| 7 | Share loss carryforward cash-tax schedule across calculations | Hand tax schedule and sampled profit-crossing cases |
| 8 | Recalculate dilution for each simulation draw | Sampled draws compared with forward engine |
| 9 | Reject invalid correlation matrices explicitly | Invalid-correlation regression |
| 10 | Invert the full selected model and verify roots | Four reverse round-trip cases |
| 11 | Apply the displayed WACC directly without changing other discount assumptions | Both SBC sensitivity cases and Excel grids |
| 12 | Use latest actual operating metrics for comparable-company values | Comps tests and current metric selection |
| 13 | Align cash, debt, minority interest and operating EBITDA definitions | Comps and bridge tests |
| 14 | Preserve historical opening working capital under forecast overrides | Opening-NWC regression |
| 15 | Resolve cash alternatives per period and use the current balance | Cash fallback regression |
| 16 | Use one debt fallback for WACC and equity bridge | Metadata-debt regression |
| 17 | Define margin basis, gross SBC addback, buyback and loss taxation consistently | SBC tests, hand tax schedule and modeling contract |
| 18 | Revalidate method changes and make rejected assignment atomic | Assumption mutation tests |
| 19 | Reject non-finite and economically invalid inputs | Domain tests and invalid workbook edit |
| 20 | Remove invented capital and unverified moat claims | ROIC tests; unavailable-capital reporting |
| 21 | Remove blanket Credit Services exemption | Sector classification tests; only explicit payment-network exceptions |
| 22 | Retain warnings across engine, CLI and scenario reports | Output tests and delivered warning collections |
| 23 | Refuse automatic live FX in offline mode and fail empty dashboards | Currency tests and empty-dashboard regression |
| 24 | Align dashboard equity floor and refresh derived outputs | Executed JavaScript regression |
| 25 | Add hashes and provenance; label undated data; refuse unsupported as-of requests | Manifest tests and delivered manifests |
| 26 | Fill aliases per period and record selected sources | Normalization alias regression |
| 27 | Merge nested CLI overrides without losing forecast horizon | CLI override tests |
| 28 | Preserve original currency and avoid duplicate EV conversion | Currency conversion tests |
| 29 | Replace contradictory SBC warning with explicit accounting-basis guidance | Modeling contract and delivered warnings |
| 30 | Return operating EBIT plus D&A from terminal EBITDA property | Corrected property and terminal tests |

Additional repairs match historical interest expense to its debt period and warn that the resulting book yield is not a current borrowing rate. Missing matching debt produces an explicit fallback warning.

## Evidence and reproduction

`outputs/reliable-release/validation-measurements.json` records probes, fixture outcomes and sample manifests. `independent-workbook-checks.json` records actual workbook comparisons. `test-results.xml` is the final test report. Each sample has a companion manifest containing code, data and assumption fingerprints. The `code_sha256` covers Python files under `src`; it is not a hash of every repository file.

Install the project with `python -m pip install -e ".[dev,validation]"`, then run `python -m pytest` and `python -m ruff check src tests tools`. Node must be available for the dashboard runtime regression. `python tools/reliability_release.py` rebuilds the sample workbooks and measurement sweep. See README for dashboard commands.

## Remaining limits

The delivered sample values—AAPL 120.2420319184, MSFT 165.0570149811 and TSLA 36.2602488375—are outputs of stored, undated snapshots and configured assumptions. They are not current fair-value estimates. A complete independent filing-by-filing authentication of all source data was not performed. Hashes establish identity and detect change; they do not authenticate a provider's economic facts.

The engine uses annual forecasts without as-of/stub-period valuation. Unsupported dated valuation requests are refused. The simplified loss schedule assumes unrestricted carryforward and SBC deductibility; jurisdiction-specific tax limits and settlement rules require separate modeling. Dilution assumes issuance prices grow at the cost of equity. Forecasts, terminal growth, reinvestment, RONIC, discount rates and scenario distributions require judgment and are not empirically calibrated here. Lending and other unsupported businesses are refused rather than forced into an operating-company DCF.

Workbook core calculations update with supported inputs. Supporting diagnostics are saved at generation and must be rebuilt after edits; generation manifests do not describe subsequent manual edits. Small football-field widths for point estimates are display widths, not uncertainty intervals.

No competitor benchmark, out-of-sample valuation accuracy study or investment-performance study was conducted. The defensible conclusion is improved reliability against the audited baseline on the disclosed checks. Read `RELIABILITY.md` before relying on results.
