# DCF Valuation Engine

A discounted-cash-flow and comparable-company valuation engine that outputs a working
Excel model — live formulas throughout, so you can open it, change the risk-free rate,
and watch the valuation move.

It is built around four problems that separate a student project from a tool an analyst
would actually use: stock-based compensation treated as a real cost, a model that still
runs when the data provider breaks, terminal multiples that decay as growth fades, and
tests that feed the model real-world data garbage rather than clean inputs.

```bash
python run.py value --ticker AAPL --use-offline
```

That command needs no API key, no network, and no configuration. It runs against
financial data committed to this repository.

---

## Quick start

```bash
pip install -e ".[dev]"
python run.py value --ticker AAPL --use-offline        # full model + Excel output
python run.py reverse --ticker AAPL --use-offline      # market expectations (reverse DCF)
python run.py scenarios --ticker AAPL --use-offline    # bear / base / bull + margin of safety
python run.py dashboard --use-offline                  # interactive multi-company dashboard
python run.py value --ticker MSFT --sbc-method dilute  # switch SBC treatment
python run.py snapshot --ticker NVDA                   # capture new offline fixtures
python run.py value --ticker SAP --auto-fx             # ADR: convert at spot
pytest                                                  # 261 tests, no network
```

The generated workbook lands in `outputs/` with eight sheets: Summary, Inputs, WACC,
DCF, Sensitivity, Comps, FootballField and Historicals.

---

## The four design decisions

### 1. Stock-based compensation is a real cost — charged once

SBC is the largest single lie in technology-company cash flow. "Free cash flow" as
reported is operating cash flow less capex, and operating cash flow adds SBC back as a
non-cash item. A company paying a fifth of its payroll in stock looks proportionally
more profitable than it is.

The model carries three cash-flow definitions:

| Definition | Formula | Used for |
|---|---|---|
| Reported FCF | CFO − capex | Contrast only. Never discounted. |
| Adjusted FCF | EBIT(1−t) + D&A − capex − ΔNWC, SBC left inside EBIT | `sbc.method = "expense"` |
| SBC-neutral FCF | Adjusted FCF + SBC after tax | `sbc.method = "dilute"`, and for reconciling to sell-side numbers |

**The part most implementations get wrong.** Reported EBIT is already net of stock
compensation. Subtracting SBC again from EBIT(1−t) *while also* growing the share count
charges shareholders twice for one cost. Expensing and diluting are two routes to the
same answer, so the model does one or the other and a Pydantic validator refuses any
configuration that tries both:

```
SBC double-count: deduct_from_fcf and grow_share_count are both true.
Expensing SBC and diluting the share count are two routes to the same answer,
so applying both charges shareholders twice. Pick one via sbc.method.
```

The claim that they are equivalent is tested, not asserted. On the sample companies the
two treatments agree within **0.5% for Apple, 0.8% for Microsoft and 3.9% for Tesla** —
the residual widens as stock compensation grows relative to equity value, and it is
explainable rather than noise: expensing charges the present value of SBC *after tax*
discounted at WACC, while diluting charges it *before tax* at the cost of equity. The gap
is the tax shield plus the spread between the two discount rates. The test pins 5%, which
binds; an earlier 15% threshold passed even with dilution removed entirely.

**The circularity, solved in closed form.** Shares issued to employees depend on the
price they are issued at, which depends on equity value per share, which depends on the
share count. The engine resolves this by iteration, and the fixed point also has an
analytic solution:

```
S = S₀ / (1 − K/E)        P = (E − K) / S₀        K = PV of future SBC at the cost of equity
```

So diluting is exactly "subtract the present value of the stock you are going to hand
employees from equity value" — which is the clearest statement of why also expensing it
would be charging the same cost twice. The test suite checks the iterative solver against
the closed form; they agree to within 0.01%. Both raise `ConvergenceError` rather than
returning a number when K exceeds E, because a company in that state cannot pay its staff
in stock without destroying the value of the stock.

**Both treatments start from the same share count.** Options and RSUs already granted
will vest whatever the model assumes about future grants, so `option_overhang_shares`
belongs in the opening count under expensing *and* dilution. The two methods differ only
in how they charge *future* grants. Adding the overhang to one and not the other made the
dilute path understate the share count by exactly the overhang — 4.8% on Apple at a 5%
overhang, worth 5.0% on value per share — and quietly broke the convergence property
below. It was invisible for as long as it was, and through 173 passing tests, because the
shipped configuration leaves the overhang unset.

**One further consistency point.** The terminal value is always built on the
SBC-expensed cash flow, whichever treatment the explicit period uses. Dilution can be
modelled explicitly for five years; it cannot be modelled explicitly forever.
Capitalising an added-back SBC into perpetuity while charging only five years of
issuance against it inflates the terminal value by roughly `1/(WACC − g)` times the
annual add-back — for a mega-cap, over a hundred billion dollars conjured out of an
accounting choice.

### 2. It runs when Yahoo Finance does not

`yfinance` is an unofficial, unversioned scraper. It renames rows between releases,
rate-limits, and returns empty frames for tickers that worked yesterday. A model that
only runs when Yahoo cooperates is a model nobody can evaluate — including a recruiter
who clones the repository on the wrong afternoon.

So the client exposes one interface over two backends. `data/offline_sample/` holds
committed fixtures for AAPL, MSFT, TSLA, plus SAP and TSM as cross-currency cases —
human-readable JSON you can open and read Apple's revenue history out of without running
anything.

```bash
python run.py value --ticker AAPL --use-offline   # no network at all
```

The entire test suite runs offline, so CI can never go red because of a third-party
outage. `python run.py snapshot --ticker NVDA` captures new fixtures when you want them.
Canonical field names live in one `FIELD_MAP`, so a Yahoo rename is a one-line fix rather
than a hunt through the engine.

### 3. Terminal multiples decay as growth fades

If a company trades at 22× EV/EBITDA while growing 8%, stamping 22× onto year-5 EBITDA
asserts it is still an 8% grower in perpetuity — when the same model just forecast growth
fading to 4%. The multiple has to come down with the growth:

```
multiple = peer_median − decay_turns_per_pp × (peer_growth − year5_growth in pp)
clamped to [mature_industry_multiple, peer_median]
```

Decay is expressed in *turns of EV/EBITDA per percentage point* of growth given up, and
clamped at both ends — the naive multiplicative form goes negative when growth collapses.
It is also one-directional: outgrowing peers earns no premium, because the extra growth
is already in the EBITDA the multiple is applied to. Paying for it again through the
multiple would be the same double-count as the SBC one.

**Every exit multiple is cross-checked against the growth it implies.** Back-solving
Gordon gives `g = (TV × WACC − FCF) / (TV + FCF)`, and the model flags any multiple
implying perpetuity growth above the ceiling:

```
A 12.0x exit multiple implies perpetuity growth of 3.62%, above the 3.50% ceiling.
No company outgrows the economy forever — the multiple is too high, or the year-5
cash flow is too low.
```

It reports the reverse check too (what multiple the perpetuity method implies) and warns
when the two terminal-value methods disagree by more than 35%.

**Peer sets are screened.** Multiples outside a plausible band are excluded from the
median — a name at 114× EV/EBITDA is being priced on a story the multiple cannot express,
and including it does not make the median more informative. Below three screened peers
the medians are reported but do not drive the terminal multiple, which falls back to the
stated static assumption. Two comparables is an anecdote, not a benchmark.

### 4. Tested against data garbage, not clean inputs

Clean-input tests prove the arithmetic. The edge-case matrix proves the model survives
contact with actual filings:

| Case | Expected behaviour |
|---|---|
| No debt, interest expense is NaN | Cost of debt falls back to the risk-free rate; zero weight anyway |
| Negative EBITDA | Warns, and the exit multiple falls back to Gordon Growth automatically |
| `"1,234,000"`, `"2.5B"`, `"(1,500)"`, `"invalid"` | Coerced to float64; unparseable becomes NaN, never an exception |
| All-NaN financials (delisted shell, SPAC) | `DataQualityError` before any arithmetic runs |
| SBC exceeds operating profit | Adjusted FCF goes negative, SBC-neutral stays positive — the treatment decides whether the business looks profitable at all |
| Negative beta (gold miner, inverse ETF) | Cost of equity below the risk-free rate, not clamped — that is a real property of the asset |
| Net cash (Apple) | Enterprise value below market cap |

A `DataQualityGate` runs before the engine and separates critical failures (raise) from
warnings (surface, and sometimes change model behaviour).

---

## Verification

**244 tests, no network required.** Including a golden case worked out by hand so a
refactor cannot silently move the valuation:

```
Revenue 1,000 growing 10%, EBIT margin 20%, tax 25%, D&A and capex both 5% of revenue
  →  every discounted cash flow lands on exactly 150.00
  →  terminal value 2,545.5375, enterprise value 2,362.50
  →  value per share exactly 23.625
```

**Python and Excel are cross-checked against each other.** The workbook is not a report
of numbers Python computed — it is an independent implementation in Excel formulas.
`tools/build_crosscheck.py` builds a workbook for every ticker and SBC method, and
`tools/crosscheck.ps1` opens each one in Excel, forces a full recalculation, and compares:

Each case compares value per share, equity value, share count and the terminal value,
then every cell of both sensitivity grids — WACC against perpetuity growth, and WACC
against exit multiple. That is 54 comparisons per case and **324 across three tickers
and both SBC treatments**, all at a 0.1% tolerance. Cells the Python side declines
because WACC ≤ growth must come back as `n/a` in Excel too, rather than being skipped.
The residual differences are the Python solver stopping at its own convergence tolerance
against Excel's exact closed form.

The grid comparison is new. An earlier version of this file claimed the sensitivity
table was checked when the script compared three cells and stopped — the check had been
run once by hand and never committed. The terminal value was in a worse state:
`build_crosscheck.py` wrote the expected figure into `expected.json` and the PowerShell
script never read it. Both are now in the tooling, which is the only version of a claim
like this that means anything.

This cross-check earned its place. It found three bugs that no amount of Python testing
would have caught:

- Subtotal labels written as `"= NOPAT"` are parsed by Excel as formulas and return
  `#NAME?`. The file looked perfect in openpyxl.
- The Inputs sheet was being handed the engine's *already-diluted* share count, so the
  workbook's own dilution formula applied dilution a second time — the same class of
  double-count the model exists to prevent, one level up. It only showed on TSLA, because
  the other workbooks had been built from expense-method runs.
- The workbook's share-count formula added the option overhang under the expense branch
  and not the dilute branch, mirroring the same asymmetry the Python engine had.

All three are now pytest regressions that run without Excel.

---

## What the Excel model actually does

Every cell on the DCF, WACC and Sensitivity sheets is a real formula. The colour
convention is the standard one: blue for hardcoded inputs, black for formulas on the
sheet, green for cross-sheet links. The Summary, Comps and Historicals sheets carry
reported figures as values — they present results rather than recomputing them.

The **SBC treatment is a live switch**. Type `dilute` in the SBC method cell on Inputs and
the tax line, the free cash flow line and the share count all change together:

```
method 'expense' -> value per share 120.0844, shares 14,594,180,000
method 'dilute'  -> value per share 119.4412, shares 15,063,896,452
value moved -0.54%, share count moved +3.22%
```

The sensitivity grids are live too — each cell rebuilds the valuation at that discount
rate and terminal assumption straight from the DCF sheet via `SUMPRODUCT`, rather than
holding a value Python pasted in. The dilution formula uses the closed form, so it needs
no iterative-calculation setting and cannot produce a circular-reference warning.

---

## Results on the sample companies

Base case, SBC expensed, as of the committed fixtures:

| | Implied | Market | Upside | WACC | TV % of EV |
|---|---|---|---|---|---|
| AAPL | $120.08 | $319.70 | −62.4% | 10.03% | 71.0% |
| MSFT | $188.99 | $513.53 | −63.2% | 10.15% | 70.7% |
| TSLA | $8.86 | $348.75 | −97.5% | 14.12% | 55.3% |

**Read these as an illustration of the machinery, not as a recommendation.** The model is
deliberately not calibrated to market prices, because reverse-engineering assumptions
until the answer matches the tape is the one thing a valuation must never do. AAPL moves
from $80 to $161 across the bear/bull scenarios, and the sensitivity table spans roughly
$86 to $188 on WACC and growth. The range is the output; the point estimate is an artefact
of the assumptions listed on one sheet.

Two things drive the gap, and it is worth separating them:

- **Apple** is mostly a discount-rate story. A 10% WACC comes from a 4.2% risk-free rate
  and a 5.5% equity risk premium; practitioners frequently use materially lower figures,
  and a reverse DCF shows the market price is reachable with a plausible lower rate.
- **Microsoft is not.** Its terminal year carries capex at **2.5× depreciation**, because
  the forecast drivers default to trailing averages and Microsoft is mid-way through an AI
  build-out. Gordon then capitalises that reinvestment gap into perpetuity. The model now
  warns when terminal capex diverges from D&A, but it will not overrule the forecast for
  you — normalising capex toward D&A in year 5 is the analyst's call.

TSLA is the clearest case of the model doing exactly what it was told: trailing EBIT
margin near 6%, capex around 10% of revenue, and no view whatsoever on autonomy or
robotaxi optionality. It values the reported numbers.

### These numbers changed after an audit

An adversarial audit in September 2026 found three critical defects, all with the same
signature: a plausible, confidently-labelled, wrong number with no error and no warning.
The most consequential for the table above:

- **The share count was last year's weighted average**, not today's count. A per-share
  value as of today needs today's denominator. Correcting it moved Tesla by 12%.
- **Scenario overrides were silently dropped** when the CLI ran from anywhere but the repo
  root, so bear, base and bull all returned the base-case number under their own headings.
- **The workbook ignored `capital_structure: target`**, disagreeing with the Python
  valuation printed beside it by up to 17%.

Each is now pinned by a regression test in `tests/test_audit_regressions.py`.

**A second audit followed.** It did not move any headline number, which is the useful
result, but it found four things worth naming — and two of them were defects in the first
audit's own fixes:

- **`--use-offline` broke outside the repo root.** Config paths had been taught to resolve
  against the package; offline fixtures had not. The README's headline promise held only
  if you happened to be standing in the repo.
- **The option overhang was applied to one SBC method and not the other**, in both the
  Python engine and the Excel formula. Described above.
- **The replacement for the vacuous NaN test was also vacuous**, and its docstring
  confidently explained a fact about openpyxl that is not true. openpyxl coerces `inf`,
  `-inf` and `nan` alike to `None` on read, so any check that runs against a reloaded
  workbook cannot fail. It now runs before the file is written. An infinity is refused
  outright rather than blanked: a blank cell reads as zero to every formula pointing at it.
- **Preferred equity and restricted cash had no route into the model.** The bridge had a
  line for preferred and no way to populate it — the derived value was hardcoded `NaN`, so
  a company with preferred stock outstanding valued at zero preferred and printed 0 as
  though that were reported.

These are pinned in `tests/test_second_audit_regressions.py`.

---

## Layout

```
config/          assumptions.yaml + scenarios.yaml (bear / base / bull)
data/            offline_sample/{AAPL,MSFT,TSLA} — committed fixtures
src/models/      assumptions (Pydantic + the SBC guard), canonical schema, quality gate
src/fetcher/     yfinance client (live + offline), normalizer, snapshot writer
src/dcf/         wacc, projector, dilution, terminal_value, bridge, engine,
                 sensitivity, monte_carlo
src/comps/       peer multiples with outlier screening
src/excel/       workbook builder and formatting conventions
tools/           Excel cross-check and verification scripts (Windows + Excel)
tests/           244 tests, all offline
```

---

## Where this model applies

A sweep across 31 tickers in 11 sectors, run live. 23 produce a valuation; 8 refuse. The
refusals are the point — each one used to produce a confident number that was wrong.

**Works.** Non-financial operating companies with a positive EBIT: big tech (MSFT, GOOGL,
META), semiconductors (NVDA, AMD), retail and consumer (WMT, COST, MCD), energy (XOM, CVX),
healthcare (JNJ, LLY), industrials (CAT, BA, DAL), telecom and utilities (T, NEE), REITs
(O, PLD). Also the fee businesses Yahoo files under "Financial Services" — Visa, Mastercard,
S&P Global, the exchanges, and insurance brokers — because those have no lending or
underwriting balance sheet and an unlevered DCF handles them normally.

**Works, but read it rather than lifting the number.** Loss-makers produce negative
per-share values (SNOW at −$41.86, MRNA at −$128.14). That is the model doing as it is
told, and it is a signal to change approach, not a target price. REITs and utilities run,
but capex here is a trailing average, which for a REIT conflates maintenance and growth
spending — FFO is the better lens.

**Refuses: banks, insurers, asset managers, capital markets.** JPM, BAC, PGR and BRK-B all
stop. Unlevered free cash flow deliberately excludes financing so that the operating
business can be valued on its own; for a lender or an underwriter, financing *is* the
business — deposits, float and leverage are the product. Banks always failed structurally,
because they report no EBIT line. Insurers did not: Progressive returned $837.92 against a
$221.38 price and Berkshire $1,378.52 against $505.24, because both report something
EBIT-shaped. Now they refuse, and `--force-sector` overrides it if you want to look anyway.

**Refuses: ADRs, unless you convert.** Yahoo reports the statements in the company's
reporting currency and the price, market cap and share count in the listing currency:

| | Statements | Quote | Unconverted | Converted at spot | Market |
|---|---|---|---|---|---|
| TM | JPY | USD | $79,467 | $616.87 | $198.30 |
| BABA | CNY | USD | $1,571 | $143.72 | $111.81 |
| TSM | TWD | USD | $5,294 | $109.14 | $415.50 |
| SAP | EUR | USD | $176.90 | $205.73 | $209.69 |

**SAP is the row that matters.** At a EUR/USD rate near 1.16 the unconverted answer looks
entirely reasonable and is wrong by the same mechanism that made Toyota absurd. The WACC
went with it: a USD market cap weighed against JPY debt gave Toyota a 0.43% discount rate.

```bash
python run.py value --ticker SAP --auto-fx        # fetch spot from Yahoo
python run.py value --ticker TM  --fx-rate 0.0063 # or supply your own
```

Two things converting does **not** fix, and the model says so out loud each time:

- **The rate is spot, held flat forever.** Interest-rate parity says a pair with a wide
  rate differential has a forward curve that is anything but flat.
- **Converting amounts does not convert rates.** The cost of equity is built from the
  configured risk-free rate and ERP — USD assumptions — while the cost of debt is the
  company's actual local borrowing rate. Toyota's is 0.50% against a 4.20% US risk-free
  rate, inside one WACC. Re-basing one or the other is a judgement the model will not make
  for you.

The same defect was live in the comps engine one level down, and is now fixed there too: a
peer whose statements and quote disagree is excluded from the medians with a stated reason.
Unconverted, TSM computed at **0.03x EV/EBITDA**, which the outlier screen waved through
because the plausibility band starts at zero. A bad peer is quieter than a bad target — it
moves a median instead of producing an absurd share price.

---

## Known limitations

Stated rather than hidden, because every one of them is a question worth being asked:

- **Peer selection is a judgement call.** Config first, then a small curated sector map.
  There is no defensible automatic answer, so the source of the peer set is recorded on
  the output. Offline mode has only three fixtures, so its comps are labelled as too thin
  to use.
- **Forecast drivers default to trailing averages.** For a company at a cyclical extreme
  this is wrong in a specific direction — Microsoft's capex is currently around 25% of
  revenue on an AI build-out, and extrapolating that flat suppresses free cash flow for
  five years. The model flags the resulting disagreement between terminal-value methods
  but will not overrule you.
- **Working capital is modelled as a percentage of revenue.** Adequate for stable
  businesses, crude for anything with a working-capital cycle that is actually changing.
- **No operating-lease or pension adjustments**, and no segment-level build. Enterprise
  value uses reported total debt.
- **Long-term investments are reported but not counted.** Whether a securities portfolio
  is a claim available to the common or capital tied up in the business is a judgement
  about that company, and the amounts are not small — Apple reports $77.7bn, worth +4.4%
  on value per share, and Microsoft +2.6%. The model warns when the balance sheet shows a
  material figure the bridge is ignoring and leaves `bridge.investments` to the analyst.
  Adding them automatically would have moved two headline numbers on a field-mapping
  decision, which is not a decision a mapping should make.
- **Beta comes from the provider**, not re-levered from a peer set, and is used as
  reported rather than Blume-adjusted. `beta_override` is there for when that matters.
- **The tax rate is the 21% marginal rate, not an effective rate.** Apple's effective rate
  is nearer 15%. Marginal is the defensible choice for a forward-looking unlevered
  forecast, but it is a choice, and it is the one being made.
- **Monte Carlo output is only as good as its input distributions**, which are themselves
  assumptions. It is there to show the shape of the uncertainty, not to add precision.
- **The Excel cross-check needs Windows and Excel.** The pytest suite covers the same
  ground structurally and runs anywhere.
- **CI has never run.** `.github/workflows/test.yml` is committed and correct, but the
  project is not yet a git repository, so no green badge is being claimed.

---

## Methodology notes worth defending out loud

- **Mid-year convention** is on by default: cash arrives through the year rather than in a
  lump on 31 December. The terminal value is discounted on the same convention as the
  final explicit year, because if the explicit flows are mid-year then so are the
  perpetuity's.
- **No floor on cost of equity.** A negative beta produces a cost of equity below the
  risk-free rate. Economically strange, mathematically correct, and clamping it would hide
  a real property of the asset.
- **Cost of debt** is interest expense over *average* total debt, because interest is a
  flow over the year and debt is a point in time. Clamped against garbage, and falling
  back to the risk-free rate when a company has no debt — unobservable, and zero-weighted
  in the WACC either way.
- **Working capital excludes cash and current debt.** Those are financing items and belong
  in the EV-to-equity bridge, not in the operating capital the business ties up. Apple's
  operating working capital is genuinely negative: growth releases cash rather than
  consuming it.
- **Restricted cash does not count as cash.** It is pledged — collateral, escrow, a
  regulatory reserve — so it is neither distributable to shareholders nor available to the
  business, and crediting it against enterprise value overstates equity value by its full
  amount. Providers fold it into the headline balance for companies that report it
  separately, so it comes off explicitly, with a warning when it is material.
- **Preferred equity comes off enterprise value** alongside debt and minority interest,
  because all three rank ahead of the common. Read from the balance sheet, overridable in
  config.
- **Sensitivity tables solve for the beta** that produces each target WACC, rather than
  injecting a discount rate inconsistent with the rest of the model. Note this is a
  different question from the one the *workbook's* grids answer: those hold the forecast
  cash flows fixed and re-discount them, which is why the cross-check compares Excel
  against a Python implementation of that same definition rather than against
  `src/dcf/sensitivity.py`.
- **A terminal value above 85% of enterprise value triggers a warning.** At that point
  almost nothing in the answer comes from the forecast you actually built.
