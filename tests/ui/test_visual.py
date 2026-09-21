"""Screenshot baselines for the surfaces styles.css can silently break.

Marked ``visual`` and excluded from the default run. Baselines are captured per
platform (see visual.py), so the first run on a new machine writes them and
skips. Regenerate deliberately with:

    pytest tests/ui -m visual --update-visual-baselines

Playwright disables animations and hides the caret, and the seeded database is
fixed, so the only remaining source of churn is a genuine rendering change.
"""

from __future__ import annotations

import pytest

from conftest import sign_in_as_owner, wait_for_results
from visual import baseline_path, compare, write_baseline

pytestmark = pytest.mark.visual

# Any element whose content is time-dependent gets masked; otherwise "checked
# 2 hours ago" turns every baseline stale overnight.
# The Urgent badge counts dated items relative to today, so it is masked too.
VOLATILE_SELECTORS = (".source-line", "#page-status", "#urgent-badge")


def _check(page, name: str, request, tmp_path_factory, full_page: bool = False) -> None:
    # The app sends `style-src 'self'`, so an injected stylesheet is blocked. Use
    # Playwright's own stabilisation instead: `animations="disabled"` finishes CSS
    # animations and transitions, and `caret="hide"` removes the blinking cursor.
    masks = [page.locator(selector) for selector in VOLATILE_SELECTORS]
    image = page.screenshot(
        full_page=full_page, mask=masks, animations="disabled", caret="hide"
    )

    if request.config.getoption("--update-visual-baselines"):
        target = write_baseline(name, image)
        pytest.skip(f"baseline rewritten: {target}")
    if not baseline_path(name).exists():
        target = write_baseline(name, image)
        pytest.skip(f"no baseline for this platform yet; wrote {target}. Commit it to enable the check.")

    artifacts = tmp_path_factory.getbasetemp() / "visual"
    result = compare(name, image, artifacts)
    assert result.within_tolerance, (
        f"{name}: {result.changed_pixels} of {result.total_pixels} pixels changed "
        f"({result.fraction:.4%}, limit {0.002:.2%}). Diff: {result.diff_path}"
    )


def test_sign_in_gate_appearance(page, request, tmp_path_factory):
    page.goto("/")
    page.wait_for_selector("#auth-gate.is-visible")
    _check(page, "sign-in-gate", request, tmp_path_factory)


def test_discover_deck_appearance(owner_page, request, tmp_path_factory):
    _check(owner_page, "discover-deck", request, tmp_path_factory)


def test_detail_panel_appearance(owner_page, request, tmp_path_factory):
    owner_page.locator(".opportunity-card .card-button").first.click()
    owner_page.wait_for_selector("#detail-panel.is-open")
    owner_page.wait_for_timeout(250)  # let the slide-over settle at its final offset
    _check(owner_page, "detail-panel", request, tmp_path_factory)


@pytest.mark.parametrize("width,label", [(375, "mobile"), (768, "tablet")])
def test_narrow_layout_appearance(page, request, tmp_path_factory, width, label):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto("/")
    sign_in_as_owner(page)
    wait_for_results(page)
    _check(page, f"discover-{label}", request, tmp_path_factory, full_page=True)


# Urgent's rows are relative to today, so the screenshot uses a fixed payload
# rather than the live queue. Everything else on the page is real.
URGENT_FIXTURE = {
    "today": "2026-09-17", "timezone": "America/Chicago", "utc_offset": "-05:00", "window_days": 14,
    "counts": {"overdue": 1, "upcoming": 3, "attention": 2},
    "older_overdue": 0, "skipped_count": 0, "skipped": [],
    "items": [
        {"key": "application_follow_up:app-1", "kind": "application_follow_up", "date": "2026-09-15",
         "date_source": "Follow-up date", "overdue": True, "days_until": -2, "title": "Controls Co-op",
         "subtitle": None, "company": "Orbit Systems", "source_name": None, "opportunity_id": "job-b",
         "application_id": "app-job-b", "outreach_target_id": None, "task_id": None, "saved": False, "stage": "applied"},
        {"key": "your_deadline:job-a", "kind": "your_deadline", "date": "2026-09-18",
         "date_source": "You entered", "overdue": False, "days_until": 1, "title": "Mechanical Engineering Intern",
         "subtitle": None, "company": "Acme Robotics", "source_name": None, "opportunity_id": "job-a",
         "application_id": None, "outreach_target_id": None, "task_id": None, "saved": True, "stage": None},
        {"key": "outreach_deadline:o-1", "kind": "outreach_deadline", "date": "2026-09-22",
         "date_source": "Outreach record deadline", "overdue": False, "days_until": 5, "title": "Bovi Robotics",
         "subtitle": None, "company": "Bovi Robotics", "source_name": None, "opportunity_id": None,
         "application_id": None, "outreach_target_id": "o-1", "task_id": None, "saved": False, "stage": "not_started"},
        {"key": "task:t-1", "kind": "task", "date": "2026-09-26", "date_source": "Task due", "overdue": False,
         "days_until": 9, "title": "Prep for phone screen", "subtitle": "Controls Co-op", "company": "Orbit Systems",
         "source_name": None, "opportunity_id": "job-b", "application_id": "app-job-b", "outreach_target_id": None,
         "task_id": "t-1", "saved": False, "stage": "applied"},
    ],
}


@pytest.mark.parametrize("width,label", [(1280, "desktop"), (375, "mobile")])
def test_urgent_appearance(page, request, tmp_path_factory, width, label):
    import json

    page.set_viewport_size({"width": width, "height": 900})
    page.route("**/api/v1/urgent*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(URGENT_FIXTURE)
    ))
    page.goto("/urgent")
    sign_in_as_owner(page)
    wait_for_results(page)
    page.wait_for_selector(".urgent-row")
    _check(page, f"urgent-{label}", request, tmp_path_factory, full_page=True)
