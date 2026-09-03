"""Sector-map peer fallback, used when config supplies no explicit peers.

Found by manually running a live valuation for Micron (MU). Nothing was broken in the
sense of raising or crashing -- the model produced a confident number with a plausible
story -- but the peer set behind it was wrong. `SECTOR_PEERS` keys on Yahoo's *sector*
field, and every semiconductor name (NVDA, AMD, TSM, QCOM, INTC, AVGO, TXN, MU itself)
is filed under the single broad sector "Technology", alongside AAPL, MSFT, GOOGL and
META. A cyclical, capital-intensive chip maker is not a peer of a software platform.

The concrete damage: with the sector-level fallback, Micron's peer-median growth came
back at 16.2% (dragged up by AAPL/MSFT/GOOGL/META/NVDA/ORCL/CRM/ADBE as a group) against
Micron's own 4% terminal growth. At `decay_turns_per_pp = 2.0` that is a 12-point gap
times 2 turns per point, decaying the exit multiple to -6.9x -- caught by the
mature-industry floor, but only after the damage was already done to the number.

`INDUSTRY_PEERS` is checked first, on Yahoo's finer-grained `industry` field, before
falling back to `SECTOR_PEERS`. After the fix, Micron's peers are NVDA, AMD, QCOM,
INTC, AVGO and TXN -- names that actually share its demand and cost structure -- and
the raw decayed multiple moves from -6.9x to +2.0x.
"""

from __future__ import annotations

import warnings

import pytest

from src.comps.comps_engine import INDUSTRY_PEERS, SECTOR_PEERS, CompsEngine
from src.models.assumptions import DCFAssumptions
from tests.conftest import make_financials


def _engine(industry: str | None, sector: str | None, target: str = "MU") -> CompsEngine:
    info: dict[str, str] = {}
    if industry is not None:
        info["industry"] = industry
    if sector is not None:
        info["sector"] = sector
    financials = make_financials(ticker=target, revenue=1000.0, ebit=200.0, info=info)
    return CompsEngine(target, DCFAssumptions(), target_financials=financials)


class TestIndustryTakesPriorityOverSector:
    def test_a_semiconductor_gets_semiconductor_peers_not_the_tech_sector_bucket(self):
        peers, source = _engine("Semiconductors", "Technology").resolve_peers()
        assert source == "industry_map:Semiconductors"
        assert set(peers) <= set(INDUSTRY_PEERS["Semiconductors"])
        # The whole point: none of the sector-level megacaps should show up here.
        assert not set(peers) & {"AAPL", "MSFT", "GOOGL", "META", "ORCL", "CRM", "ADBE"}

    def test_the_target_never_appears_in_its_own_peer_set(self):
        peers, _ = _engine("Semiconductors", "Technology", target="MU").resolve_peers()
        assert "MU" not in peers

    def test_semiconductor_equipment_is_its_own_bucket(self):
        """ASML/AMAT/LRCX/KLAC make equipment; they do not compete with chip makers."""
        peers, source = _engine(
            "Semiconductor Equipment & Materials", "Technology", target="ASML"
        ).resolve_peers()
        assert source == "industry_map:Semiconductor Equipment & Materials"
        assert "NVDA" not in peers

    def test_a_software_company_still_falls_back_to_the_sector_bucket(self):
        """Only industries with a dedicated bucket bypass SECTOR_PEERS -- not all of Tech."""
        peers, source = _engine(
            "Software - Infrastructure", "Technology", target="MSFT"
        ).resolve_peers()
        assert source == "sector_map:Technology"
        assert set(peers) == set(SECTOR_PEERS["Technology"]) - {"MSFT"}

    def test_a_missing_industry_falls_back_to_sector(self):
        peers, source = _engine(None, "Technology", target="MSFT").resolve_peers()
        assert source == "sector_map:Technology"

    def test_a_missing_industry_and_sector_returns_no_peers_not_an_error(self):
        peers, source = _engine(None, None, target="XYZ").resolve_peers()
        assert peers == []
        assert source == "sector_map:unknown"


class TestConfiguredPeersStillWin:
    def test_explicit_peers_bypass_both_maps(self):
        financials = make_financials(
            ticker="MU", revenue=1000.0, ebit=200.0, info={"industry": "Semiconductors"}
        )
        assumptions = DCFAssumptions.model_validate({"comps": {"peers": ["ASML", "AMAT"]}})
        peers, source = CompsEngine("MU", assumptions, target_financials=financials).resolve_peers()
        assert source == "config"
        assert peers == ["ASML", "AMAT"]


class TestMicronEndToEnd:
    """The case that found the defect, run live -- skipped if the network is unavailable."""

    def test_micron_peers_are_semiconductor_names(self):
        from src.fetcher.yfinance_client import YFinanceClient
        from src.models.errors import ValuationError

        warnings.simplefilter("ignore")
        try:
            financials = YFinanceClient("MU").get_financials()
        except ValuationError:
            pytest.skip("network unavailable")

        assumptions = DCFAssumptions.from_yaml()
        result = CompsEngine("MU", assumptions, target_financials=financials).run()

        assert result.peer_source == "industry_map:Semiconductors"
        assert not set(result.peers) & {"AAPL", "MSFT", "GOOGL", "META"}
