"""Keyboard and focus behaviour — the half of UX that axe cannot judge.

axe verifies that a control *has* an accessible name; only a driven browser can
verify that a keyboard user can actually reach it, escape a slide-over, and never
lose the focus ring behind a scrim.
"""

from __future__ import annotations

from playwright.sync_api import expect

from conftest import sign_in_as_owner, wait_for_results


def _focused_description(page) -> str:
    return page.evaluate(
        """
        () => {
          const node = document.activeElement;
          if (!node || node === document.body) return 'body';
          return (node.id && `#${node.id}`) || `${node.tagName.toLowerCase()}.${node.className}`;
        }
        """
    )


def test_skip_link_is_the_first_tab_stop_and_jumps_to_results(owner_page):
    """A skip link that cannot be focused first is decoration, not a skip link."""
    # Signing in left focus on the submit button near the end of the document, and
    # Chromium resumes sequential navigation from there. Only a load resets the
    # focus origin, so reload into the established session before measuring.
    owner_page.reload()
    wait_for_results(owner_page)
    owner_page.keyboard.press("Tab")
    assert _focused_description(owner_page).startswith("a.skip-link"), (
        f"the first tab stop was {_focused_description(owner_page)}, not the skip link"
    )
    owner_page.keyboard.press("Enter")
    assert owner_page.evaluate("() => window.location.hash") == "#results"


def test_sign_in_form_submits_from_the_keyboard_alone(page):
    """Enter inside the token field must submit; a click-only form locks out keyboards."""
    page.goto("/")
    page.wait_for_selector("#auth-gate.is-visible")
    page.locator("#token-input").click()
    page.keyboard.type("ui-suite-owner-token")
    page.keyboard.press("Enter")
    page.wait_for_selector("#auth-gate.is-visible", state="detached", timeout=15_000)
    wait_for_results(page)


def test_every_navigation_item_is_reachable_by_tab(owner_page):
    """Walk the tab order and confirm all eight primary destinations appear in it."""
    expected = {f"#{view}-nav" for view in ("discover", "urgent", "saved", "applications", "outreach", "prepare", "agent", "profile")}
    seen: set[str] = set()
    for _ in range(40):
        owner_page.keyboard.press("Tab")
        seen.add(_focused_description(owner_page))
        if expected <= seen:
            break
    missing = expected - seen
    assert not missing, f"never reached by Tab within 40 stops: {sorted(missing)}"


def test_detail_panel_closes_on_escape_and_restores_focus(owner_page):
    """Opening a slide-over from a card must not strand focus when it closes."""
    trigger = owner_page.locator(".opportunity-card .card-button").first
    trigger.click()
    owner_page.wait_for_selector("#detail-panel.is-open")
    expect(owner_page.locator("#detail-panel")).to_have_attribute("aria-hidden", "false")

    owner_page.keyboard.press("Escape")
    expect(owner_page.locator("#detail-panel")).to_have_attribute("aria-hidden", "true")
    expect(owner_page.locator("#detail-scrim")).to_be_hidden()
    expect(trigger).to_be_focused()


def _tab_stops_stay_inside(page, container: str, presses: int = 12) -> list[str]:
    """Press Shift+Tab and Tab repeatedly and report any stop outside ``container``."""
    escaped: list[str] = []
    for key in ["Shift+Tab"] * presses + ["Tab"] * presses:
        page.keyboard.press(key)
        outside = page.evaluate(
            """(selector) => {
              const node = document.activeElement;
              if (!node || node === document.body || document.querySelector(selector).contains(node)) return null;
              return node.id ? `#${node.id}` : `${node.tagName.toLowerCase()}.${node.className}`;
            }""",
            container,
        )
        if outside:
            escaped.append(outside)
    return escaped


def test_detail_panel_keeps_focus_inside_while_open(owner_page):
    owner_page.locator(".opportunity-card .card-button").first.click()
    expect(owner_page.locator("#detail-close")).to_be_focused()
    expect(owner_page.locator(".app-shell")).to_have_attribute("inert", "")
    escaped = _tab_stops_stay_inside(owner_page, "#detail-panel")
    assert not escaped, f"focus left the open detail panel for: {sorted(set(escaped))}"


def test_detail_panel_keeps_focus_inside_on_a_phone(owner_page):
    """At 375px the panel covers the screen; a hidden Apply link must not be reachable."""
    owner_page.set_viewport_size({"width": 375, "height": 812})
    owner_page.locator(".opportunity-card .card-button").first.click()
    expect(owner_page.locator("#detail-close")).to_be_focused()
    escaped = _tab_stops_stay_inside(owner_page, "#detail-panel")
    assert not escaped, f"focus left the open detail panel for: {sorted(set(escaped))}"


def test_sign_in_gate_keeps_focus_inside_and_hands_it_to_the_page(page):
    page.goto("/")
    page.wait_for_selector("#auth-gate.is-visible")
    expect(page.locator("#auth-email")).to_be_focused()
    escaped = _tab_stops_stay_inside(page, "#auth-gate", presses=8)
    assert not escaped, f"focus left the sign-in gate for: {sorted(set(escaped))}"

    sign_in_as_owner(page)
    wait_for_results(page)
    expect(page.locator("#page-title")).to_be_focused()


def test_unsaving_a_card_keeps_keyboard_focus_on_the_list(owner_page):
    owner_page.click("#saved-nav")
    wait_for_results(owner_page)
    owner_page.locator(".opportunity-card").first.get_by_role("button", name="Saved ✓").click()
    expect(owner_page.locator("#action-status")).to_contain_text("Undid")
    assert _focused_description(owner_page) != "body"


def test_detail_close_button_is_operable_by_keyboard(owner_page):
    owner_page.locator(".opportunity-card .card-button").first.click()
    owner_page.wait_for_selector("#detail-panel.is-open")
    close = owner_page.locator("#detail-close")
    close.focus()
    owner_page.keyboard.press("Enter")
    expect(owner_page.locator("#detail-panel")).to_have_attribute("aria-hidden", "true")


def test_focus_is_always_visible(owner_page):
    """Every keyboard tab stop must show a focus indicator.

    styles.css styles focus through ``:focus-visible``, which Chromium does not
    apply to programmatic ``element.focus()`` on most controls. The indicator can
    therefore only be judged by pressing Tab for real, one stop at a time, and
    reading the computed style of whatever ended up focused. The ring may sit on
    the control itself (``button:focus-visible``) or on a wrapper
    (``.search-field:focus-within``), so an ancestor counts too.
    """
    owner_page.reload()
    wait_for_results(owner_page)

    offenders: set[str] = set()
    seen: set[str] = set()
    for _ in range(60):
        owner_page.keyboard.press("Tab")
        stop = owner_page.evaluate(
            """
            () => {
              const node = document.activeElement;
              if (!node || node === document.body) return null;
              const label = node.id ? `#${node.id}` : `${node.tagName.toLowerCase()}.${node.className}`;
              const indicated = (element) => {
                if (!element || element === document.body) return false;
                const style = getComputedStyle(element);
                const outlined = style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0;
                return outlined || style.boxShadow !== 'none' || indicated(element.parentElement);
              };
              return { label, indicated: indicated(node) };
            }
            """
        )
        if stop is None:
            continue
        if stop["label"] in seen:
            break  # the tab order has wrapped
        seen.add(stop["label"])
        if not stop["indicated"]:
            offenders.add(stop["label"])

    assert seen, "tabbing never moved focus off the body"
    assert not offenders, "tab stops with no visible focus indicator: " + ", ".join(sorted(offenders))


def test_hidden_views_are_removed_from_the_tab_order(owner_page):
    """`hidden` panels must not leave focusable descendants behind."""
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    reachable_in_hidden = owner_page.evaluate(
        """
        () => {
          const offenders = [];
          for (const container of document.querySelectorAll('[hidden]')) {
            const focusable = container.querySelectorAll('button, a[href], select, input, textarea, [tabindex]:not([tabindex="-1"])');
            for (const node of focusable) {
              if (node.offsetParent !== null) {
                offenders.push(node.id || node.tagName.toLowerCase());
              }
            }
          }
          return offenders;
        }
        """
    )
    assert not reachable_in_hidden, (
        "focusable controls inside [hidden] containers: " + ", ".join(sorted(set(reachable_in_hidden)))
    )
