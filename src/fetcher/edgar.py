"""SEC EDGAR as a statement source: what the company actually filed.

`yfinance` is an unofficial scrape of Yahoo's aggregated figures, and the aggregation is
where the damage happens -- Yahoo's `EBIT` row is pretax plus interest rather than
operating income, which differs by 24.5% on Amazon FY2025 and survived three audits
because Apple is the one ticker where the two agree exactly.

EDGAR carries the XBRL facts as tagged in the filing itself. `us-gaap:OperatingIncomeLoss`
is operating income by definition, with no aggregation step to get wrong.

What it does NOT carry is market data. There is no price, no market capitalisation and no
beta anywhere in a 10-K or 20-F, so this module produces statements only and the `info`
payload still comes from Yahoo. The `--source edgar` flag is a hybrid by necessity.

Four traps, each verified against the live API and each capable of producing a silently
wrong answer rather than an error:

  Foreign filers carry BOTH taxonomies, and the US GAAP one is frozen. Honda exposes 420
  `us-gaap` concepts and 252 `ifrs-full`; Toyota 483 and 221. Both migrated to IFRS, so
  `us-gaap:OperatingIncomeLoss` stops at 2014-03-31 for Honda and 2020-03-31 for Toyota
  while the IFRS equivalent runs to 2025. Preferring `us-gaap` returns decade-old figures
  with no error, so `select_taxonomy` picks by recency instead.

  The same period appears several times. Each 20-F or 10-K restates prior-year
  comparatives, so Honda's FY2023 revenue appears under `fy` 2023, 2024 and 2025.
  Deduplication keeps the most recently filed version of each period.

  `total_debt` has no single US GAAP tag. Apple has no combined-debt concept at all; it
  composes from `LongTermDebtNoncurrent` + `LongTermDebtCurrent` + `CommercialPaper`.
  IFRS is the easier case -- `Borrowings` is the total, verified against its own
  components on both Honda and Toyota to the yen.

  A tag that looks like a synonym may be a different concept. Honda reports
  `AdditionsToNoncurrentAssets` of 4,036,808 million yen against
  `PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities` of 510,803 -- eight
  times larger, because it includes equipment on operating leases. Toyota tags only the
  former. Using it as a capex fallback would be catastrophically wrong for exactly the
  captive-finance automakers this model already struggles with, so capex is left absent
  rather than substituted.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from src.models.errors import ValuationError

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# SEC's access policy asks for a User-Agent naming the caller and a contact address, so
# they can reach whoever is generating the traffic. It is enforced, not advisory: a
# User-Agent with no email-shaped token is refused with a bare 403 and no explanation.
# Measured directly --
#     "dcf-valuation-engine"                          -> 403
#     "dcf-valuation-engine (github.com/...)"         -> 403
#     "dcf-valuation-engine contact@example.com"      -> 200
#
# The default below is a placeholder rather than a real mailbox, because this repository
# is public and committing someone's personal address to it is a worse outcome than a
# generic contact. Set SEC_USER_AGENT to your own "name you@domain" before doing anything
# beyond occasional use -- that is what SEC asks for, and it is one environment variable.
# https://www.sec.gov/os/accessing-edgar-data
DEFAULT_USER_AGENT = "dcf-valuation-engine contact@example.com"
USER_AGENT_ENV = "SEC_USER_AGENT"


def user_agent() -> str:
    """The contact string sent to SEC, overridable by environment variable."""
    import os

    return os.environ.get(USER_AGENT_ENV, "").strip() or DEFAULT_USER_AGENT

# Annual filings only. A 10-Q would bring quarterly figures into a series the projector
# reads as fiscal years.
ANNUAL_FORMS: frozenset[str] = frozenset({"10-K", "20-F", "40-F"})

# A duration fact tagged FY should span a year. The bound is loose enough for 52/53-week
# retail calendars and transition periods, tight enough to exclude a stray quarter.
MIN_DURATION_DAYS = 300
MAX_DURATION_DAYS = 430

# Which statement each canonical field is written to. Cosmetic for the engine -- the
# normalizer merges all three -- but the fixtures are meant to be readable by a person.
INCOME, BALANCE, CASHFLOW = "income", "balance", "cashflow"


class EdgarField:
    """One canonical field: where it goes, what it is called, and how to find it.

    `label` is the row name written into the fixture. It is deliberately Yahoo's
    vocabulary and deliberately the FIRST candidate in `FIELD_MAP` for that canonical
    field, so `FinancialNormalizer.canonicalize` resolves an EDGAR fixture through
    exactly the same path as a Yahoo one, with no second code path and no changes to
    `FIELD_MAP`. The real XBRL tag behind each number is recorded in the manifest, so
    the provenance is auditable even though the label is the model's own.
    """

    __slots__ = ("canonical", "label", "statement", "us_gaap", "ifrs", "instant")

    def __init__(
        self,
        canonical: str,
        label: str,
        statement: str,
        *,
        us_gaap: tuple[str, ...] = (),
        ifrs: tuple[str, ...] = (),
        instant: bool = False,
    ) -> None:
        self.canonical = canonical
        self.label = label
        self.statement = statement
        self.us_gaap = us_gaap
        self.ifrs = ifrs
        # Balance-sheet facts are instants and carry no `start`; income and cash flow
        # facts are durations and do. Checking the wrong one silently drops every row.
        self.instant = instant

    def tags(self, taxonomy: str) -> tuple[str, ...]:
        return self.ifrs if taxonomy == "ifrs-full" else self.us_gaap


# Every tag below was confirmed present with real values on at least one live filer
# during planning. Absent tags are left absent rather than approximated.
EDGAR_FIELDS: tuple[EdgarField, ...] = (
    EdgarField(
        "revenue",
        "Total Revenue",
        INCOME,
        us_gaap=(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ),
        ifrs=("Revenue", "RevenueFromContractsWithCustomers"),
    ),
    # Operating income, never a pretax-plus-interest aggregate. This is the defect the
    # FIELD_MAP comment documents at length, and EDGAR is structurally immune to it:
    # there is no tag that means what Yahoo's "EBIT" row means.
    EdgarField(
        "ebit",
        "Operating Income",
        INCOME,
        us_gaap=("OperatingIncomeLoss",),
        ifrs=("ProfitLossFromOperatingActivities",),
    ),
    EdgarField(
        "cost_of_revenue",
        "Cost Of Revenue",
        INCOME,
        us_gaap=("CostOfRevenue", "CostOfGoodsAndServicesSold"),
        ifrs=("CostOfSales",),
    ),
    EdgarField(
        "gross_profit", "Gross Profit", INCOME, us_gaap=("GrossProfit",), ifrs=("GrossProfit",)
    ),
    EdgarField(
        "rnd",
        "Research And Development",
        INCOME,
        us_gaap=("ResearchAndDevelopmentExpense",),
        ifrs=("ResearchAndDevelopmentExpense",),
    ),
    EdgarField(
        "sga",
        "Selling General And Administration",
        INCOME,
        us_gaap=("SellingGeneralAndAdministrativeExpense",),
        ifrs=("SellingGeneralAndAdministrativeExpense",),
    ),
    EdgarField(
        "interest_expense",
        "Interest Expense",
        INCOME,
        us_gaap=("InterestExpense", "InterestExpenseDebt", "InterestExpenseNonoperating"),
        ifrs=("InterestExpense",),
    ),
    EdgarField(
        "pretax_income",
        "Pretax Income",
        INCOME,
        us_gaap=(
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        ),
        ifrs=("ProfitLossBeforeTax",),
    ),
    EdgarField(
        "tax_provision",
        "Tax Provision",
        INCOME,
        us_gaap=("IncomeTaxExpenseBenefit",),
        ifrs=("IncomeTaxExpenseContinuingOperations",),
    ),
    EdgarField(
        "net_income", "Net Income", INCOME, us_gaap=("NetIncomeLoss",), ifrs=("ProfitLoss",)
    ),
    EdgarField(
        "diluted_shares",
        "Diluted Average Shares",
        INCOME,
        us_gaap=("WeightedAverageNumberOfDilutedSharesOutstanding",),
        ifrs=("WeightedAverageShares",),
    ),
    # ---------------------------------------------------------------- cash flow
    EdgarField(
        "cfo",
        "Operating Cash Flow",
        CASHFLOW,
        us_gaap=(
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
        ifrs=("CashFlowsFromUsedInOperatingActivities",),
    ),
    # `AdditionsToNoncurrentAssets` is NOT a fallback here -- see the module docstring.
    # Eight times larger than capex on Honda because it sweeps in operating-lease assets.
    EdgarField(
        "capex",
        "Capital Expenditure",
        CASHFLOW,
        us_gaap=(
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
        ),
        ifrs=("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",),
    ),
    EdgarField(
        "da",
        "Depreciation And Amortization",
        CASHFLOW,
        us_gaap=(
            "DepreciationDepletionAndAmortization",
            "DepreciationAmortizationAndAccretionNet",
            "DepreciationAndAmortization",
        ),
        ifrs=("DepreciationAndAmortisationExpense",),
    ),
    EdgarField(
        "sbc",
        "Stock Based Compensation",
        CASHFLOW,
        us_gaap=("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"),
        ifrs=("ExpenseFromSharebasedPaymentTransactionsWithEmployees",),
    ),
    EdgarField(
        "buybacks",
        "Repurchase Of Capital Stock",
        CASHFLOW,
        us_gaap=("PaymentsForRepurchaseOfCommonStock",),
        ifrs=("PaymentsToAcquireOrRedeemEntitysShares",),
    ),
    # ------------------------------------------------------------ balance sheet
    # The second US GAAP candidate includes restricted cash, which this model otherwise
    # keeps strictly separate. It is here because filers migrate between the two tags --
    # P&G reports only the combined concept from FY2023 -- and it is the same figure
    # Yahoo publishes under "Cash And Cash Equivalents", so the two providers agree.
    # `total_cash_position` still nets restricted cash out when the balance is reported.
    EdgarField(
        "cash",
        "Cash And Cash Equivalents",
        BALANCE,
        us_gaap=(
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ),
        ifrs=("CashAndCashEquivalents",),
        instant=True,
    ),
    EdgarField(
        "short_term_investments",
        "Other Short Term Investments",
        BALANCE,
        us_gaap=(
            "MarketableSecuritiesCurrent",
            "ShortTermInvestments",
            "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
            "OtherShortTermInvestments",
        ),
        ifrs=("OtherFinancialAssetsCurrent",),
        instant=True,
    ),
    EdgarField(
        "restricted_cash",
        "Restricted Cash",
        BALANCE,
        us_gaap=("RestrictedCashAndCashEquivalentsAtCarryingValue", "RestrictedCashCurrent"),
        ifrs=("RestrictedCashAndCashEquivalents",),
        instant=True,
    ),
    EdgarField(
        "current_assets",
        "Current Assets",
        BALANCE,
        us_gaap=("AssetsCurrent",),
        ifrs=("CurrentAssets",),
        instant=True,
    ),
    EdgarField(
        "current_liabilities",
        "Current Liabilities",
        BALANCE,
        us_gaap=("LiabilitiesCurrent",),
        ifrs=("CurrentLiabilities",),
        instant=True,
    ),
    EdgarField(
        "stockholders_equity",
        "Stockholders Equity",
        BALANCE,
        us_gaap=("StockholdersEquity",),
        ifrs=("EquityAttributableToOwnersOfParent",),
        instant=True,
    ),
    EdgarField(
        "total_equity_incl_minority",
        "Total Equity Gross Minority Interest",
        BALANCE,
        us_gaap=("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",),
        ifrs=("Equity",),
        instant=True,
    ),
    EdgarField(
        "minority_interest",
        "Minority Interest",
        BALANCE,
        us_gaap=("MinorityInterest",),
        ifrs=("NoncontrollingInterests",),
        instant=True,
    ),
    EdgarField(
        "preferred_equity",
        "Preferred Stock",
        BALANCE,
        us_gaap=("PreferredStockValue",),
        ifrs=(),
        instant=True,
    ),
    EdgarField(
        "long_term_investments",
        "Investments And Advances",
        BALANCE,
        us_gaap=("LongTermInvestments", "MarketableSecuritiesNoncurrent"),
        ifrs=("OtherFinancialAssetsNoncurrent",),
        instant=True,
    ),
    EdgarField(
        "net_ppe",
        "Net PPE",
        BALANCE,
        us_gaap=("PropertyPlantAndEquipmentNet",),
        ifrs=("PropertyPlantAndEquipment",),
        instant=True,
    ),
    EdgarField(
        "ordinary_shares",
        "Ordinary Shares Number",
        BALANCE,
        us_gaap=("CommonStockSharesOutstanding", "CommonStockSharesIssued"),
        ifrs=("NumberOfSharesOutstanding",),
        instant=True,
    ),
    # `Borrowings` is the IFRS total, and it is genuinely the total: it reconciles to
    # LongtermBorrowings + CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings
    # exactly on both Honda (11,451,267) and Toyota (38,792,879), to the yen.
    EdgarField(
        "total_debt",
        "Total Debt",
        BALANCE,
        us_gaap=("DebtLongtermAndShorttermCombinedAmount",),
        ifrs=("Borrowings",),
        instant=True,
    ),
    EdgarField(
        "current_debt",
        "Current Debt",
        BALANCE,
        us_gaap=("LongTermDebtCurrent",),
        ifrs=("CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings",),
        instant=True,
    ),
)

# Fields with no single tag, summed from whichever components the filer reports. Apple
# has no combined-debt concept at all, so without this its total debt would be absent and
# the WACC would weight a debt-free capital structure.
#
# Borrowings only -- capitalised leases are deliberately excluded, for two reasons.
#
# The modelling reason: under ASC 842 an operating lease cost is a single operating
# expense, so it is already deducted inside the EBIT this model discounts. Adding the
# lease liability to net debt as well would charge the same lease twice, once through
# the cash flow and again through the bridge. Finance leases do split their interest out
# below EBIT and have a better claim to debt treatment, but they are under 1% of
# enterprise value on every filer checked, and including one lease type without the
# other invites more confusion than it removes.
#
# The consistency reason: Yahoo's own series is not consistent about this, which the
# reconciliation tool found. On Apple, Yahoo's "Total Debt" equals borrowings + finance
# leases + operating leases in FY2022 (132,480) and borrowings alone in FY2023, FY2024
# and FY2025 (111,088 / 106,629 / 98,657). `WACCCalculator._average_debt` averages the
# last two balance-sheet debt figures to derive a cost of debt, so a definition that
# changes between adjacent years silently averages across two different quantities.
# Composing from borrowings gives one definition in every period, which is the point.
COMPOSED_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "total_debt": {
        "us-gaap": (
            "LongTermDebtNoncurrent",
            "LongTermDebtCurrent",
            "CommercialPaper",
            "OtherShortTermBorrowings",
        ),
    },
    "current_debt": {
        "us-gaap": ("LongTermDebtCurrent", "CommercialPaper", "OtherShortTermBorrowings"),
    },
}


# ----------------------------------------------------------------------- network


def _get_json(url: str, what: str) -> Any:
    """Fetch and parse one JSON document, or explain how to proceed without it.

    `urllib` is imported inside the function for the same reason `yfinance` is inside
    `fetch_fx_rate`: importing this module must never pull in a network client, because
    `--use-offline` has to stay provably network-free and there is a test asserting it.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": user_agent()})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except Exception as exc:
        raise ValuationError(
            f"could not fetch {what} from SEC EDGAR ({url}): {exc}. "
            f"Use --source yahoo instead."
        ) from exc

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValuationError(
            f"SEC EDGAR returned a malformed response for {what} ({url}): {exc}. "
            f"Use --source yahoo instead."
        ) from exc


def resolve_cik(ticker: str) -> int:
    """Map a ticker to its SEC central index key.

    All 57 committed fixture tickers resolve, foreign depositary receipts included.
    Tencent does not -- it trades as an unsponsored ADR and files nothing with the SEC --
    which is a coverage limit of EDGAR itself rather than of this lookup.
    """
    symbol = (ticker or "").upper().strip()
    if not symbol:
        raise ValuationError("cannot resolve a CIK without a ticker")

    payload = _get_json(SEC_TICKERS_URL, "the ticker-to-CIK map")
    for entry in payload.values():
        if str(entry.get("ticker", "")).upper() == symbol:
            return int(entry["cik_str"])

    raise ValuationError(
        f"{symbol} is not in the SEC's ticker map, so it has no EDGAR filings. "
        f"Companies that trade as unsponsored depositary receipts (Tencent, for one) "
        f"do not register with the SEC. Use --source yahoo instead."
    )


def fetch_company_facts(cik: int) -> dict[str, Any]:
    """Every XBRL fact the company has ever tagged, as SEC publishes it."""
    payload = _get_json(SEC_FACTS_URL.format(cik=cik), f"company facts for CIK {cik:010d}")
    if not isinstance(payload, dict) or "facts" not in payload:
        raise ValuationError(
            f"SEC EDGAR returned no 'facts' block for CIK {cik:010d}. Use --source yahoo instead."
        )
    return payload


# ------------------------------------------------------------------- extraction


def select_taxonomy(facts: dict[str, Any]) -> str:
    """Pick the taxonomy that is actually current for this filer.

    Honda and Toyota both migrated from US GAAP to IFRS and kept their historical US GAAP
    facts, so both taxonomies are present and only one is live. Choosing by recency of
    the revenue fact is what stops a mapper returning Honda's 2014 figures in 2026.
    """
    available = facts.get("facts", {})
    best, best_end = "us-gaap", ""
    for taxonomy in ("us-gaap", "ifrs-full"):
        if taxonomy not in available:
            continue
        field = next(f for f in EDGAR_FIELDS if f.canonical == "revenue")
        series, _ = _series_for(available[taxonomy], field.tags(taxonomy), instant=False)
        if not series:
            continue
        latest = max(series)
        if latest > best_end:
            best, best_end = taxonomy, latest

    if not best_end:
        raise ValuationError(
            "no annual revenue fact found in either the us-gaap or ifrs-full taxonomy. "
            "Use --source yahoo instead."
        )
    return best


def _annual_rows(node: dict[str, Any], *, instant: bool) -> dict[str, dict[str, Any]]:
    """Latest-filed annual value per period end, from one tag's unit block.

    Two filters carry the weight here. Restricting to annual forms and `fp == "FY"`
    keeps quarterly facts out of a series the projector reads as fiscal years, and the
    duration bound catches anything that slipped through with an FY label but a quarter's
    span. Instant facts carry no `start` at all, so the bound cannot be applied to them
    and must not be.
    """
    units = node.get("units", {})
    if not units:
        return {}
    # Monetary facts are quoted in the filer's reporting currency (USD, JPY, ...) and
    # share counts in "shares". Either way a tag carries one unit in practice; taking the
    # first keeps a stray "USD/shares" per-share block from being read as a total.
    unit_key = next(iter(units))

    chosen: dict[str, dict[str, Any]] = {}
    for row in units[unit_key]:
        if row.get("form") not in ANNUAL_FORMS or row.get("fp") != "FY":
            continue
        end = row.get("end")
        val = row.get("val")
        if not end or val is None:
            continue

        if not instant:
            start = row.get("start")
            if not start:
                continue
            span = (pd.Timestamp(end) - pd.Timestamp(start)).days
            if not MIN_DURATION_DAYS <= span <= MAX_DURATION_DAYS:
                continue

        # Each filing restates prior-year comparatives, so one period appears under
        # several `fy` values. The most recently filed version is the company's current
        # view of that year.
        previous = chosen.get(end)
        if previous is None or str(row.get("filed", "")) >= str(previous.get("filed", "")):
            chosen[end] = row
    return chosen


def _series_for(
    taxonomy_facts: dict[str, Any], tags: tuple[str, ...], *, instant: bool
) -> tuple[dict[str, float], str | None]:
    """Merge the candidate tags per period, earlier candidates winning each period.

    First-match-wins *per period*, not per tag -- the same `combine_first` semantics
    `FinancialNormalizer.canonicalize` already uses for Yahoo's row labels.

    Taking the first tag with any data at all was the obvious implementation and it is
    wrong in a way that produces no error. Procter & Gamble stopped tagging
    `CashAndCashEquivalentsAtCarryingValue` and moved to
    `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents`, so the first
    candidate still had rows -- just none for the newest year. `Financials.latest` then
    reached back for the most recent non-null value it could find and returned cash from
    an earlier fiscal year as though it were current: 4,239m against the 9,942m on the
    FY2026 balance sheet, a 57% understatement of the cash that comes off enterprise
    value, silently.
    """
    merged: dict[str, float] = {}
    used: list[str] = []
    for tag in tags:
        node = taxonomy_facts.get(tag)
        if not node:
            continue
        rows = _annual_rows(node, instant=instant)
        if not rows:
            continue
        contributed = False
        for end, row in rows.items():
            if end not in merged:
                merged[end] = float(row["val"])
                contributed = True
        if contributed:
            used.append(tag)
    return merged, (" | ".join(used) if used else None)


def _composed_series(
    taxonomy_facts: dict[str, Any], components: tuple[str, ...]
) -> tuple[dict[str, float], list[str]]:
    """Sum whichever components the filer reports, per period.

    Apple's total debt is `LongTermDebtNoncurrent` + `LongTermDebtCurrent` +
    `CommercialPaper` and there is no combined tag to read instead. Periods where no
    component is present stay absent rather than becoming a zero, because a zero here
    would read downstream as a debt-free company.
    """
    totals: dict[str, float] = {}
    used: list[str] = []
    for tag in components:
        node = taxonomy_facts.get(tag)
        if not node:
            continue
        rows = _annual_rows(node, instant=True)
        if not rows:
            continue
        used.append(tag)
        for end, row in rows.items():
            totals[end] = totals.get(end, 0.0) + float(row["val"])
    return totals, used


def build_statements(facts: dict[str, Any]) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Turn a company-facts payload into income/balance/cashflow frames.

    The frames carry Yahoo's row labels so `FinancialNormalizer.canonicalize` consumes
    them unchanged; `meta` records which XBRL tag actually supplied each row, so the
    provenance survives into the fixture manifest.
    """
    taxonomy = select_taxonomy(facts)
    taxonomy_facts = facts["facts"][taxonomy]

    rows: dict[str, dict[str, dict[str, float]]] = {INCOME: {}, BALANCE: {}, CASHFLOW: {}}
    tag_provenance: dict[str, str] = {}

    for field in EDGAR_FIELDS:
        series, tag = _series_for(
            taxonomy_facts, field.tags(taxonomy), instant=field.instant
        )
        if not series:
            composed = COMPOSED_FIELDS.get(field.canonical, {}).get(taxonomy)
            if composed:
                series, used = _composed_series(taxonomy_facts, composed)
                tag = " + ".join(used) if used else None
        if not series:
            continue
        rows[field.statement][field.label] = series
        if tag:
            tag_provenance[field.canonical] = f"{taxonomy}:{tag}"

    frames: dict[str, pd.DataFrame] = {}
    for name, table in rows.items():
        if not table:
            frames[name] = pd.DataFrame(dtype="float64")
            continue
        frame = pd.DataFrame(table).T.astype("float64")
        # Period labels must be Timestamps, not the ISO strings EDGAR returns. Yahoo's
        # frames carry Timestamps, and everything downstream -- the normalizer's period
        # sort, `_drop_empty_periods`, and any comparison against a committed fixture --
        # assumes one type. Leaving these as strings made the reconciliation tool report
        # zero overlapping periods rather than a mismatch, which is the quieter failure.
        frame.columns = [pd.Timestamp(c) for c in frame.columns]
        frames[name] = frame.reindex(sorted(frame.columns), axis=1)

    latest_accn = _latest_accession(taxonomy_facts)
    meta = {
        "taxonomy": taxonomy,
        "tags": tag_provenance,
        "latest_accession": latest_accn,
        "entity_shares_outstanding": _entity_shares(facts),
    }
    return frames, meta


def _latest_accession(taxonomy_facts: dict[str, Any]) -> str | None:
    """Accession number of the most recent annual filing the facts came from."""
    best_filed, best_accn = "", None
    for node in taxonomy_facts.values():
        for rows in node.get("units", {}).values():
            for row in rows:
                if row.get("form") in ANNUAL_FORMS and str(row.get("filed", "")) > best_filed:
                    best_filed, best_accn = str(row["filed"]), row.get("accn")
    return best_accn


def _entity_shares(facts: dict[str, Any]) -> dict[str, Any] | None:
    """Cover-page share count, when the filer tags one.

    Reliable for US domestic filers -- Apple's is current to 2026-07-17 and matches the
    14.594bn the model uses -- and unreliable to absent for foreign private issuers:
    Honda's latest is 2020-03-31, and PetroChina's `dei` block is empty entirely. It is
    reported here for the manifest rather than used to override anything, because a
    six-year-old count is not an improvement on Yahoo's.
    """
    node = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding")
    if not node:
        return None
    best = None
    for rows in node.get("units", {}).values():
        for row in rows:
            if best is None or str(row.get("end", "")) > str(best.get("end", "")):
                best = row
    if best is None:
        return None
    return {"value": float(best["val"]), "as_of": best.get("end")}


def fetch_edgar_statements(ticker: str) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Ticker in, statement frames plus provenance out."""
    cik = resolve_cik(ticker)
    facts = fetch_company_facts(cik)
    frames, meta = build_statements(facts)
    meta["cik"] = cik
    meta["entity_name"] = facts.get("entityName")
    return frames, meta
