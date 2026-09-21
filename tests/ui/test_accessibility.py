"""axe-core accessibility scans of every page and every authenticated view.

axe catches the mechanical WCAG failures — unlabelled controls, insufficient
contrast, broken ARIA relationships, missing landmarks. It cannot judge whether a
flow makes sense, so it complements rather than replaces the keyboard and
responsive suites next to it.

Scans are scoped to WCAG 2.1 A/AA, which is the level a product this size can
realistically hold. ``KNOWN_VIOLATIONS`` is the quarantine list: an entry there
is a real defect that is not fixed yet, so it does not fail the build — but it is
still reported in the run summary under "known UI defects", and any rule *not*
listed fails immediately.
"""

from __future__ import annotations

import json

import pytest
from axe_core_python.sync_playwright import Axe

from conftest import AUTHENTICATED_VIEWS, record_quarantined, wait_for_results

AXE_OPTIONS = {
    "runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]},
}

# Keep this explicit: a future quarantine must name and document a real defect.
# The phase-verification pass on 2026-08-23 cleared the previous backlog.
KNOWN_VIOLATIONS: dict[str, str] = {}


def _scan(page, context=None) -> list[dict]:
    results = Axe().run(page, context=context, options=AXE_OPTIONS)
    return results.get("violations", [])


def _describe(violations: list[dict]) -> str:
    lines = []
    for violation in violations:
        targets = ", ".join(
            str(target) for node in violation["nodes"][:4] for target in node.get("target", [])
        )
        lines.append(
            f"[{violation['impact'] or 'unknown'}] {violation['id']}: {violation['help']}\n"
            f"    affects: {targets}\n"
            f"    docs: {violation['helpUrl']}"
        )
    return "\n".join(lines)


def _assert_accessible(page, label: str) -> None:
    violations = _scan(page)
    unexpected = []
    for violation in violations:
        if violation["id"] in KNOWN_VIOLATIONS:
            record_quarantined(violation["id"], label)
        else:
            unexpected.append(violation)
    if unexpected:
        pytest.fail(
            f"axe found {len(unexpected)} violation(s) on {label}:\n{_describe(unexpected)}",
            pytrace=False,
        )


def test_sign_in_gate_is_accessible(page):
    """The auth card is the first thing every user meets, including keyboard users."""
    page.goto("/")
    page.wait_for_selector("#auth-gate.is-visible")
    _assert_accessible(page, "the sign-in gate")


def test_public_market_page_is_accessible(page):
    page.goto("/market")
    page.wait_for_selector("#market-issues")
    _assert_accessible(page, "/market")


@pytest.mark.parametrize("view", AUTHENTICATED_VIEWS)
def test_authenticated_view_is_accessible(owner_page, view):
    owner_page.click(f"#{view}-nav")
    wait_for_results(owner_page)
    _assert_accessible(owner_page, f"the {view} view")


@pytest.mark.parametrize("view", AUTHENTICATED_VIEWS)
def test_authenticated_view_is_accessible_in_dark_mode(owner_page, view):
    """Dark mode swaps every color token, so contrast is re-checked per view."""
    owner_page.evaluate("document.documentElement.dataset.theme = 'dark'")
    owner_page.click(f"#{view}-nav")
    wait_for_results(owner_page)
    _assert_accessible(owner_page, f"the {view} view in dark mode")


def test_opportunity_detail_panel_is_accessible(owner_page):
    """The detail panel is a dialog-shaped surface; scan it in its open state."""
    owner_page.locator(".opportunity-card .card-button").first.click()
    owner_page.wait_for_selector("#detail-panel.is-open")
    owner_page.wait_for_selector("#detail-content .detail-line, #detail-content h2, #detail-content h3")
    _assert_accessible(owner_page, "the opportunity detail panel")


def test_advanced_filters_are_accessible_when_expanded(owner_page):
    """Collapsed <details> content is invisible to axe until it is opened."""
    owner_page.locator(".advanced-filters > summary").click()
    owner_page.wait_for_selector(".advanced-filter-grid")
    _assert_accessible(owner_page, "the expanded advanced filters")


def test_accessibility_report_is_written_for_review(owner_page, tmp_path_factory):
    """Emit the full axe result, including passes, as a CI artifact.

    This test does not gate the build; it exists so a reviewer can read what axe
    actually checked rather than only what it rejected.
    """
    report_dir = tmp_path_factory.getbasetemp() / "axe-reports"
    report_dir.mkdir(exist_ok=True)
    results = Axe().run(owner_page, options=AXE_OPTIONS)
    summary = {
        "url": owner_page.url,
        "violations": len(results.get("violations", [])),
        "passes": len(results.get("passes", [])),
        "incomplete": len(results.get("incomplete", [])),
        "detail": results.get("violations", []),
    }
    (report_dir / "discover.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    assert summary["passes"] > 0, "axe ran no successful checks, which means the scan was misconfigured"
