"""The EDGAR statement source, and the four ways it could be silently wrong.

Every test here runs against canned payloads through a fake `urlopen`, so the file
touches no network and `--use-offline` stays provably network-free.

The traps are not hypothetical. Each was found against live SEC data while building the
module, and each produces a plausible number rather than an error:

  a filer carrying both taxonomies, where the US GAAP one froze years ago
  the same fiscal year restated across several filings
  a canonical field with no single tag, needing composition
  a tag that stops being used, leaving the newest year to a later candidate
"""

from __future__ import annotations

import io
import json

import pytest

from src.fetcher import edgar
from src.models.errors import ValuationError


def _duration(end: str, val: float, *, fy: int, filed: str, start: str, form: str = "10-K"):
    return {
        "start": start,
        "end": end,
        "val": val,
        "fy": fy,
        "fp": "FY",
        "form": form,
        "filed": filed,
        "accn": f"acc-{fy}",
    }


def _instant(end: str, val: float, *, fy: int, filed: str, form: str = "10-K"):
    return {
        "end": end,
        "val": val,
        "fy": fy,
        "fp": "FY",
        "form": form,
        "filed": filed,
        "accn": f"acc-{fy}",
    }


def _facts(taxonomies: dict) -> dict:
    return {"entityName": "Test Co", "facts": taxonomies}


class _Response(io.BytesIO):
    """Just enough of an HTTP response for `with urlopen(...) as r: r.read()`."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture
def fake_network(monkeypatch):
    """Serve canned JSON for any URL, and record the headers sent.

    Patches `urllib.request.urlopen` on the real module rather than swapping the module
    in `sys.modules`: `_get_json` does `import urllib.request` inside the function, and
    that resolves through the already-imported `urllib` package's attribute, so a
    sys.modules substitution never takes effect.
    """
    import urllib.request

    sent: dict = {}

    def _install(payload_by_url: dict):
        def fake_urlopen(request, timeout=None):
            sent["User-Agent"] = request.get_header("User-agent")
            sent["url"] = request.full_url
            if request.full_url not in payload_by_url:
                raise AssertionError(f"unexpected URL requested: {request.full_url}")
            body = payload_by_url[request.full_url]
            if isinstance(body, Exception):
                raise body
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            return _Response(raw)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        return sent

    return _install


class TestUserAgent:
    """SEC refuses a User-Agent with no contact address, with a bare 403 and no hint."""

    def test_default_carries_a_contact_address(self):
        assert "@" in edgar.DEFAULT_USER_AGENT

    def test_environment_variable_overrides_it(self, monkeypatch):
        monkeypatch.setenv(edgar.USER_AGENT_ENV, "My Firm me@example.org")
        assert edgar.user_agent() == "My Firm me@example.org"

    def test_blank_environment_variable_falls_back(self, monkeypatch):
        monkeypatch.setenv(edgar.USER_AGENT_ENV, "   ")
        assert edgar.user_agent() == edgar.DEFAULT_USER_AGENT

    def test_the_header_is_actually_sent(self, fake_network, monkeypatch):
        monkeypatch.setenv(edgar.USER_AGENT_ENV, "My Firm me@example.org")
        sent = fake_network({edgar.SEC_TICKERS_URL: {"0": {"ticker": "X", "cik_str": 7}}})
        edgar.resolve_cik("X")
        assert sent["User-Agent"] == "My Firm me@example.org"


class TestResolveCik:
    def test_finds_the_ticker(self, fake_network):
        fake_network({edgar.SEC_TICKERS_URL: {"0": {"ticker": "PG", "cik_str": 80424}}})
        assert edgar.resolve_cik("pg") == 80424

    def test_unlisted_ticker_explains_why(self, fake_network):
        """Tencent trades as an unsponsored ADR and files nothing with the SEC."""
        fake_network({edgar.SEC_TICKERS_URL: {"0": {"ticker": "PG", "cik_str": 80424}}})
        with pytest.raises(ValuationError, match="not in the SEC's ticker map"):
            edgar.resolve_cik("TCEHY")

    def test_network_failure_names_the_way_out(self, fake_network):
        fake_network({edgar.SEC_TICKERS_URL: ConnectionError("no route to host")})
        with pytest.raises(ValuationError, match="--source yahoo"):
            edgar.resolve_cik("PG")

    def test_malformed_payload_names_the_way_out(self, fake_network):
        fake_network({edgar.SEC_TICKERS_URL: b"<html>503</html>"})
        with pytest.raises(ValuationError, match="--source yahoo"):
            edgar.resolve_cik("PG")


class TestTaxonomySelection:
    """Honda's us-gaap revenue stops at 2014 and Toyota's at 2020; both moved to IFRS."""

    def _both_taxonomies(self):
        return _facts(
            {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "JPY": [
                                _duration(
                                    "2014-03-31", 3_097_246e6,
                                    fy=2014, filed="2014-06-24", start="2013-04-01",
                                    form="20-F",
                                )
                            ]
                        }
                    }
                },
                "ifrs-full": {
                    "Revenue": {
                        "units": {
                            "JPY": [
                                _duration(
                                    "2025-03-31", 21_688_767e6,
                                    fy=2025, filed="2025-06-20", start="2024-04-01",
                                    form="20-F",
                                )
                            ]
                        }
                    }
                },
            }
        )

    def test_picks_the_taxonomy_with_the_more_recent_revenue(self):
        assert edgar.select_taxonomy(self._both_taxonomies()) == "ifrs-full"

    def test_picks_us_gaap_when_it_is_the_current_one(self):
        facts = _facts(
            {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                _duration(
                                    "2025-12-31", 100.0,
                                    fy=2025, filed="2026-02-01", start="2025-01-01",
                                )
                            ]
                        }
                    }
                },
                "ifrs-full": {
                    "Revenue": {
                        "units": {
                            "USD": [
                                _duration(
                                    "2019-12-31", 50.0,
                                    fy=2019, filed="2020-02-01", start="2019-01-01",
                                )
                            ]
                        }
                    }
                },
            }
        )
        assert edgar.select_taxonomy(facts) == "us-gaap"

    def test_no_revenue_anywhere_refuses(self):
        with pytest.raises(ValuationError, match="no annual revenue fact"):
            edgar.select_taxonomy(_facts({"us-gaap": {}}))

    def test_a_stale_us_gaap_series_does_not_reach_the_statements(self):
        """The whole point: the 2014 figure must not be presented as current."""
        frames, meta = edgar.build_statements(self._both_taxonomies())
        assert meta["taxonomy"] == "ifrs-full"
        revenue = frames["income"].loc["Total Revenue"]
        assert revenue.index.max().year == 2025
        assert 3_097_246e6 not in list(revenue.values)


class TestRestatedPeriodsCollapse:
    """Each later filing repeats prior years as comparatives, so a period appears many
    times. The most recently filed version is the company's current view of that year."""

    def test_latest_filed_wins(self):
        node = {
            "units": {
                "USD": [
                    _duration("2024-06-30", 111.0, fy=2024, filed="2024-08-05",
                              start="2023-07-01"),
                    _duration("2024-06-30", 999.0, fy=2025, filed="2025-08-04",
                              start="2023-07-01"),
                    _duration("2024-06-30", 222.0, fy=2023, filed="2023-08-06",
                              start="2023-07-01"),
                ]
            }
        }
        rows = edgar._annual_rows(node, instant=False)
        assert rows["2024-06-30"]["val"] == 999.0

    def test_one_row_per_period(self):
        node = {
            "units": {
                "USD": [
                    _duration("2024-06-30", 1.0, fy=2024, filed="2024-08-05",
                              start="2023-07-01"),
                    _duration("2024-06-30", 2.0, fy=2025, filed="2025-08-04",
                              start="2023-07-01"),
                    _duration("2025-06-30", 3.0, fy=2025, filed="2025-08-04",
                              start="2024-07-01"),
                ]
            }
        }
        assert sorted(edgar._annual_rows(node, instant=False)) == ["2024-06-30", "2025-06-30"]


class TestOnlyAnnualFactsSurvive:
    def test_a_quarter_mislabelled_FY_is_rejected_on_its_span(self):
        node = {
            "units": {
                "USD": [
                    _duration("2025-03-31", 25.0, fy=2025, filed="2025-04-30",
                              start="2025-01-01"),
                ]
            }
        }
        assert edgar._annual_rows(node, instant=False) == {}

    def test_a_full_year_is_kept(self):
        node = {
            "units": {
                "USD": [
                    _duration("2025-12-31", 100.0, fy=2025, filed="2026-02-01",
                              start="2025-01-01"),
                ]
            }
        }
        assert "2025-12-31" in edgar._annual_rows(node, instant=False)

    def test_a_10q_is_rejected_on_its_form(self):
        node = {
            "units": {
                "USD": [
                    _duration("2025-12-31", 100.0, fy=2025, filed="2026-02-01",
                              start="2025-01-01", form="10-Q"),
                ]
            }
        }
        assert edgar._annual_rows(node, instant=False) == {}

    def test_instant_facts_carry_no_span_and_must_not_be_filtered_on_one(self):
        """Balance-sheet facts have no `start`. Applying the duration bound to them
        would drop every balance sheet in the payload."""
        node = {"units": {"USD": [_instant("2025-12-31", 42.0, fy=2025, filed="2026-02-01")]}}
        assert edgar._annual_rows(node, instant=True)["2025-12-31"]["val"] == 42.0
        assert edgar._annual_rows(node, instant=False) == {}


class TestCandidatesMergePerPeriod:
    """A filer that migrates between tags leaves the newest year to a later candidate.

    P&G stopped tagging `CashAndCashEquivalentsAtCarryingValue`; taking the first tag
    with any data returned cash from an earlier fiscal year as though it were current,
    a 57% understatement with no error raised.
    """

    def _migrating_filer(self):
        return {
            "OldCashTag": {
                "units": {
                    "USD": [
                        _instant("2024-06-30", 4_239e6, fy=2024, filed="2024-08-05"),
                    ]
                }
            },
            "NewCashTag": {
                "units": {
                    "USD": [
                        _instant("2024-06-30", 4_239e6, fy=2024, filed="2024-08-05"),
                        _instant("2025-06-30", 9_942e6, fy=2025, filed="2025-08-04"),
                    ]
                }
            },
        }

    def test_the_later_candidate_fills_the_period_the_first_lacks(self):
        series, tags = edgar._series_for(
            self._migrating_filer(), ("OldCashTag", "NewCashTag"), instant=True
        )
        assert series["2025-06-30"] == 9_942e6
        assert series["2024-06-30"] == 4_239e6
        assert "NewCashTag" in tags

    def test_the_earlier_candidate_still_wins_the_periods_it_covers(self):
        facts = {
            "Preferred": {"units": {"USD": [_instant("2025-06-30", 1.0, fy=2025, filed="x")]}},
            "Fallback": {"units": {"USD": [_instant("2025-06-30", 2.0, fy=2025, filed="x")]}},
        }
        series, _ = edgar._series_for(facts, ("Preferred", "Fallback"), instant=True)
        assert series["2025-06-30"] == 1.0

    def test_absent_everywhere_returns_nothing_rather_than_zero(self):
        series, tag = edgar._series_for({}, ("A", "B"), instant=True)
        assert series == {} and tag is None


class TestComposedTotalDebt:
    """Apple has no combined-debt tag: it is the sum of three separate ones."""

    def _apple_debt(self):
        return {
            "LongTermDebtNoncurrent": {
                "units": {"USD": [_instant("2025-09-27", 78_328e6, fy=2025, filed="2025-10-31")]}
            },
            "LongTermDebtCurrent": {
                "units": {"USD": [_instant("2025-09-27", 12_350e6, fy=2025, filed="2025-10-31")]}
            },
            "CommercialPaper": {
                "units": {"USD": [_instant("2025-09-27", 7_979e6, fy=2025, filed="2025-10-31")]}
            },
        }

    def test_components_sum(self):
        totals, used = edgar._composed_series(
            self._apple_debt(), edgar.COMPOSED_FIELDS["total_debt"]["us-gaap"]
        )
        assert totals["2025-09-27"] == pytest.approx(98_657e6)
        assert len(used) == 3

    def test_missing_components_are_skipped_not_zeroed(self):
        facts = {
            "LongTermDebtNoncurrent": {
                "units": {"USD": [_instant("2025-09-27", 78_328e6, fy=2025, filed="x")]}
            }
        }
        totals, used = edgar._composed_series(
            facts, edgar.COMPOSED_FIELDS["total_debt"]["us-gaap"]
        )
        assert totals["2025-09-27"] == pytest.approx(78_328e6)
        assert used == ["LongTermDebtNoncurrent"]

    def test_no_components_at_all_leaves_the_period_absent(self):
        """Absent must not become a zero -- a zero reads downstream as debt-free."""
        totals, used = edgar._composed_series({}, ("A", "B"))
        assert totals == {} and used == []


class TestBuildStatements:
    def _minimal(self):
        return _facts(
            {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {
                            "USD": [
                                _duration("2025-12-31", 1000.0, fy=2025,
                                          filed="2026-02-01", start="2025-01-01")
                            ]
                        }
                    },
                    "OperatingIncomeLoss": {
                        "units": {
                            "USD": [
                                _duration("2025-12-31", 200.0, fy=2025,
                                          filed="2026-02-01", start="2025-01-01")
                            ]
                        }
                    },
                    "CashAndCashEquivalentsAtCarryingValue": {
                        "units": {"USD": [_instant("2025-12-31", 50.0, fy=2025,
                                                   filed="2026-02-01")]}
                    },
                }
            }
        )

    def test_rows_use_the_labels_field_map_resolves(self):
        """Emitting the canonical Yahoo vocabulary is what lets an EDGAR fixture run
        through exactly the same normalizer as a Yahoo one, with no second code path."""
        frames, _ = edgar.build_statements(self._minimal())
        assert "Total Revenue" in frames["income"].index
        assert "Operating Income" in frames["income"].index
        assert "Cash And Cash Equivalents" in frames["balance"].index

    def test_periods_are_timestamps_not_strings(self):
        """EDGAR returns ISO strings; Yahoo frames carry Timestamps. Leaving these as
        strings made a cross-source comparison report zero overlapping periods rather
        than a mismatch, which is the quieter failure."""
        import pandas as pd

        frames, _ = edgar.build_statements(self._minimal())
        assert all(isinstance(c, pd.Timestamp) for c in frames["income"].columns)

    def test_provenance_records_the_real_tag(self):
        _, meta = edgar.build_statements(self._minimal())
        assert meta["tags"]["ebit"] == "us-gaap:OperatingIncomeLoss"
        assert meta["taxonomy"] == "us-gaap"

    def test_the_canonical_frame_resolves_through_the_normalizer(self):
        """End to end: EDGAR labels in, canonical field names out."""
        from src.fetcher.normalizer import FinancialNormalizer

        frames, _ = edgar.build_statements(self._minimal())
        canonical = FinancialNormalizer.canonicalize(frames)
        assert canonical.loc["revenue"].iloc[-1] == pytest.approx(1000.0)
        assert canonical.loc["ebit"].iloc[-1] == pytest.approx(200.0)


class TestEntityShares:
    def test_absent_dei_block_returns_none_rather_than_raising(self):
        """PetroChina's `dei` block is empty. That is a coverage limit, not an error."""
        assert edgar._entity_shares(_facts({"us-gaap": {}})) is None

    def test_latest_cover_page_count_is_reported(self):
        facts = _facts(
            {
                "dei": {
                    "EntityCommonStockSharesOutstanding": {
                        "units": {
                            "shares": [
                                _instant("2025-07-18", 15_000e6, fy=2025, filed="x"),
                                _instant("2026-07-17", 14_594_180_000.0, fy=2026, filed="y"),
                            ]
                        }
                    }
                }
            }
        )
        out = edgar._entity_shares(facts)
        assert out == {"value": 14_594_180_000.0, "as_of": "2026-07-17"}
