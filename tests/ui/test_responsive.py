"""Layout integrity across the viewport range styles.css actually targets.

styles.css has breakpoints at 960px and below, and the sidebar collapses to a
76px icon rail. These tests assert the properties that break first when CSS
changes: horizontal overflow, controls pushed off-screen, and tap targets that
shrink below the 24x24 CSS-pixel floor in WCAG 2.2 SC 2.5.8.
"""

from __future__ import annotations

import re
from typing import NamedTuple

import pytest
from playwright.sync_api import expect

from conftest import AUTHENTICATED_VIEWS, record_quarantined, sign_in_as_owner, wait_for_results

VIEWPORTS = {
    "mobile": {"width": 375, "height": 812},
    "tablet": {"width": 768, "height": 1024},
    # Upper end of the 721-960px icon rail, where nav labels are visually hidden.
    "rail": {"width": 900, "height": 1000},
    "laptop": {"width": 1280, "height": 900},
    "wide": {"width": 1680, "height": 1050},
}

# WCAG 2.2 SC 2.5.8 (Target Size, Minimum). Inline links in prose are exempt.
MIN_TARGET_PX = 24

# Keep this explicit: the phase-verification pass on 2026-08-23 cleared the
# previous backlog, and future undersized controls must fail until documented.
KNOWN_UNDERSIZED: set[str] = set()


def _horizontal_overflow(page) -> int:
    """Pixels the document scrolls sideways. Anything above zero is a layout bug."""
    return page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )


class SizedPage(NamedTuple):
    page: object
    label: str


@pytest.fixture(params=sorted(VIEWPORTS), ids=sorted(VIEWPORTS))
def sized(request, page, base_url) -> SizedPage:
    page.set_viewport_size(VIEWPORTS[request.param])
    page.goto("/")
    sign_in_as_owner(page)
    wait_for_results(page)
    return SizedPage(page, request.param)


@pytest.mark.parametrize("view", AUTHENTICATED_VIEWS)
def test_no_view_scrolls_sideways_on_a_phone(page, base_url, view):
    """Found in the 2026-09-17 polish pass: Profile was 580px wide at 375px, so
    its cards (and anything placed beside them) ran off the right edge."""
    page.set_viewport_size(VIEWPORTS["mobile"])
    page.goto("/")
    sign_in_as_owner(page)
    wait_for_results(page)
    page.click(f"#{view}-nav")
    wait_for_results(page)
    overflow = _horizontal_overflow(page)
    assert overflow <= 0, f"the {view} view overflows a 375px phone by {overflow}px"


def test_workspace_never_scrolls_sideways(sized: SizedPage):
    overflow = _horizontal_overflow(sized.page)
    assert overflow <= 0, (
        f"the {sized.label} layout overflows horizontally by {overflow}px; "
        "a fixed width or an unwrapped element is escaping its column"
    )


def test_primary_navigation_keeps_accessible_names(sized: SizedPage):
    """The icon rail hides the text labels; screen readers must still hear them."""
    for label in ("Discover", "Urgent", "Saved", "Applications", "Outreach", "Prepare", "Agent", "Profile"):
        # Urgent's name carries its count ("Urgent, 2 need attention") but must start with the label.
        name = re.compile(rf"^{label}(, \d+ needs? attention)?$") if label == "Urgent" else label
        button = sized.page.get_by_role("navigation").get_by_role("button", name=name, exact=True)
        assert button.count() == 1, f"no nav button named {label!r} at {sized.label}"
    # Programs is named from the student's own list (tests/fixtures/early_programs.json).
    expect(sized.page.locator("#programs-nav")).to_have_accessible_name("Sandbox")


def test_primary_navigation_stays_reachable(sized: SizedPage):
    """At <=960px the labels are hidden by design, but the buttons must stay hittable."""
    for view in ("discover", "urgent", "saved", "applications", "programs", "outreach", "prepare", "agent", "profile"):
        nav = sized.page.locator(f"#{view}-nav")
        assert nav.is_visible(), f"#{view}-nav is not visible at {sized.label}"
        box = nav.bounding_box()
        assert box is not None, f"#{view}-nav has no layout box at {sized.label}"
        assert box["x"] >= 0, f"#{view}-nav starts off-screen at x={box['x']}"


def test_interactive_controls_meet_the_minimum_target_size(sized: SizedPage):
    """Undersized controls are the most common mobile defect in a desktop-first CSS file.

    The hit area is not always the control's own box: an ``<input>`` wrapped in a
    styled ``<label>`` (``.search-field`` sets ``min-height: 54px``) is clickable
    across the whole label. The measurement therefore uses the wrapping label when
    there is one, which is what a finger actually hits.
    """
    undersized = sized.page.evaluate(
        """
        (minimum) => {
          const selector = 'button:not([hidden]), select, input:not([type=hidden]), summary';
          const hitArea = (node) => {
            const label = node.closest('label');
            return (label || node).getBoundingClientRect();
          };
          return Array.from(document.querySelectorAll(selector))
            .filter((node) => {
              const rect = hitArea(node);
              if (rect.width === 0 && rect.height === 0) return false;   // not rendered
              return rect.width < minimum || rect.height < minimum;
            })
            .map((node) => {
              const rect = hitArea(node);
              const id = node.id ? `#${node.id}` : `${node.tagName.toLowerCase()}.${node.className}`;
              return { id, size: `${Math.round(rect.width)}x${Math.round(rect.height)}` };
            });
        }
        """,
        MIN_TARGET_PX,
    )
    unexpected = []
    for entry in undersized:
        if entry["id"] in KNOWN_UNDERSIZED:
            record_quarantined("target-size", f"{entry['id']} at {sized.label}")
        else:
            unexpected.append(f"{entry['id']} ({entry['size']})")
    assert not unexpected, (
        f"controls below {MIN_TARGET_PX}x{MIN_TARGET_PX}px at {sized.label}: "
        + ", ".join(unexpected)
    )


def test_sign_out_is_reachable_at_every_viewport(sized: SizedPage):
    """Wider layouts keep Sign out in the sidebar. Phones give the bottom bar to
    the eight destinations, so Sign out lives at the top of Profile instead;
    it must be fully on screen, named, keyboard-reachable, and working there."""
    page = sized.page
    if VIEWPORTS[sized.label]["width"] > 720:
        assert page.locator("#logout-button").is_visible(), f"no sign-out control is visible at {sized.label}"
        return
    page.click("#profile-nav")
    control = page.get_by_role("button", name="Sign out", exact=True)
    control.wait_for(state="visible")
    assert control.count() == 1, "exactly one Sign out control should be exposed on a phone"
    box = control.bounding_box()
    viewport = page.viewport_size
    assert box and box["x"] >= 0 and box["y"] >= 0, f"Sign out starts off-screen: {box}"
    assert box["x"] + box["width"] <= viewport["width"], f"Sign out runs off the right edge: {box}"
    control.focus()
    assert page.evaluate("document.activeElement.textContent.trim()") == "Sign out"
    control.press("Enter")
    page.wait_for_selector("#auth-gate.is-visible")


def test_phone_bottom_bar_fits_every_destination(sized: SizedPage):
    """Every nav button shares the phone bar: each fully on screen, none overlapping."""
    if VIEWPORTS[sized.label]["width"] > 720:
        pytest.skip("the bottom bar exists only at phone widths")
    boxes = []
    viewport = sized.page.viewport_size
    rendered = sized.page.locator(".nav-list .nav-item").evaluate_all("(nodes) => nodes.map((node) => node.id)")
    assert rendered == [f"{view}-nav" for view in ("discover", "urgent", "saved", "applications", "programs", "outreach", "prepare", "agent", "profile")], "the bar's destinations changed; update this list"
    for view in ("discover", "urgent", "saved", "applications", "programs", "outreach", "prepare", "agent", "profile"):
        box = sized.page.locator(f"#{view}-nav").bounding_box()
        assert box, f"#{view}-nav has no layout box"
        assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"], f"#{view}-nav is clipped: {box}"
        assert box["y"] + box["height"] <= viewport["height"], f"#{view}-nav is below the fold: {box}"
        assert box["width"] >= MIN_TARGET_PX and box["height"] >= MIN_TARGET_PX, f"#{view}-nav is too small: {box}"
        boxes.append((view, box))
    for (left, a), (right, b) in zip(boxes, boxes[1:]):
        assert a["x"] + a["width"] <= b["x"] + 0.5, f"#{left}-nav overlaps #{right}-nav"


# The sign-in card is 688px tall before anything is expanded, and opening both
# <details> takes it past 1550px. These run pre-authentication, so they use `page`
# and their own viewport rather than the signed-in `sized` fixture.
AUTH_VIEWPORTS = [(1280, 900, "laptop"), (1366, 768, "laptop-short"), (375, 812, "mobile")]


@pytest.mark.parametrize("width,height,label", AUTH_VIEWPORTS)
def test_every_auth_gate_control_stays_reachable_when_expanded(page, base_url, width, height, label):
    """Expanding both disclosures must not put controls beyond reach.

    The gate is `position: fixed; inset: 0`, so if it is not a scroll container
    its overflow is simply unreachable — the document behind it scrolls, but the
    fixed overlay does not move. Clicking the second summary is the real user
    action that fails: Chromium reports "element is outside of the viewport"
    after trying and failing to scroll it into view.
    """
    page.set_viewport_size({"width": width, "height": height})
    page.goto("/")
    page.wait_for_selector("#auth-gate.is-visible")

    page.locator("#auth-form details > summary").first.click()
    page.locator("#auth-form details > summary").nth(1).click()

    for control in ("#register-submit", "#recovery-complete"):
        target = page.locator(control)
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        assert box is not None, f"{control} has no layout box at {label}"
        # bounding_box() reports viewport-relative x/y/width/height.
        assert box["y"] >= 0 and box["y"] + box["height"] <= height, (
            f"{control} cannot be scrolled into view at {label}: "
            f"it sits at y={round(box['y'])}..{round(box['y'] + box['height'])} "
            f"in a {height}px viewport"
        )


@pytest.mark.parametrize("width,height,label", AUTH_VIEWPORTS)
def test_auth_gate_scrolls_when_its_content_overflows(page, base_url, width, height, label):
    """The overlay itself must scroll, not the page behind it."""
    page.set_viewport_size({"width": width, "height": height})
    page.goto("/")
    page.wait_for_selector("#auth-gate.is-visible")
    page.evaluate("() => document.querySelectorAll('#auth-form details').forEach((d) => { d.open = true; })")

    state = page.evaluate(
        """
        () => {
          const gate = document.getElementById('auth-gate');
          return {
            overflowing: gate.scrollHeight - gate.clientHeight,
            overflowY: getComputedStyle(gate).overflowY,
          };
        }
        """
    )
    if state["overflowing"] <= 0:
        pytest.skip(f"the expanded card already fits at {label}; nothing to scroll")
    assert state["overflowY"] in {"auto", "scroll"}, (
        f"#auth-gate overflows by {state['overflowing']}px at {label} but its "
        f"overflow-y is '{state['overflowY']}', so the overflow cannot be reached"
    )

    page.mouse.move(width // 2, height // 2)
    page.mouse.wheel(0, 2000)
    page.wait_for_timeout(250)
    assert page.evaluate("() => document.getElementById('auth-gate').scrollTop") > 0, (
        f"scrolling did not move the gate at {label}"
    )


def test_detail_panel_fits_the_viewport(sized: SizedPage):
    """On a phone the slide-over must not sit partly off-screen or trap content."""
    sized.page.locator(".opportunity-card .card-button").first.click()
    sized.page.wait_for_selector("#detail-panel.is-open")
    panel = sized.page.locator("#detail-panel").bounding_box()
    viewport = sized.page.viewport_size
    assert panel is not None
    assert panel["width"] <= viewport["width"] + 1, (
        f"the detail panel is {panel['width']}px wide in a {viewport['width']}px viewport"
    )
    assert _horizontal_overflow(sized.page) <= 0, (
        "opening the detail panel introduced horizontal overflow"
    )
