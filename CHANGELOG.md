# Changelog

Dated by the audit that prompted each round rather than by release, because there have
been no releases. Every entry names what moved and by how much.

## The discount rate has a currency too — September 2026

Found by valuing Samsung and SK Hynix. Both report and trade in won, so the
statement-versus-quote check passes cleanly, and both were then discounted at a WACC
built from a US Treasury yield and a US equity risk premium. Nothing warned, because
nothing recorded what currency the rates were in.

This is the more dangerous of the two currency defects. Toyota's was loud -- $79,467 per
share against a $198 quote, which is why the ADR guard exists. This one produces an
output where every figure looks reasonable and the units are internally consistent
everywhere except the discount rate. **Worth 13.5% on Samsung**: at Korea's ~3.2%
ten-year rather than the US 5.0%, WACC falls 13.33% -> 11.55% and value per share rises
from 44,928 to 50,970 won.

`wacc.assumption_currency` (default `USD`) records what the rates are denominated in, and
`_check_rate_currency_coherence` refuses when it disagrees with the statements. Three ways
out, all named in the refusal: supply a local rate and declare it (`--risk-free` with
`--rate-currency`), convert the statements (`--fx-rate` / `--auto-fx`, which rewrites the
statement currency so a converted ADR passes on its own), or accept it explicitly
(`quality.allow_rate_currency_mismatch`).

`currency.allow_mismatch` downgrades the refusal to a warning rather than silencing it.
That flag is a claim about statements against price; whoever set it may never have
considered the rates, so the fact survives even when the refusal does not.

No committed fixture changes behaviour: the sweep is unchanged at 48 valuations and 9
refusals, and the three tickers that cite this guard -- ASML, SAP and TSM -- were already
refusing on the ADR check. It only fires on companies that report and trade in one
non-USD currency, which is precisely the case nothing covered.

Verified: 425 tests passing, `ruff` clean, four mutations of the guard all caught.

## SEC EDGAR as a second statement source — September 2026

Thirteen tickers run in one sitting made the case that the weak link is the input data
rather than the valuation maths: a beta of 0.10 for Lockheed Martin, a 0.50% cost of debt
for Toyota (below the risk-free rate), a 91.4% share-count basis mismatch on PetroChina.
`yfinance` is an unofficial scrape of Yahoo's aggregated figures, and aggregation is
where the EBIT defect above came from.

`--source edgar` on the `snapshot` command takes the three statements from the company's
own 10-K or 20-F XBRL facts. `info.json` stays on Yahoo, because EDGAR carries no market
data whatsoever, and the flag is named for where the statements come from rather than
pretending to be a full replacement. Output is the existing fixture format, so the engine,
the comps module, the Excel builder and every test consume it unchanged.

Four traps, each found against the live API, each capable of producing a plausible number
rather than an error, and each now pinned by a test that was mutation-checked:

- **Foreign filers carry both taxonomies and the US GAAP one is frozen.** Honda's
  `us-gaap:OperatingIncomeLoss` stops at 2014-03-31, Toyota's at 2020-03-31; both moved to
  IFRS. Selecting by recency rather than preference is what stops a mapper returning
  twelve-year-old figures.
- **Every period appears several times**, once per filing that restates it as a
  comparative. Latest-filed wins.
- **`total_debt` has no single US GAAP tag** — Apple's composes from three, and absent
  components must stay absent rather than summing to a zero that reads as debt-free.
- **Filers migrate between tags.** P&G stopped tagging
  `CashAndCashEquivalentsAtCarryingValue`; taking the first candidate with any data
  returned an earlier year's cash as current, 4,239m against 9,942m. Candidates merge per
  period now, the same `combine_first` semantics the Yahoo normalizer already used.

`tools/reconcile_edgar.py` compares the two providers field by field. Apple reconciles to
0.00% across four fiscal years and twelve fields. Two differences are reported rather than
absorbed: Yahoo's `total_debt` includes capitalised leases in FY2022 and excludes them in
FY2023-25 — its own series is inconsistent — and EDGAR lags Yahoo for foreign filers.

This tool is what found the EBIT defect in the entry below.

Not fixed by any of it, and stated plainly: cost of debt and beta are not in XBRL, and
coverage stops at SEC registrants — Tencent and SK Hynix trade as unsponsored depositary
receipts and file nothing, so the CIK lookup refuses by name.

Verified: 415 tests passing, `ruff` clean, committed fixtures byte-identical, and a PG
fixture snapshotted through EDGAR values within 1.3% of the Yahoo-sourced run, the whole
gap traced to the documented lease difference in `total_debt`.

## The EBIT row, again — September 2026

An earlier round established that Yahoo's `EBIT` row is pretax plus interest and moved
`FIELD_MAP` to prefer `Operating Income`. That was the right direction and the wrong
destination. Yahoo publishes **two** operating-profit rows, and the one the model
switched to is Yahoo's own adjusted figure rather than what the company filed.

Found by reconciling against `us-gaap:OperatingIncomeLoss` on SEC EDGAR — the number in
the filing itself. In **28 of 28** ticker-periods checked, the filed figure matched
`Total Operating Income As Reported` and the adjusted `Operating Income` in none. The two
rows disagree in **75 of 152** fixture ticker-periods, and **18 of the 57 fixtures had an
affected base year** — the year whose margin the projector holds flat across the whole
forecast.

```
BA    FY2025   model read -5,416m   company filed  +4,281m    a sign flip
INTC  FY2025   model read    -23m   company filed  -2,214m
ABBV  FY2025   model read 20,091m   company filed  15,075m
KO    FY2024   model read 14,022m   company filed   9,992m
CRM   FY2023   model read  1,858m   company filed   1,030m
```

Boeing is the one that matters: the model read a loss where the company filed a profit,
and then forecast five years forward from it.

`FIELD_MAP["ebit"]` and `["operating_income"]` now put the as-filed row first, keeping
the adjusted row as a fallback for periods where Yahoo carries only that one. The alias
warning in `DCFEngine.run` was inverted by the same change and has been reversed with it:
it treated the adjusted row as the safe case, so left alone it would have gone quiet for
the input that needs checking and shouted about the one that does not.

Headline movement is confined to Tesla, $35.34 -> $33.55, and its ROIC pin, 0.042427 ->
0.038104. **Apple and Microsoft do not move at all** — both are companies where Yahoo's
two rows agree, which is why this survived four audits: every pinned number and the whole
Excel cross-check is anchored on Apple.

Verified after the fix: 385 tests passing, `ruff` clean, native Excel cross-check still
324/324 at 0.000%, fixture sweep still 48 valuations / 9 refusals / 0 errors. Pinned in
`tests/test_ebit_definition.py`; the tool that found it is `tools/reconcile_edgar.py`.

## Risk-free rate: stale by 75 basis points, and no way to check — September 2026

`config/assumptions.yaml` pinned `risk_free_rate: 0.042` for reproducibility, the same
reason it pins the equity risk premium and the tax rate. The difference is that this one
input tracks a real market that moves every trading day, and nothing in the codebase ever
checked whether the pin was still close. It was not: the actual 10-year Treasury yield had
drifted to 4.95%, a 75bp gap that moved AAPL's implied value **8.9%** on its own —
comparable in size to the dual-class and EBIT-definition findings above, and it had been
sitting there undetected the whole time.

Added `src/fetcher/rates.py::fetch_risk_free_rate`, fetching `^TNX` the same way
`src/fetcher/fx.py::fetch_fx_rate` fetches a spot rate. `--risk-free` and `--auto-risk-free`
follow the same opt-in shape as `--fx-rate` / `--auto-fx` — refused together, and
`--auto-risk-free` refused under `--use-offline`, since there is no network to fetch from
there. The pinned config default was re-based to the current yield (0.04947, dated
2026-09-17) rather than left stale, which moved every sample valuation in this repository:

```
AAPL  $120.24 -> $109.56   (-8.9%)
MSFT  $165.06 -> $151.14   (-8.4%)
TSLA   $36.26 ->  $35.34   (-2.5%, with comps)
```

Every other input is unchanged. Re-verified after the repin: 359 tests passing, the native
Excel cross-check still 324/324 at 0.000%, `ruff` clean.

## Dual-class share counts — September 2026

Found by generating a Nike workbook. The share-count basis check added in an earlier
round warned that `sharesOutstanding` (1.202bn) disagreed with `marketCap / price`
(1.483bn) by 19%. That check was written for depositary receipts; the actual cause here
is a dual-class structure, and it was wrong in a way that reverses the conclusion.

Yahoo reports `sharesOutstanding` for the listed class only. Market capitalisation covers
every class, and so does the equity value a DCF produces -- so dividing the whole
company's equity by one share class overstates value per share by the ratio between them.

```
GOOGL  5.867bn reported vs 12.230bn total   $158.36 -> $75.97   (2.08x overstated)
NKE    1.202bn vs 1.483bn                   $44.05  -> $35.70
META   2.205bn vs 2.548bn                   $229.28 -> $199.87
```

**Nike is the one that matters: it read as 13.6% upside and is actually 7.9% downside.**
The only ticker in the whole fixture set that looked like a buy, and it looked that way
because it was valued on one class of stock.

Four of the 56 fixtures are affected and all four are dual class. `base_share_count` now
trusts `sharesOutstanding` only when it agrees with market capitalisation divided by
price, and otherwise uses the balance-sheet "Ordinary Shares Number" or the
market-implied total, warning either way. Single-class tickers are untouched -- AAPL,
MSFT, TSLA and AMZN are unchanged.

Worth noting the guard that caught this was added for a different reason entirely, and
its message named the wrong cause. It still did its job: the number disagreed with the
market's own arithmetic, which was enough to make someone look.

## EBIT definition, and a cross-check that could not fail — September 2026

From a second external review, this one of the Amazon workbook. Four findings, and
chasing them turned up two more in the verification itself.

**Headline numbers moved, and they were wrong before.** AAPL $120.08 (unchanged),
MSFT $188.99 -> $165.06, TSLA $8.86 -> $36.09, AMZN $59.38 -> $39.59.

### Fixed — EBIT was pretax income plus interest, not operating income

`FIELD_MAP` preferred Yahoo's "EBIT" row over "Operating Income". That row is
`pretax + interest expense`, which sweeps interest income, equity-method marks and every
other non-operating item into what an unlevered DCF treats as operating profit. The cash
generating that interest income is then added back whole in the equity bridge, so it is
counted twice.

Amazon FY2025: operating income 79,975 against an "EBIT" of 99,585, which is exactly
pretax 97,311 + interest 2,274 -- an 11.16% operating margin reported as 13.89%. FY2022
is starker: "EBIT" of -3,569 against operating income of +12,248, the Rivian writedown
landing in an operating line.

**This survived three audits because Apple is the one sample ticker where the two rows
are identical.** Gap by ticker: AAPL 0.0%, NVDA 8.7%, MSFT 8.9%, TSLA 15.8%, GOOGL 23.7%,
AMZN 24.5%. The model was validated against the single company where the defect is
invisible.

### Fixed — the cross-check could not report a failure

Found while checking the fix. `Compare-Value` in `crosscheck.ps1` wrote its mismatch
message with `Write-Output`. PowerShell returns everything a function writes, so a
failing comparison returned `@("...message...", $false)` -- an array, which is truthy --
and `if (-not (Compare-Value ...))` never fired.

The script printed `TSLA 36.0925 | 7.1883 | 80.084% | PASS` and then
`CROSS-CHECK PASSED: 324 comparisons`. Every "324 comparisons pass" reported in this
changelog was produced by a script that could not fail. Messages now go to the host, so
the return value is a bare boolean.

### Fixed — the workbook could not follow the engine's terminal-method fallback

Which is what the repaired cross-check immediately caught. `TerminalValue.select` skips
any method whose value is not positive; the workbook branched on the *configured* method
alone. With Tesla's year-5 free cash flow now negative, Gordon goes negative, Python falls
back to the exit multiple and reports $36.09 while the workbook carried the negative
Gordon figure and reported $7.19 -- an 80% disagreement between two halves of one model.
Both the selection and the discount period now mirror the fallback. TSLA agrees to 0.000%.

### Fixed — the growth sensitivity axis pointed at an empty cell

**Introduced in the previous commit.** Converting the axis labels from literals to
formulas, the growth axis was pointed at `refs["growth"]` -- the revenue-growth series,
whose base column is blank by design -- instead of `refs["g"]`, the perpetuity growth
rate. The axis evaluated to zero, so the grid ran from -1.0% to +1.0% perpetuity growth,
its centre cell no longer reproduced the model's own answer, and the table quoted
valuations at negative terminal growth.

The test written alongside that change asserted only that the formula mentioned "Inputs!"
or "WACC!" -- which the broken reference did. It now checks the cell it points at is
non-empty and equals the perpetuity growth rate.

### Consequences worth stating

Tesla is now degenerate rather than merely pessimistic: year-5 free cash flow is negative,
so terminal value is 100% of enterprise value and the scenario ordering can invert,
because Gordon and the exit multiple do not respond to growth in the same direction. Two
integration tests now assert the ordering only where a single terminal method carries
every scenario, and require the degeneracy to be warned about where it does not. The model
already emits four warnings on that run, including "the terminal value is 100% of
enterprise value".

Normalising terminal capex resolves it -- `--fade-capex` moves TSLA to $16.07 with a
usable Gordon and 75.6% TV, and AMZN to $76.41. It is left opt-in: the README's stated
position is that the model flags the reinvestment gap and does not overrule the forecast.

## External workbook review — September 2026

An outside review of the generated Excel found eight defects. Six were real, one was
stale, one misdiagnosed its own mechanism. The first is the worst, and it is a defect in
the verification as much as in the model.

### Fixed — the exit-multiple sensitivity contradicted the DCF sheet

Both sensitivity grids discounted terminal value at the mid-year period (4.5 years under
a 5-year forecast). That is correct for Gordon -- a perpetuity of mid-year flows really
does start half a year early -- and wrong for an exit multiple, which is a sale price at
a point in time and is discounted the full 5.0. The DCF sheet already branched correctly;
the grids did not:

```
exit grid at 12x / 10.025%:  $136.50      DCF sheet, same assumptions:  $131.66
```

Every cell of that grid was high by (1+w)^0.5. After the fix the grid reads $131.66 at
12x -- the DCF sheet's own number.

**The cross-check did not catch this, and could not have.** `build_crosscheck.py`
computed its expected grid using the same exponent copied from the workbook formula, so
Excel and Python agreed to 0.000% and the run reported "324 comparisons, Excel agrees
with Python on every case". Two implementations of one error agree with each other. That
was reported here and in the README as evidence of correctness; it was evidence of
consistency. The Python side now derives the exponent from the convention instead of
restating the sheet, and `tests/test_excel_output.py` compares the grid's discounting
against the DCF sheet directly. Reverting the exponent now fails that test.

### Fixed — sensitivity axes were frozen at build time

The WACC and growth axis labels were written as literal values, so changing beta or the
ERP in the workbook left the grid no longer centred on the base case while still
presenting itself as bracketing it. They are formulas off the live WACC and growth cells
now.

### Fixed — the Summary note was false

It read "Every figure links to the DCF sheet. Blue cells on Inputs are the only hardcoded
numbers in the workbook." Summary's comps, historicals and SBC memo blocks are written as
values. They tie out on build and desync silently on the first edit. The note now says
which blocks are static and that a rebuild is required. The WACC sheet's equivalent note
was true of that sheet and now says so explicitly rather than implying it of the file.

### Fixed — FootballField labelled the base case as a "Midpoint"

The column carries `base_result.value_per_share`, not the midpoint of the range: $120.08
against an 88.22-193.75 span whose actual midpoint is 140.98. Renamed to "Base case".

### Fixed — cost of debt below the risk-free rate now says so

3.83% against a 4.20% risk-free rate, because the shipped Apple fixture reports no
interest expense for FY2024-25 and the model reaches back to FY2023, pairing that with
current average debt.

Deliberately **not** floored at rf plus a spread, as the review suggested. A company that
termed out at 2% coupons in 2021 genuinely does pay less than today's Treasury, and
clamping would overwrite a fact about the balance sheet with an assumption. The vintage
mismatch is the real defect, so it warns and names both possible causes.

### Not defects

- **Comps peer count.** The review saw "screened peer count 1" with two-name medians
  including TSLA. That workbook predates the peer-universe snapshot; AAPL now draws seven
  Technology peers and nothing is screened.
- **"The reverse DCF flexes growth alone while holding margin fixed."** It solves for
  margin too -- `reverse_dcf.py` patches `projection.ebit_margin` independently. The
  criticism underneath is fair (single-lever solves produce absurd figures, and a 28.9%
  implied CAGR is a symptom of the method, not of the company) but the stated mechanism
  is not what the code does.

## Full peer universe offline — September 2026

Offline mode had 5 fixtures. It now has 56: every ticker `SECTOR_PEERS` and
`INDUSTRY_PEERS` can request, which is the exact set the comps engine falls back to when
config names no peers. 393KB -> 4.1MB. All 56 fetched cleanly, none failed.

The payoff is that offline comps became real. Micron now values with no network at all
against NVDA, AMD, TSM, QCOM, INTC, AVGO and TXN, and the peer set is usable rather than
reported as too thin to drive anything.

### Fixed — offline peers were chosen alphabetically

This is why the snapshot was not just a download. `resolve_peers` picked offline peers by
globbing the fixture directory and taking the first `max_peers` **alphabetically**:

```python
available = sorted(p.name for p in self.offline_dir.glob("*") if ...)
return available[: max_peers], "offline_fixtures"
```

With five fixtures that was harmless — too few survived to clear `min_peers`, so the comps
were honestly labelled thin and the terminal multiple fell back to the stated static
assumption. With 56 it would have become actively wrong: Apple's peers would have been
**ABBV, ADBE, AMAT, AMD, AMZN, ASML, AVGO, BA** — AbbVie, Adobe, Applied Materials, Boeing
— and eight of them clears the minimum, so an alphabetical accident would have driven the
terminal multiple under a confident heading.

Offline selection now runs the same industry-then-sector resolution as live and intersects
it with what is on disk, reporting the path taken (`offline:industry_map:Semiconductors`)
rather than a bare `offline_fixtures`. The alphabetical glob survives only as the fallback
for targets with no industry or sector, which is what synthetic test financials are.

### Fixed — snapshotting silently invalidated the pinned reference numbers

`--peer-universe` includes AAPL, MSFT and TSLA, so the first run refreshed the three
fixtures every pinned figure reproduces from. Only market data changed — the statements
were identical — but Apple's market capitalisation moved 2.7%, which shifts the WACC
capital-structure weights:

```
AAPL  $120.08 -> $120.02      MSFT  $188.99 -> $189.01
```

Small, correct, and quietly invalidating the README, this changelog, three test files and
the Excel cross-check. TSM's income statement had changed too, which would have broken the
pinned conversion test. The reference fixtures were restored, and `snapshot` now **skips
existing fixtures unless `--force`** — refreshing a reference should be a decision, not a
side effect of asking for something else.

### Tests

Three tests asserted against however many fixtures the repository happened to contain, so
adding fixtures broke them. Each now builds the condition it tests:

- `test_offline_comps_report_a_thin_peer_set` copies two fixtures into `tmp_path`, so it
  tests the below-minimum behaviour rather than the repository's contents.
- `test_outlier_peer_is_screened_from_medians` and the ADR currency tests name their peers
  explicitly. TSLA is Consumer Cyclical and SAP is not in the Technology bucket, so the
  maps will never pair either with AAPL — naming them is what still exercises the screen
  and the currency guard.
- The Excel comps test asserts peer **provenance** reaches the sheet, which is durable,
  instead of a "below the minimum" note that correctly disappeared.

### CLI

`snapshot` takes `--tickers` (comma-separated), `--peer-universe`, `--delay` (default 1.5s,
because Yahoo throttles) and `--force`. One retry per ticker: a single throttled request
should not cost a fixture, and a half-fetched set is worse than a short one because the
gaps are invisible afterwards.

289 tests, ruff clean, Excel cross-check 324 comparisons. Headline numbers unmoved:
AAPL $120.08, MSFT $188.99, TSLA $8.86.

## Dashboard made genuinely standalone — September 2026

The dashboard called itself a "standalone report" while fetching 407KB of Tailwind from
`https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js` — a dev-channel path on
infrastructure this project does not control. It worked when checked (HTTP 200), which is
the problem: it fails silently and completely the moment that URL is retired or the reader
is offline, leaving unstyled HTML and no warning. That also flatly contradicted the
project's headline promise that everything runs with no network at all.

The stylesheet is now inlined: 112 utility classes hand-written, 6.6KB, replacing a 407KB
download. The generated page makes **zero external requests**.

### Also fixed, found by actually looking at the rendered page

Four classes are applied by JavaScript rather than appearing in a static `class`
attribute, and the first pass missed them because the extraction regex stopped at the end
of the line while the assignment spans a `+` concatenation:

```
btn.className = "py-1.5 px-2 rounded-lg ... " +
  (ticker === selectedTicker
    ? "bg-blue-600 text-white border-blue-500 shadow-lg"   <- these
```

With no rule for `bg-blue-600`, the selected-company button rendered with the browser's
default white background and near-white text — invisible. Caught by screenshotting the
page, not by the test, which is the honest account of it.

### Tests

`tests/test_dashboard.py` — the module was the largest in the repository with no tests at
all (438 lines). It computes nothing, so it cannot produce a wrong valuation, but it could
and did lose styling silently. The suite now asserts no remote assets are referenced, that
every class used has a matching rule, **and that the same holds for classes assigned in
JavaScript**. Both halves are mutation-tested: deleting `.rounded-2xl` or `.bg-blue-600`
fails the suite.

Also removed a hardcoded "261 Tests" badge from the page, which had already drifted (the
suite was at 281) and would keep drifting. The README has been bitten by exactly this
before, so the count is simply gone rather than updated.

289 tests, ruff clean. Headline numbers unmoved: AAPL $120.08, MSFT $188.99, TSLA $8.86.

## Audit of the parallel work — September 2026

Prompted by "recheck if the work is genuine". Verifying live rather than citing pass
counts turned up something the question did not ask about: the working tree had diverged
from the pushed commit by 692 lines across 12 files plus 9 new files, none of it written
in the session that had just pushed. This entry covers auditing that work before it
reached GitHub, plus one defect found in the auditing session's own tests.

Headline numbers unmoved: AAPL $120.08, MSFT $188.99, TSLA $8.86. Test count 272 -> 281.
Excel cross-check still 324 comparisons at 0.1%.

### Fixed — a requested terminal-value method was silently substituted

Setting `terminal.method = "value_driver"` with a RONIC below the perpetuity growth rate
returned a **Gordon Growth number under the value-driver heading, with no warning**:

```
requested method    : value_driver (ronic 2% < g 2.5%)
method actually used: gordon
value per share     : $120.08
warnings            : none
```

The mechanism: growth at or above RONIC drives the reinvestment rate to 1.25, steady-state
cash flow negative, terminal value -3,416, `ok` reads False, and `select` quietly moves on.
The `ok` guard correctly prevents a negative terminal value being *used* — but the
substitution itself was invisible. This is the same class of defect the first audit found
with `method: "both"` silently resolving to Gordon, reintroduced through a new code path.

Two fixes: `value_driver_value` now warns when the reinvestment rate reaches 1, explaining
that growth funded below the cost of capital destroys value; and `select` warns whenever a
configured method is unusable and a different one carries the valuation. Verified no false
positive on the healthy path — RONIC 12% still selects value_driver, giving $100.21 against
Gordon's $120.08.

### Fixed — `test_moat.py` did not test the moat

Mutation testing: pinning `roic_base = 0.15` as a constant inside `analyze_moat` left all
three moat tests green. They asserted `roic_base > 0` and that `moat_rating` was a member
of the valid list — shape, never value. The arithmetic turned out to be **correct**
(re-derived independently as EBIT x (1-t) / invested capital, matching to 1e-9 on all
three fixtures) but it was correct untested.

Added six binding tests pinning ROIC to AAPL 0.609719, MSFT 0.276576, TSLA 0.049138,
re-deriving the definition independently, and checking the economic spread. The constant
mutant now fails six of them.

### Fixed — my own conversion test was too weak

`test_conversion_lands_in_the_same_order_as_the_quoted_price` asserted only
`0.05 < ratio < 20` against the quoted price. With the FX rate inverted:

```
TSM  correct $109.14 (ratio 0.263)   inverted $313,737 (ratio 755)   -> caught
SAP  correct $205.68 (ratio 0.981)   inverted $152.21 (ratio 0.726)  -> MISSED
```

A 26% error passed, because a EUR/USD rate near 1.16 barely moves the magnitude when
flipped — exactly the case the file exists to guard. Replaced with a pinned value.

Worth recording: the obvious replacement, asserting value per share scales linearly with
the rate, **fails and should**. Market capitalisation is already in the price currency and
is correctly left unscaled while debt is scaled, so WACC weights shift with the rate
(SAP: 8.2226% at 1x, 8.0912% at 2x). The non-linearity is right; the premise was wrong.

### Audited and found sound

- **`reverse_dcf.py`** — uses the correct Gordon back-solve `g = (TV x WACC - FCF)/(TV + FCF)`,
  not the no-growth shortcut. Substituting the shortcut fails its round-trip test.
- **`scenario_blender.py`** — `normalize_weights` correctly handles negative, empty and
  zero-total weights. Blend arithmetic re-derived by hand: 120.4850, exact match.
- **`dashboard.py`** — 438 lines and no tests, but it is an HTML template with no financial
  arithmetic in Python, so it cannot produce a wrong number of its own. Smoke-tested end to
  end; its figures reconcile with the engine (AAPL ROIC 61.0%, MSFT 27.7%).
- **`terminal_value.py` Value Driver formula** — `TV = NOPAT x (1 - g/RONIC) / (WACC - g)`
  is stated and implemented correctly, and is opt-in rather than silently replacing Gordon.

### Noted, not changed

- `dashboard.py` loads Tailwind from `gstatic.com/antigravity/web/dev/`, a dev-channel CDN
  path. It works, but it is not a stable asset URL for something meant to be shared.
- `ronic` is a validated field (`gt=0`, `le=2.0`) but has no entry in
  `config/assumptions.yaml`, unlike every other assumption. It defaults to WACC, which is
  the standard fade condition and a defensible default — just an undocumented one.

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
