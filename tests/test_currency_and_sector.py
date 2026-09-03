"""Currency coherence and unsuitable-sector guards.

Both defects were found by a sector sweep across 31 live tickers, and neither could
have been caught by the existing fixtures: AAPL, MSFT and TSLA are all US non-financial
operating companies, so all three sit in the one corner of the input space where both
bugs are invisible.

    TM   JPY statements, USD quote  ->  $79,467 per share against $198,  WACC 0.43%
    SAP  EUR statements, USD quote  ->  $176.90 against $210
    PGR  "Insurance - Property & Casualty"  ->  $837.92 against $221.38

SAP is why the currency check is a critical rather than a warning. At a EUR/USD rate
near 1.16 the wrong answer looks entirely reasonable, which is exactly the failure this
model is built to refuse. TSM and SAP are both committed as fixtures for that reason --
a test covering only the dramatic case would pass a fix that still got SAP subtly wrong.
"""

from __future__ import annotations

import warnings

import pytest

from src.dcf.engine import DCFEngine
from src.fetcher.fx import MONETARY_FIELDS, NON_MONETARY_FIELDS, convert_statements
from src.fetcher.yfinance_client import YFinanceClient
from src.models.assumptions import CurrencyAssumptions, DCFAssumptions
from src.models.errors import DataQualityError
from src.models.financials import (
    FIELD_MAP,
    field_value,
    price_currency,
    share_count_basis_gap,
    statement_currency,
)
from tests.conftest import make_financials

# Roughly spot at the time the fixtures were captured. Exact values do not matter --
# these tests assert coherence and ratios, never a particular valuation.
TWD_USD = 0.031518
EUR_USD = 1.159017


def _run(ticker: str, overrides: dict | None = None):
    warnings.simplefilter("ignore")
    assumptions = DCFAssumptions.from_yaml(overrides=overrides)
    financials = YFinanceClient(ticker, offline_mode=True).get_financials(
        currency=assumptions.currency
    )
    return DCFEngine(financials, assumptions, ticker=ticker).run(), financials


class TestCurrencyMismatchIsRefused:
    @pytest.mark.parametrize(
        "ticker,statement", [("TSM", "TWD"), ("SAP", "EUR")]
    )
    def test_an_unconverted_adr_raises(self, ticker, statement):
        with pytest.raises(DataQualityError) as exc:
            _run(ticker)
        message = str(exc.value)
        assert statement in message and "USD" in message, (
            "the error must name both currencies -- the reader cannot act on it otherwise"
        )

    def test_the_message_says_how_to_proceed(self):
        with pytest.raises(DataQualityError, match="--auto-fx"):
            _run("TSM")

    def test_a_us_company_is_untouched(self):
        """The whole point: this must be invisible to every domestic ticker."""
        result, financials = _run("AAPL")
        assert result.value_per_share == pytest.approx(120.08, abs=0.01)
        assert financials.fx_rate_applied is None
        assert not financials.converted

    def test_allow_mismatch_is_an_escape_hatch_not_a_conversion(self):
        result, financials = _run("SAP", {"currency": {"allow_mismatch": True}})
        assert result.value_per_share > 0
        assert financials.fx_rate_applied is None, "allow_mismatch must not convert"
        assert financials.currency == "EUR", "the statements are still in EUR"


class TestConversion:
    @pytest.mark.parametrize(
        "ticker,rate,statement", [("TSM", TWD_USD, "TWD"), ("SAP", EUR_USD, "EUR")]
    )
    def test_the_converted_valuation_is_pinned(self, ticker, rate, statement):
        """The conversion has to be exact, not merely the right order of magnitude.

        An earlier version of this test asserted `0.05 < ratio < 20` against the quoted
        price. That band catches a catastrophe and nothing else. Reproduced with the
        rate inverted:

            TSM  correct $109.14 (ratio 0.263)  inverted $313,737 (ratio 755)  -> caught
            SAP  correct $205.68 (ratio 0.981)  inverted $152.21 (ratio 0.726) -> MISSED

        A 26% error on SAP sailed straight through, because a EUR/USD rate near 1.16
        barely moves the magnitude when you flip it -- exactly the case this file exists
        to guard, the currencies whose errors look reasonable.

        Pinning the number is what binds. Note the answer is deliberately NOT linear in
        the rate: market capitalisation is already in the price currency and is correctly
        left alone, while debt is scaled, so the WACC weights shift with the rate. That
        is right, and it is why the obvious "double the rate, double the value" check
        does not hold.
        """
        expected = {"TSM": 109.1382, "SAP": 205.6809}[ticker]
        result, financials = _run(ticker, {"currency": {"fx_rate": rate}})

        assert financials.fx_rate_applied == pytest.approx(rate)
        assert financials.original_currency == "USD"
        assert result.value_per_share == pytest.approx(expected, abs=0.01), (
            f"{ticker} converted at {rate} now values at {result.value_per_share:,.4f}, "
            f"not the pinned {expected:,.4f}"
        )

    @pytest.mark.parametrize(
        "ticker,rate", [("TSM", TWD_USD), ("SAP", EUR_USD)]
    )
    def test_an_inverted_rate_is_caught(self, ticker, rate):
        """The mutation the old ratio-band assertion let through on SAP."""
        correct, _ = _run(ticker, {"currency": {"fx_rate": rate}})
        inverted, _ = _run(ticker, {"currency": {"fx_rate": 1.0 / rate}})
        assert inverted.value_per_share != pytest.approx(
            correct.value_per_share, rel=0.01
        ), "inverting the rate must change the answer materially"

    def test_conversion_rewrites_the_statement_currency(self):
        """Which is what lets the quality gate pass without a separate flag."""
        _, financials = _run("SAP", {"currency": {"fx_rate": EUR_USD}})
        assert statement_currency(financials) == "USD"
        assert price_currency(financials) == "USD"
        assert financials.currency == "USD"

    def test_share_counts_are_not_scaled(self):
        """Scaling a count by an FX rate would move TSM's shares by a factor of 32."""
        _, unconverted = _run("TSM", {"currency": {"allow_mismatch": True}})
        _, converted = _run("TSM", {"currency": {"fx_rate": TWD_USD}})
        for name in ("diluted_shares", "ordinary_shares"):
            before = field_value(unconverted, name)
            after = field_value(converted, name)
            if before == before:  # skip NaN
                assert after == pytest.approx(before), f"{name} was scaled by the FX rate"

    def test_monetary_rows_are_scaled(self):
        _, unconverted = _run("TSM", {"currency": {"allow_mismatch": True}})
        _, converted = _run("TSM", {"currency": {"fx_rate": TWD_USD}})
        assert field_value(converted, "revenue") == pytest.approx(
            field_value(unconverted, "revenue") * TWD_USD, rel=1e-9
        )

    def test_every_canonical_field_is_classified(self):
        """A new field must be money or a count, never neither."""
        assert set(FIELD_MAP) == MONETARY_FIELDS | NON_MONETARY_FIELDS
        assert not MONETARY_FIELDS & NON_MONETARY_FIELDS

    def test_no_share_field_is_treated_as_money(self):
        """The loud failure if someone adds a count-like field and forgets."""
        offenders = [f for f in MONETARY_FIELDS if "share" in f]
        assert not offenders, f"these look like counts but would be scaled: {offenders}"

    def test_an_identity_rate_changes_nothing(self):
        statements = YFinanceClient("AAPL", offline_mode=True).get_financials().statements
        assert convert_statements(statements, 1.0) is statements

    def test_config_refuses_contradictory_settings(self):
        with pytest.raises(ValueError, match="not both"):
            CurrencyAssumptions(fx_rate=0.0063, auto_fx=True)
        with pytest.raises(ValueError, match="contradictory"):
            CurrencyAssumptions(fx_rate=0.0063, allow_mismatch=True)


class TestCompsExcludeMismatchedPeers:
    """The same defect one level over, found by committing the ADR fixtures.

    Adding TSM and SAP to the sample directory put them straight into the offline peer
    set, where TSM computed at **0.03x EV/EBITDA** -- TWD earnings against a USD market
    capitalisation. The outlier screen did not catch it, because the plausibility band
    is 0 to 50 and 0.03 sits comfortably inside it. SAP's 19.86x looked entirely normal
    and was wrong by the same mechanism, just smaller.

    A bad peer is quieter than a bad target: it moves a median rather than producing an
    absurd share price, so nothing looks wrong at all.
    """

    def test_mismatched_peers_are_excluded_with_a_reason(self):
        from src.comps.comps_engine import CompsEngine

        warnings.simplefilter("ignore")
        target = YFinanceClient("AAPL", offline_mode=True).get_financials()
        result = CompsEngine(
            "AAPL", DCFAssumptions.from_yaml(), offline_mode=True, target_financials=target
        ).run()

        assert set(result.skipped) >= {"SAP", "TSM"}
        for ticker in ("SAP", "TSM"):
            assert "quoted in USD" in result.skipped[ticker]
            assert ticker not in result.table.index

    def test_no_absurd_multiple_survives_into_the_table(self):
        """0.03x would have passed the 0-50 outlier band untouched."""
        from src.comps.comps_engine import CompsEngine

        warnings.simplefilter("ignore")
        target = YFinanceClient("AAPL", offline_mode=True).get_financials()
        result = CompsEngine(
            "AAPL", DCFAssumptions.from_yaml(), offline_mode=True, target_financials=target
        ).run()

        multiples = result.table["ev_ebitda"].dropna()
        assert (multiples > 1.0).all(), (
            f"a sub-1x EV/EBITDA is a currency error, not a cheap stock: "
            f"{multiples.to_dict()}"
        )

    def test_a_coherent_foreign_peer_would_be_kept(self):
        """A multiple is a ratio, so a company coherent in euros is comparable.

        Only internal incoherence disqualifies a peer -- not foreignness.
        """
        from src.comps.comps_engine import _currency_mismatch

        coherent = make_financials(
            revenue=1000.0, info={"financialCurrency": "EUR", "currency": "EUR"}
        )
        assert _currency_mismatch(coherent) is None

        incoherent = make_financials(
            revenue=1000.0, info={"financialCurrency": "EUR", "currency": "USD"}
        )
        assert _currency_mismatch(incoherent) is not None


class TestSectorSuitability:
    """Banks fail structurally on the missing EBIT line. Insurers did not."""

    REFUSED = [
        "Banks - Diversified",
        "Banks - Regional",
        "Insurance - Property & Casualty",
        "Insurance - Diversified",
        "Insurance - Life",
        "Capital Markets",
        "Asset Management",
        "Mortgage Finance",
    ]
    ALLOWED = [
        "Credit Services",             # V, MA, AXP -- payment networks
        "Financial Data & Stock Exchanges",  # SPGI, CME, ICE
        "Insurance Brokers",           # AJG -- a fee business, not an underwriter
        "Consumer Electronics",
        "Semiconductors",
    ]

    @staticmethod
    def _financials(industry):
        return make_financials(
            revenue=1000.0,
            ebit=200.0,
            info={"industry": industry} if industry is not None else {},
        )

    @pytest.mark.parametrize("industry", REFUSED)
    def test_unsuitable_industries_are_refused(self, industry):
        from src.models.quality_gate import DataQualityGate

        with pytest.raises(DataQualityError, match="unlevered DCF"):
            DataQualityGate(self._financials(industry)).validate()

    @pytest.mark.parametrize("industry", ALLOWED)
    def test_suitable_industries_still_run(self, industry):
        from src.models.quality_gate import DataQualityGate

        report = DataQualityGate(self._financials(industry)).validate()
        assert report.ok

    @pytest.mark.parametrize("industry", [None, "", "   "])
    def test_a_missing_industry_never_blocks(self, industry):
        """Synthetic financials carry no info payload, and Yahoo returns None for MMC."""
        from src.models.quality_gate import DataQualityGate

        report = DataQualityGate(self._financials(industry)).validate()
        assert report.ok

    def test_the_override_forces_it_through(self):
        from src.models.quality_gate import DataQualityGate

        report = DataQualityGate(
            self._financials("Insurance - Property & Casualty"),
            allow_unsuitable_sector=True,
        ).validate()
        assert report.ok

    def test_the_message_explains_rather_than_just_refusing(self):
        from src.models.quality_gate import DataQualityGate

        with pytest.raises(DataQualityError) as exc:
            DataQualityGate(self._financials("Banks - Regional")).validate()
        message = str(exc.value)
        assert "financing is the business" in message
        assert "allow_unsuitable_sector" in message


class TestShareCountBasis:
    """`sharesOutstanding` and `marketCap / price` must describe the same thing."""

    def test_a_consistent_basis_is_silent(self):
        financials = make_financials(
            revenue=1000.0,
            ebit=200.0,
            info={"sharesOutstanding": 100.0, "marketCap": 1000.0, "currentPrice": 10.0},
        )
        assert share_count_basis_gap(financials) == pytest.approx(0.0)

    def test_an_ordinary_share_count_against_an_adr_price_warns(self):
        """Toyota's ADR ratio is 10:1, so this is an order-of-magnitude error."""
        from src.models.quality_gate import DataQualityGate

        financials = make_financials(
            revenue=1000.0,
            ebit=200.0,
            info={"sharesOutstanding": 1000.0, "marketCap": 1000.0, "currentPrice": 10.0},
        )
        report = DataQualityGate(financials).validate()
        assert any("ADR ratio" in w for w in report.warnings)
        assert report.ok, "an integrity signal, not a blocker"

    def test_missing_inputs_return_nan_rather_than_raising(self):
        """Most synthetic financials carry no market data at all."""
        import math

        gap = share_count_basis_gap(make_financials(revenue=1000.0))
        assert math.isnan(gap)

    def test_a_nan_gap_produces_no_warning(self):
        from src.models.quality_gate import DataQualityGate

        report = DataQualityGate(make_financials(revenue=1000.0, ebit=200.0)).validate()
        assert not any("ADR ratio" in w for w in report.warnings)


class TestFixturesCarryBothCurrencies:
    """Stage 1 of the fix: without this, none of the offline tests above are possible."""

    @pytest.mark.parametrize(
        "ticker,statement,price",
        [("TSM", "TWD", "USD"), ("SAP", "EUR", "USD")],
    )
    def test_snapshot_persists_the_quote_currency(self, ticker, statement, price):
        financials = YFinanceClient(ticker, offline_mode=True).get_financials()
        assert statement_currency(financials) == statement
        assert price_currency(financials) == price

    def test_info_keys_includes_currency(self):
        from src.models.financials import INFO_KEYS

        assert "currency" in INFO_KEYS and "financialCurrency" in INFO_KEYS
