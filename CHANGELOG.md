# Changelog

Dated by the audit that prompted each round rather than by release, because there have
been no releases. Every entry names what moved and by how much.

## Semiconductor peer bucket — September 2026

Found by manually valuing Micron (MU) after the sector sweep above shipped. Nothing
crashed — the model produced a confident number with a plausible-looking story — but the
peer set behind it was wrong. `SECTOR_PEERS` keys on Yahoo's *sector* field, and every
semiconductor name (NVDA, AMD, TSM, QCOM, INTC, AVGO, TXN, MU itself) is filed under the
single broad sector "Technology", alongside AAPL, MSFT, GOOGL and META.

Concretely: Micron's peer-median growth came back at 16.2%, dragged up by that megacap
basket, against Micron's own 4% terminal growth. At `decay_turns_per_pp = 2.0` that 12
point gap decayed the exit multiple to **-6.9x**, caught by the mature-industry floor but
only after the damage was done.

`INDUSTRY_PEERS` in `src/comps/comps_engine.py` is now checked first, on Yahoo's
finer-grained `industry` field, before falling back to `SECTOR_PEERS`. Two buckets added:
Semiconductors and Semiconductor Equipment & Materials. Micron's peers are now NVDA, AMD,
QCOM, INTC, AVGO, TXN; the raw decayed multiple moves from -6.9x to +2.0x, and the
comparable-companies range moves from $278–474 to $450–855, closer to where the market
actually prices it. `tests/test_comps_peer_selection.py` pins the industry-before-sector
lookup order and the sector-bucket fallback for companies with no dedicated industry.

No headline number moved — AAPL, MSFT and TSLA are all non-semiconductor and untouched.

## Sector sweep — September 2026

A sweep across 31 live tickers in 11 sectors, run because "does this work for any ticker?"
turned out not to have an evidenced answer. It found two defects that the committed
fixtures could not have exposed: AAPL, MSFT and TSLA are all US non-financial operating
companies, which is the one corner of the input space where both are invisible.

23 of the 31 now produce a valuation; 8 refuse. Every refusal previously produced a
confident number that was wrong. Test count 202 -> 244. Headline numbers unchanged.

### Fixed — mixed-currency companies were silently incoherent

Yahoo reports the statements in the company's reporting currency and the price, market
capitalisation and share count in the listing currency. Nothing reconciled them:

```
TM    JPY / USD   $79,467 per share vs $198   WACC 0.43%
BABA  CNY / USD   $1,571 vs $112
TSM   TWD / USD   $5,294 vs $416
SAP   EUR / USD   $176.90 vs $210             <- the dangerous one
```

SAP is why this is a critical and not a warning: at a EUR/USD rate near 1.16 the wrong
answer looks entirely reasonable. The WACC was corrupted the same way, weighing a USD
market capitalisation against JPY debt.

The quality gate now refuses on a mismatch, naming both currencies and both ways forward.
`--fx-rate` or `--auto-fx` converts the statements into the price currency; converted at
spot, TM lands at $616.87, TSM at $109.14, SAP at $205.73 against a $209.69 price.

Verified before building it rather than assumed: FX pairs fetch through yfinance, and
`sharesOutstanding` equals `marketCap / price` to a ratio of 1.00 across TM, BABA, TSM,
SAP and AAPL — so the ADR ratio is already inside Yahoo's count and per-share maths lands
correctly once the money is converted. A new gate warning fires if that ever stops holding.

### Fixed — the same defect in the comps engine

Found by committing the ADR fixtures, which put them straight into the offline peer set.
Unconverted, TSM computed at **0.03x EV/EBITDA** — TWD earnings against a USD market
capitalisation — and the outlier screen waved it through, because the plausibility band
starts at zero. SAP's 19.86x looked normal and was wrong by the same mechanism.

Peers whose own statements and quote disagree are now excluded with a stated reason. Only
*internal* incoherence disqualifies a peer: a multiple is a ratio, so a company coherent
in euros throughout is perfectly comparable to a dollar one.

### Fixed — insurers passed a gate banks failed

JPM and BAC always failed structurally, on the missing EBIT line. PGR returned $837.92
against a $221.38 price and BRK-B $1,378.52 against $505.24, because both report something
EBIT-shaped. Unlevered free cash flow excludes financing so the operating business can be
valued alone; for a lender or underwriter, financing is the business.

Keyed on **industry, not sector** — verified against the live taxonomy. Yahoo files Visa,
Mastercard, S&P Global, the exchanges and insurance brokers under "Financial Services"
too, and all of those are ordinary fee businesses an unlevered DCF handles fine. A
sector-level rule would have wrongly blocked Visa. `--force-sector` overrides.

### Stated, not solved

- **The spot rate is held flat forever.** Interest-rate parity says a pair with a wide
  rate differential has a forward curve that is not flat. The standard shortcut, warned
  about on every conversion.
- **Converting amounts does not convert rates.** Cost of equity comes from the configured
  USD risk-free rate and ERP; cost of debt is the company's actual local borrowing rate.
  Toyota's is 0.50% against a 4.20% US risk-free rate inside one WACC. Re-basing either is
  an analyst judgement, and the model says so rather than picking one.

## Second audit — September 2026

No headline number moved: AAPL $120.08, MSFT $188.99, TSLA $8.86 are unchanged. The
defects were latent, environmental, or in the tests themselves. Test count 173 → 202,
Excel cross-check 18 comparisons → 324.

Two of the four findings were defects in the *first* audit's fixes, which is the useful
lesson from this round: a fix is not verified until something can fail when it regresses.

### Fixed

- **`--use-offline` failed from any directory but the repo root.** The first audit taught
  config paths to resolve against the package root and missed the offline fixtures. The
  CLI passes an explicit relative string built from `--offline-dir`, so resolving only
  `DEFAULT_OFFLINE_DIR` would not have helped — resolution now happens at the point of
  use, in `YFinanceClient` and `CompsEngine`. The comps case was the quieter of the two:
  it returned an empty peer set with no error at all. `resolve_path` and `PACKAGE_ROOT`
  moved to `src/paths.py`.

- **The option overhang was applied under `sbc.method = "expense"` and not under
  `"dilute"`**, in the Python engine and again in the Excel share-count formula. Options
  already granted vest whatever the model assumes about future grants, so both treatments
  must open from the same count. With a 5% overhang on AAPL the dilute path understated
  the share count by 4.8% and overstated value per share by 5.0%. Latent on the shipped
  config, which leaves `option_overhang_shares` unset — which is how 173 tests passed
  over it.

- **`closed_form_dilution_price` ignored the overhang and returned a negative price when
  K ≥ E**, a state its own docstring described as divergence. It now takes the overhang
  and raises `ConvergenceError`.

- **Preferred equity and restricted cash had no route into the model.** The bridge had a
  line for preferred and no way to populate it: the derived value was hardcoded `NaN`, so
  `_resolve` fell through to zero and the line item printed 0 as though reported. Both are
  now read from the balance sheet, with config still overriding. Restricted cash is
  deducted from the cash that comes off enterprise value, with a warning above 5% of the
  balance. No effect on the three fixtures — TSLA reports preferred stock of exactly zero,
  and none of them report restricted cash.

- **Non-finite values reaching the workbook.** `_num` guarded 3 of roughly 40 numeric
  write paths. It now sits on both cursor write helpers, an infinity raises rather than
  being blanked, and `assert_all_cells_finite` sweeps the in-memory workbook before save
  as a backstop for any path that bypasses it.

### Tests

- **Two workbook-level non-finite tests could not fail**, including the one written during
  the first audit to replace a test that could not fail. Its docstring asserted that
  infinities "DO survive the round trip"; they do not. openpyxl coerces `inf`, `-inf` and
  `nan` alike to `None` on read, and writes `<c t="n"><v></v></c>` — a numeric cell holding
  nothing. Both now assert against the in-memory workbook, and a companion test proves the
  assertion can fail.

- **`test_methods_converge` asserted a 15% bound while the README claimed 5%.** Tightened
  to 5%, which binds: removing dilution entirely produces a 6.8% gap on the test fixture,
  which the old bound passed. Added a test that reproduces that state and pins both ends.

- Added `test_second_audit_regressions.py`, one test per finding, and pinned the README's
  per-ticker convergence figures (0.5% / 0.7% / 3.9%) so they cannot go stale.

### Verification

- `tools/crosscheck.ps1` now compares the terminal value and every cell of both
  sensitivity grids, not three cells. The terminal value had been written to
  `expected.json` by `build_crosscheck.py` and never read — dead data since it was added.
  Cells Python declines because WACC ≤ growth must come back as `n/a` in Excel too.
  324 comparisons across 6 cases at 0.1%, all passing.

### Documented, not changed

- **Long-term investments** stay out of the bridge and are warned about instead. Apple
  reports $77.7bn, worth +4.4% on value per share; Microsoft +2.6%. Whether a securities
  portfolio is a claim available to the common is a judgement about the company, and a
  field-mapping decision should not move a headline number that far on its own.
- Beta is used as reported rather than Blume-adjusted; the tax rate is the 21% marginal
  rate rather than an effective rate. Both are now stated as choices in the README.

## First audit — September 2026

Three critical and ten high defects, all sharing one signature: a plausible,
confidently-labelled, wrong number with no error and no warning. Headline numbers moved —
AAPL $116.80 → $120.08, MSFT $188.30 → $188.99, TSLA $9.92 → $8.86. Test count 135 → 173.
See `tests/test_audit_regressions.py` and the audit section in the README.

## Initial build — August 2026

DCF and comparable-company engine with live-formula Excel output. Three corrections to the
original specification during the build, all concerning the double-counting of stock-based
compensation; see the README's first design decision.
