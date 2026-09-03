"""The standalone HTML dashboard.

`dashboard.py` was the largest module in the repository with no tests at all -- 438
lines. It computes nothing, so it cannot produce a wrong valuation, but it did carry a
real defect of its own: it pulled Tailwind from
`https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js`, a 407KB download from
a dev-channel path on infrastructure the project does not control.

That made a file described in its own header as a "standalone report" silently depend on
a network fetch, in a repository whose headline promise is that everything runs with no
network at all. It fails quietly and completely: no error, no warning, just unstyled HTML
the moment that URL is retired or the reader is offline.

The stylesheet is now inlined. The test that matters is the one below that keeps it
honest -- if someone adds a utility class to the markup and forgets the rule, the page
would silently lose that styling, which is exactly the failure mode the CDN had.
"""

from __future__ import annotations

import re

import pytest

from src.dcf.dashboard import generate_dashboard_html


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> str:
    """Render the dashboard with a representative payload."""
    data = {
        "generated_at": "2026-09-03",
        "companies": [
            {
                "ticker": "AAPL",
                "market_price": 319.70,
                "base_dcf": 120.08,
                "blended_fair": 122.19,
                "wacc": 0.1003,
                "verdict": "Demanding Valuation (Negative Margin of Safety)",
                "moat": {"roic": 0.6097, "spread": 0.5094, "rating": "Wide Moat"},
                "reverse": {"implied_cagr": 0.289},
                "scenarios": {"bear": 80.49, "base": 120.08, "bull": 161.29},
            }
        ],
    }
    path = tmp_path_factory.mktemp("dash") / "d.html"
    generate_dashboard_html(data, path)
    return path.read_text(encoding="utf-8")


class TestNoExternalDependencies:
    """A standalone report has to actually stand alone."""

    def test_no_remote_assets_are_referenced(self, rendered):
        """The whole point of the fix. No script, link or img may reach the network."""
        remote = re.findall(
            r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)["\']', rendered
        )
        assert not remote, f"dashboard fetches remote assets: {remote}"

    def test_the_retired_cdn_is_gone(self, rendered):
        """Comments stripped first: the stylesheet documents the old URL on purpose."""
        live = re.sub(r"/\*.*?\*/", "", rendered, flags=re.DOTALL)
        live = re.sub(r"<!--.*?-->", "", live, flags=re.DOTALL)
        assert "gstatic.com" not in live
        assert "cdn.tailwindcss.com" not in live

    def test_styling_is_actually_present(self, rendered):
        """Removing the CDN is only correct if the CSS replaced it."""
        assert "<style>" in rendered
        style = rendered.split("<style>", 1)[1].split("</style>", 1)[0]
        assert len(style) > 2000, "stylesheet looks too small to have replaced Tailwind"


class TestEveryUtilityClassIsDefined:
    """The failure mode the CDN had: a class with no rule styles nothing, silently."""

    @staticmethod
    def _classes_used(html: str) -> set[str]:
        used: set[str] = set()
        for attr in re.findall(r'class="([^"{}]*)"', html):
            used.update(attr.split())
        return {c for c in used if c}

    @staticmethod
    def _classes_defined(html: str) -> set[str]:
        style = html.split("<style>", 1)[1].split("</style>", 1)[0]
        style = re.sub(r"/\*.*?\*/", "", style, flags=re.DOTALL)

        # An escaped char (\: \. \[ \] \/) is part of the class name; a bare colon
        # starts a pseudo-class and a bare dot starts the next selector, so both end
        # the match. Without that distinction `.hover\:underline:hover` parses as the
        # class "hover:underline:hover" and never matches the "hover:underline" the
        # markup actually uses -- which is how the first run of this test reported a
        # rule as missing when it was present.
        tokens = re.findall(r"\.((?:\\.|[A-Za-z0-9_\[\]/-])+)", style)
        return {re.sub(r"\\(.)", r"\1", tok) for tok in tokens}

    @staticmethod
    def _classes_set_by_script() -> set[str]:
        """Classes assigned in JS, which never appear in a static class attribute.

        Scanning only the rendered markup missed these entirely, and the gap was real:
        the selected-company button is styled by `btn.className = ... "bg-blue-600
        text-white border-blue-500 shadow-lg"`, and with no rule for `bg-blue-600` it
        rendered with the browser's default white background and near-white text.
        Found by looking at the page, not by the test -- hence this half.

        The assignments span multiple lines through `+` concatenation, so the match
        has to run to the semicolon rather than to the end of the line.
        """
        import pathlib

        import src.dcf.dashboard as dashboard_module

        source = pathlib.Path(dashboard_module.__file__).read_text(encoding="utf-8")
        found: set[str] = set()
        for assignment in re.finditer(r"className\s*=\s*(.*?);", source, flags=re.DOTALL):
            for literal in re.findall(r'"([^"]*)"', assignment.group(1)):
                found.update(literal.split())
        return {c for c in found if c and not c.startswith(("$", "{"))}

    def test_no_class_is_used_without_a_rule(self, rendered):
        used = self._classes_used(rendered) | self._classes_set_by_script()
        defined = self._classes_defined(rendered)
        missing = sorted(used - defined)
        assert not missing, (
            f"{len(missing)} class(es) used in the markup have no rule in the inlined "
            f"stylesheet, so they style nothing: {missing}"
        )

    def test_script_assigned_classes_are_actually_found(self):
        """The scan above is worthless if it silently matches nothing."""
        script_classes = self._classes_set_by_script()
        assert len(script_classes) > 10, (
            f"only {len(script_classes)} script-assigned classes parsed; the multi-line "
            f"concatenation regex has probably stopped working"
        )
        assert "bg-blue-600" in script_classes

    def test_the_check_can_actually_fail(self, rendered):
        """Guard against the comparison silently matching everything."""
        used = self._classes_used(rendered)
        defined = self._classes_defined(rendered)
        assert used, "no classes parsed out of the markup -- the regex is broken"
        assert defined, "no rules parsed out of the stylesheet -- the regex is broken"
        assert "definitely-not-a-real-class" not in defined


class TestContent:
    def test_the_payload_reaches_the_page(self, rendered):
        assert "AAPL" in rendered
        assert "120.08" in rendered

    def test_no_stale_hardcoded_test_count(self, rendered):
        """It shipped claiming '261 Tests' while the suite was at 281.

        Any hardcoded count rots the moment a test is added, and this repository has
        already been bitten by exactly that in the README.
        """
        assert not re.search(r"\d+\s*Tests", rendered), (
            "the dashboard hardcodes a test count, which will drift out of date"
        )
