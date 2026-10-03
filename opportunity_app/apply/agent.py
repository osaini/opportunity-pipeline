"""The browser driver of Apply for me: Playwright, a Greenhouse adapter, and one rule for what may leave the page.

``ApplyAgent`` runs in two modes today, ``lookup`` (type what the student typed into one typeahead and read the options
it offers) and ``rehearse`` (fill the whole form in a visible window, read every field back, check the form the way a
submit would, take a picture with the sensitive fields covered, and stop). ``submit`` and ``handoff`` exist as names and
return ``failed`` without opening a browser. **Nothing here sends an application**: before the first input only GET,
HEAD and OPTIONS leave the page, after it only the typed field's own lookup and Greenhouse's static assets do, and
``checks.route_decision`` is the only thing that says so. The submit button is never pressed in these modes.

Every change to the page goes through five helpers (``_type``, ``_tick``, ``_choose``, ``_attach``, ``_click``), and a
static test (12.7) scans every Apply for me module for any other way to change a page. The adapter's react-select
functions take the agent as ``ops`` and act only through those helpers.

The plan arrives as a pickled object, built by the parent process (``policy.build_plan``) from the schema alone, and the
agent asks the parent for a new one once it has read the page (``replan``): a plan holds a closure over the database, so
it cannot be built in the browser's process. Plans and schema fields are read by attribute. Playwright is imported inside
``_start`` only, so the web app and the test suites load this module without it.

Nothing a run returns holds a field's value: reasons are fixed sentences (the question's own label at most), problems come
from ``checks``, and an exception's message is never kept, since it may quote what was typed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from .. import ROOT
from ..integrations.web_fetch import Resolver, close_browser, resolve_host
from ..outreach.forms import CAPTCHA_WIDGETS, formatting_problem
from ..outreach.render import request_allowed
from .agent_types import (
    BUILT_MODES,
    MAX_LOOKUP_OPTIONS,
    PROGRESS_STEPS,
    STOPPED,
    ApplyTimeouts,
    FilePayload,
    LookupRequest,
    RunResult,
    problem_dict,
)
from .checks import (
    CAPTCHA_ENDPOINTS,
    GREENHOUSE_LOOKUP_ENDPOINTS,
    STATIC_ASSET_HOSTS,
    TYPED_LOOKUP_KINDS,
    PHASE_AFTER_INPUT,
    PHASE_BEFORE_INPUT,
    PHASE_FILL,
    REQUIRED_CHECK_SCRIPT,
    Abort,
    Endpoint,
    Problem,
    RouteRequest,
    RouteState,
    check_required,
    leaked_field,
    question_key,
    route_decision,
    safe_host,
)
from .greenhouse import BOARD_HOSTS
from .runs import INSTALL_PLAYWRIGHT, PlaywrightProbe

# --- What the agent says (WP2 and WP3 never parse these) ------------------------------------------------------------

INSTALL = "Could not start the browser. " + INSTALL_PLAYWRIGHT
NOT_BUILT = "This kind of run is not built yet"
NOT_BOARD = "The app only opens Greenhouse's own job boards"
HTTP_STATUS = "Greenhouse answered HTTP {status}"
OFFSITE = "This posting sends applicants to {host}"
POPUP = "The page tried to open another site, so the app stopped"
LEGACY = "This is Greenhouse's older form, which the app does not fill yet"
CLOSED = "The posting is no longer accepting applications"
UNKNOWN_PAGE = "The page did not look like a Greenhouse application form"
NO_LOADER = "The app couldn't find where this form sends applications, so it could not help you submit it"
S3_NOTE = "This board uploads your résumé as soon as it is attached, so the app can't attach it without sending it"
CAPTCHA_NOTE = "The form shows a CAPTCHA checkbox"
FIELD_TOOK = 'The field "{question}" did not take the answer'
KEPT_CHANGING = "The form kept changing as it was filled in"
FILE_CHANGED = "The résumé file changed since it was confirmed. Upload it again"
FILE_TYPE = 'The form does not accept this kind of file for "{question}"'
NO_FILE = 'The app could not read the file for "{question}"'
NO_COVER_LETTER = 'The app does not attach cover letters yet; attach the one for "{question}" yourself when you submit'
NO_CONTROL = 'The form has no field for "{question}"'
NO_OPTIONS = "No options came back for what you typed"
NO_ENDPOINT = "The app has not confirmed Greenhouse's lookup service for this list yet, so it did not ask it"
PLAN_FAILED = "The app could not plan this form"
# Not in the shared list: what the agent says when a whole step, not one field, went wrong.
OPEN_FAILED = "The app could not open the Greenhouse form"
READ_FAILED = "The app could not read the form"
CHECK_FAILED = "The app could not check the filled form"
WINDOW_CLOSED = "The browser window was closed before the run finished"
DIFFERENT_POSTING = "Greenhouse opened a different posting from the one the app was asked to open"
DEFERRED_MISSING = 'The form does not offer the answer the app would give for "{question}"'
DEFERRED_UNREADABLE = 'The app could not read the options of "{question}", so it could not check the answer it would give'

# --- The pinned rules ----------------------------------------------------------------------------------------------

ENGINE_FILES = ("adapters.js", "field-engine.js", "apply-engine.js")


def _read_engine() -> str:
    try:
        folder = ROOT / "apps" / "extension"
        return "\n;\n".join((folder / name).read_text(encoding="utf-8") for name in ENGINE_FILES)
    except OSError:
        return ""   # run() then says it could not read the form


# The extension's shared field engine, read once. Injected with evaluate, which a page's Content Security Policy does not govern.
ENGINE_SOURCE: str = _read_engine()

# Controls that are never pressed, even by accident: the normalized accessible text of an element is searched for each.
DENYLIST: tuple[str, ...] = (
    "autofill my application", "apply with seek", "apply with linkedin", "locate me", "dropbox", "google drive", "enter manually",
)
CLICK_PURPOSES = ("select_open", "select_option", "select_close", "submit", "captcha_checkbox")
LEGACY_ENABLED = False
SCREENSHOT_MASK_COLOR = "#000000"
JOIN_KINDS = frozenset({"listing_mismatch", "wording_mismatch", "hidden_control", "unlisted_required"})
ACTION_TIMEOUT_MS = 10_000
MAX_REFUSED = 500

# Things in Chromium that send bytes without any request the route handler sees, so a script on the board could carry a
# filled-in answer out through them: WebRTC (STUN and TURN traffic, and a DNS lookup for the server's name), fetchLater (a
# request queued now and sent when the page goes away), FedCM (the browser itself fetches a well-known file and a config from a
# host the script names), WebTransport (QUIC), WebSocketStream, and any dedicated or shared worker (Playwright's WebSocket guard
# patches the page's own WebSocket only, and an init script does not run inside a worker, so a worker's WebSocket and
# WebTransport are out of reach). Speculation rules and a prerender link make the browser itself fetch (or load) a page, a
# prefetch to any site a script names; the Protected Audience calls (joinAdInterestGroup and the rest) make it look up and call
# the owner's host; Shared Storage fetches a module. Nothing on a Greenhouse form needs one of them, so each is removed in every
# frame before the page's own scripts run, and some are also switched off at launch (LAUNCH_ARGS), which covers a realm this script
# missed. It hides nothing about the browser: a page can tell they are not there.
NO_SIDE_CHANNELS = """(() => {
  const names = [
    'RTCPeerConnection', 'webkitRTCPeerConnection', 'RTCDataChannel', 'RTCSessionDescription', 'RTCIceCandidate',
    'fetchLater', 'FetchLaterResult', 'IdentityCredential', 'IdentityProvider', 'IdentityCredentialError',
    'WebTransport', 'WebTransportBidirectionalStream', 'WebTransportDatagramDuplexStream', 'WebTransportError', 'WebSocketStream',
    'Worker', 'SharedWorker', 'sharedStorage', 'SharedStorage', 'SharedStorageWorklet',
  ];
  for (const name of names) {
    try { delete window[name]; } catch (error) { /* already gone */ }
    try { Object.defineProperty(window, name, {value: undefined, configurable: false, writable: false}); } catch (error) { /* kept as it is */ }
  }
  const navigatorCalls = [
    'joinAdInterestGroup', 'leaveAdInterestGroup', 'clearOriginJoinedAdInterestGroups', 'updateAdInterestGroups', 'runAdAuction',
    'createAuctionNonce', 'getInterestGroupAdAuctionData', 'canLoadAdAuctionFencedFrame',
  ];
  for (const name of navigatorCalls) {
    try { delete Navigator.prototype[name]; } catch (error) { /* already gone */ }
    try { Object.defineProperty(Navigator.prototype, name, {value: undefined, configurable: false, writable: false}); } catch (error) { /* kept as it is */ }
  }
  try {
    const get = CredentialsContainer.prototype.get;
    CredentialsContainer.prototype.get = function (options) {
      if (options && options.identity) return Promise.reject(new DOMException('Not supported', 'NotSupportedError'));
      return get.apply(this, arguments);
    };
  } catch (error) { /* no credentials container */ }
  // Speculation rules (a script of that type) and a prerender link are acted on by the browser with no request the route handler
  // sees. A rule set is read when its element is inserted and the candidates are worked out a microtask later, and this observer's
  // microtask is queued first, so the element is gone before the browser has anything to fetch. Nothing the form needs is one.
  const SPECULATION = 'script[type="speculationrules" i], link[rel~="prerender" i]';
  const sweep = (root) => { try { root.querySelectorAll(SPECULATION).forEach((node) => node.remove()); } catch (error) { /* gone */ } };
  const watch = (root) => {
    try {
      new MutationObserver(() => sweep(root)).observe(root, {childList: true, subtree: true, attributes: true, characterData: true});
      sweep(root);
    } catch (error) { /* not observable */ }
  };
  watch(document);
  try {
    const attach = Element.prototype.attachShadow;
    Element.prototype.attachShadow = function (init) { const root = attach.call(this, init); watch(root); return root; };
  } catch (error) { /* no shadow roots */ }
})();"""
# Chromium switches that turn some of those features off, and one that closes the rest at the network layer. None changes how the browser
# presents itself to a page (user agent, language, screen); they only remove features this app has no use for. WebTransport has no switch
# (measured on Playwright 1.62's Chromium: neither a feature flag nor --disable-quic stops its packets), so it, and every worker that could
# reach one, is closed by the init script alone, in every realm a page can make (tests/test_apply_agent_browser.py names each one).
#
# The resolver rule is the catch-all: the browser can look up only the hosts a Greenhouse form and its fonts, lookups, static files and
# CAPTCHA use (``RESOLVABLE_HOSTS``), and any other name, an IP address included, fails inside Chromium with no query leaving the machine.
# A hint (dns-prefetch, preconnect), a prefetch, an interest-group owner or a script's own request to a host it names then goes nowhere,
# whatever channel it takes, and a value put in a host name is never looked up. The third-party widgets a board may load (Google Drive,
# Dropbox, a recruiting-analytics script) do not load either: the app presses none of them.
RESOLVABLE_HOSTS: tuple[str, ...] = tuple(sorted({
    *BOARD_HOSTS, *(endpoint.host for endpoint in GREENHOUSE_LOOKUP_ENDPOINTS), *STATIC_ASSET_HOSTS,
    "s?-recruiting.cdn.greenhouse.io", "s??-recruiting.cdn.greenhouse.io", "s???-recruiting.cdn.greenhouse.io",
    *(endpoint.host for endpoint in CAPTCHA_ENDPOINTS), "fonts.googleapis.com", "fonts.gstatic.com", "my.greenhouse.io", "c.spl.greenhouse.io",
}))


def resolver_rule(extra_hosts: Sequence[str] = ()) -> str:
    """The ``--host-resolver-rules`` switch: every name fails to resolve but ``RESOLVABLE_HOSTS`` (and ``extra_hosts``, for a test's loopback page)."""
    return "--host-resolver-rules=MAP * ~NOTFOUND , " + " , ".join(f"EXCLUDE {host}" for host in (*RESOLVABLE_HOSTS, *extra_hosts))


LAUNCH_ARGS = ("--disable-blink-features=FetchLaterAPI,WebSocketStream", "--disable-features=FedCm", resolver_rule())

_SCAN = "(a) => OpportunityApplyEngine.scan(a.profile, a.answers, {tag: true})"
_CONTAINS = "(c, e) => c.contains(e)"
# A typed value is the student's own text, not the control's name: only a button's value is read.
_ACCESSIBLE_TEXT = """(e) => [e.innerText || '', e.getAttribute('aria-label') || '',
  (e.tagName === 'BUTTON' || ['button', 'submit', 'reset', 'image'].includes((e.type || '').toLowerCase())) ? (e.value || '') : ''].join(' ')"""
_KIND = """(els) => {
  const e = els[0];
  if (!e) return 'missing';
  const tag = e.tagName.toLowerCase();
  const type = (e.type || '').toLowerCase();
  if (e.getAttribute('role') === 'combobox' && e.closest('.select__control')) return 'react_select';
  if (tag === 'select') return 'select';
  if (tag === 'textarea') return 'textarea';
  if (type === 'radio') return 'radio';
  if (type === 'checkbox') return 'checkbox';
  if (type === 'file') return 'file';
  return 'text';
}"""
_CHOICES = """(els) => els.map((e) => {
  const wrap = e.closest('label');
  const named = (!wrap && e.id) ? document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(e.id) : e.id) + '"]') : null;
  const source = wrap || named;
  return {label: (source ? source.innerText : (e.value || '')).replace(/\\s+/g, ' ').trim(), checked: !!e.checked};
})"""
_NATIVE_OPTIONS = "(e) => Array.from(e.options).map((o) => o.textContent.replace(/\\s+/g, ' ').trim()).filter((t) => t)"
_NATIVE_VALUE = "(e) => (e.selectedOptions && e.selectedOptions[0] && e.value !== '') ? e.selectedOptions[0].textContent.replace(/\\s+/g, ' ').trim() : ''"
_FILE_STATE = "(e) => ({count: e.files ? e.files.length : 0, name: e.files && e.files[0] ? e.files[0].name : '', size: e.files && e.files[0] ? e.files[0].size : -1, accept: e.getAttribute('accept') || ''})"
_MENU_OPEN = "(e) => e.getAttribute('aria-expanded') === 'true'"
_LOADER_KEY = r'"{name}"\s*:\s*("(?:[^"\\]|\\.)*")'


class ClickRefused(RuntimeError):
    """``_click`` was asked for something the allowlist does not cover. The agent turns it into a reason."""


class _Stop(Exception):
    """The run ends here, with this outcome and this sentence. Never carries a value."""

    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome
        self.reason = reason


def _normalize(text: Any) -> str:
    return " ".join(str(text if text is not None else "").split()).casefold()


def _attr(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _css(text: str) -> str:
    """A string for use inside a double-quoted CSS attribute selector."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def board_token(page_url: str) -> str:
    """The board token of a Greenhouse posting URL (the first path segment, or the embed's ``for``), or ""."""
    try:
        parts = urlsplit(page_url)
    except ValueError:
        return ""
    match = re.match(r"^/([A-Za-z0-9][A-Za-z0-9_-]{0,79})/jobs/\d+/?$", parts.path)
    if match:
        return match.group(1)
    token = (parse_qs(parts.query).get("for") or [""])[0]
    return token if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", token) and parts.path.rstrip("/") == "/embed/job_app" else ""


def posting_ids(url: str) -> tuple[str, str]:
    """(board token, job id), lower-cased, of a Greenhouse posting URL (the board's own path, or the embed's ``for`` and ``token``). ("", "") for any other."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "", ""
    match = re.match(r"^/([A-Za-z0-9][A-Za-z0-9_-]{0,79})/jobs/(\d+)/?$", parts.path)
    if match:
        return match.group(1).lower(), match.group(2)
    query = parse_qs(parts.query)
    token, job = (query.get("for") or [""])[0], (query.get("token") or [""])[0]
    if parts.path.rstrip("/") == "/embed/job_app" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", token) and job.isdigit():
        return token.lower(), job
    return "", ""


def bind_endpoints(endpoints: Sequence[Endpoint], token: str) -> tuple[Endpoint, ...]:
    """The lookup endpoints with the posting's own board token in place of ``{token}``.

    Greenhouse's school, degree and discipline lists sit under ``/v1/boards/{token}/education/``, so a path prefix
    that names the board cannot be written down once. An endpoint that needs the token when there is none is dropped.
    """
    bound = []
    for endpoint in endpoints:
        if "{token}" in endpoint.path_prefix:
            if not token:
                continue
            endpoint = Endpoint(endpoint.host, endpoint.path_prefix.replace("{token}", token), endpoint.kind)
        bound.append(endpoint)
    return tuple(bound)


# --- The Greenhouse adapter -------------------------------------------------------------------------------------------

class GreenhouseAdapter:
    """Deterministic selectors and reads for Greenhouse's job-boards form. Page text never chooses an action.

    Everything that changes the page goes through the agent (``ops``): this class reads, and asks ``ops`` to act.
    """

    # --- the page ---------------------------------------------------------------------------------------------

    def form_frame(self, page: Any) -> Any:
        """The main frame, or the frame of ``#grnhse_iframe`` when the form is there."""
        if page.locator("form#application-form, form#application_form").count():
            return page.main_frame
        iframe = page.locator("iframe#grnhse_iframe")
        if iframe.count():
            handle = iframe.first.element_handle()
            frame = handle.content_frame() if handle else None
            if frame is not None and frame.locator("form#application-form, form#application_form").count():
                return frame
        return page.main_frame

    def detect_page(self, page: Any) -> str:
        """application_form_new, application_form_legacy, confirmation, closed, offsite or unknown."""
        parts = urlsplit(page.url)
        host = (parts.hostname or "").lower().rstrip(".")
        if host not in BOARD_HOSTS:
            return "offsite"
        errored = parse_qs(parts.query).get("error") == ["true"]
        frame = self.form_frame(page)
        if frame.locator("form#application-form").count():
            return "closed" if errored else "application_form_new"
        if frame.locator("form#application_form").count():
            return "application_form_legacy"
        path = parts.path.rstrip("/")
        if path.endswith("/confirmation"):
            return "confirmation"
        if errored or len([segment for segment in path.split("/") if segment]) <= 1:
            return "closed"
        # The notice is read only because the form is absent, and it never decides anything by itself.
        try:
            text = _normalize(page.locator("body").inner_text(timeout=ACTION_TIMEOUT_MS))
        except Exception:  # noqa: BLE001 - a page with no body is an unknown page
            text = ""
        if "no longer open" in text or "no longer accepting" in text:
            return "closed"
        return "unknown"

    @staticmethod
    def loader_paths(html: str) -> tuple[str, str]:
        """("submitPath", "confirmationPath") from the served HTML, as paths; "" for one that is absent or on another host."""
        found = []
        for name in ("submitPath", "confirmationPath"):
            match = re.search(_LOADER_KEY.format(name=name), html or "")
            path = ""
            if match:
                try:
                    value = str(json.loads(match.group(1)))
                    parts = urlsplit(value)
                    if not parts.hostname or (parts.hostname.lower() in BOARD_HOSTS):
                        path = parts.path
                except ValueError:
                    path = ""
            found.append(path)
        return found[0], found[1]

    def uploads_on_attach(self, frame: Any) -> bool:
        """The form (or an upload group in it) says a file is uploaded the moment it is attached."""
        return bool(frame.locator(
            'form#application-form[data-allow-s3="true"], form#application-form [data-allow-s3="true"]').count())

    def captcha_widget(self, frame: Any) -> str:
        """The name of a visible checkbox CAPTCHA on the form, or ""."""
        for name, frame_selector, _checkbox, _token in CAPTCHA_WIDGETS:
            widget = frame.locator(frame_selector)
            try:
                if widget.count() and widget.first.is_visible():
                    return name
            except Exception:  # noqa: BLE001 - a widget that went away is not there
                continue
        return ""

    def submit_control(self, frame: Any) -> Any | None:
        """The one enabled "Submit application" button (new board) or ``#submit_app`` (legacy). Defined for M6; never pressed in M5a."""
        found = []
        buttons = frame.locator("form#application-form button[type='submit']")
        for index in range(buttons.count()):
            button = buttons.nth(index)
            disabled = button.is_disabled() or button.get_attribute("aria-disabled") == "true"
            if _normalize(button.inner_text()) == "submit application" and not disabled:
                found.append(button)
        legacy = frame.locator("#application_form #submit_app")
        if not found and legacy.count() == 1:
            found.append(legacy.first)
        return found[0] if len(found) == 1 else None

    # --- controls ---------------------------------------------------------------------------------------------

    def control(self, frame: Any, key: str) -> Any:
        """The control(s) whose id (else name) is ``key``. A radio group is several inputs."""
        by_id = frame.locator(f'[id="{_css(key)}"]')
        if by_id.count():
            return by_id
        return frame.locator(f'[name="{_css(key)}"]:not([type="hidden"]):not([aria-hidden="true"])')

    def control_kind(self, frame: Any, key: str) -> str:
        """missing, react_select, select, textarea, radio, checkbox, file or text."""
        return str(self.control(frame, key).evaluate_all(_KIND))

    def is_react_select(self, frame: Any, key: str) -> bool:
        return self.control_kind(frame, key) == "react_select"

    def field_container(self, frame: Any, key: str) -> Any:
        """The field's whole container: its label, control, value display and error text."""
        control = self.control(frame, key).first
        for step in (
            "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' field ') or self::fieldset][1]",
            "xpath=ancestor::*[contains(@class, 'select__container') or contains(concat(' ', normalize-space(@class), ' '), ' rs ')][1]",
        ):
            found = control.locator(step)
            if found.count():
                return found.first
        return control.locator("xpath=..")

    def choices(self, frame: Any, key: str) -> list[dict[str, Any]]:
        """The radios or checkboxes named ``key`` as [{"label", "checked"}]."""
        return list(self.control(frame, key).evaluate_all(_CHOICES))

    def react_values(self, container: Any) -> list[str]:
        """What a react-select shows as chosen: every multi value label, else the single value."""
        many = [_normalize(text) for text in container.locator('[class*="multi-value__label"]').all_inner_texts()]
        if many:
            return many
        return [_normalize(text) for text in container.locator('[class*="single-value"]').all_inner_texts()]

    # --- react-select, through the agent's helpers ------------------------------------------------------------

    def fill_react_select(self, ops: "ApplyAgent", frame: Any, key: str, option_text: str, *, search: str | None = None) -> str:
        """Choose the one option whose text is ``option_text``. "" on success, else the sentence of what went wrong. Never the first option."""
        control = self.control(frame, key).first
        container = self.field_container(frame, key)
        problem = FIELD_TOOK.format(question=ops._question(key))
        try:
            ops._click(control, "select_open", key)
            ops._type(control, search or option_text, key, search=True)
            ops._settle_choice(frame.page)
            ops._wait_for_options(container)
            options = container.locator('[role="option"]')
            wanted = _normalize(option_text)
            matches = [index for index, text in enumerate(options.all_inner_texts()) if _normalize(text) == wanted]
            if len(matches) != 1:
                return problem
            ops._click(options.nth(matches[0]), "select_option", key)
            ops._settle_choice(frame.page)
            shown = self.react_values(container)
            return "" if wanted in shown and (len(shown) == 1 or self._is_multi(container)) else problem
        finally:
            ops._release()

    @staticmethod
    def _is_multi(container: Any) -> bool:
        return bool(container.locator('[class*="multi-value__label"]').count())

    def read_options(self, ops: "ApplyAgent", frame: Any, key: str, *, typed: str | None = None, limit: int = MAX_LOOKUP_OPTIONS) -> list[str]:
        """The option labels a select or typeahead offers, in order and without repeats. Chooses nothing."""
        found = self.control(frame, key)
        if not found.count():
            return []
        kind = self.control_kind(frame, key)
        if kind == "select":
            texts = list(found.first.evaluate(_NATIVE_OPTIONS))
        elif kind == "react_select":
            control = found.first
            container = self.field_container(frame, key)
            try:
                if typed is not None:
                    ops._type(control, typed, key, search=True)
                else:
                    ops._click(control, "select_open", key)
                ops._settle_choice(frame.page)
                ops._wait_for_options(container)
                texts = [" ".join(text.split()) for text in container.locator('[role="option"]').all_inner_texts()]
                if control.evaluate(_MENU_OPEN):
                    # react-select ignores a second press on its own input while the menu is open; it closes on blur.
                    ops._click(control, "select_close", key)
                    ops._settle_choice(frame.page)
                    if typed is None and control.evaluate(_MENU_OPEN):
                        raise _Stop("needs_you", FIELD_TOOK.format(question=ops._question(key)))
            finally:
                ops._release()
        elif kind in ("radio", "checkbox"):
            texts = [item["label"] for item in self.choices(frame, key)]
        else:
            texts = []
        options: list[str] = []
        for text in texts:
            if text and text not in options:
                options.append(text)
        return options[:limit]

    def fill_location(self, ops: "ApplyAgent", frame: Any, key: str, label: str) -> str:
        """Type the city part of the stored exact label, then choose the option that is the whole label (never the top result)."""
        city = label.split(",")[0].strip() or label
        return self.fill_react_select(ops, frame, key, label, search=city)


# --- The agent --------------------------------------------------------------------------------------------------------

def _launch_browser(playwright: Any, **options: Any) -> Any:
    return playwright.chromium.launch(**options)


def _new_context(browser: Any, **options: Any) -> Any:
    return browser.new_context(**options)


class ApplyAgent:
    """Chromium through Playwright, in a visible window, behind ``checks.route_decision``. Use as a context manager on one thread."""

    def __init__(
        self, *, mode: str, adapter: GreenhouseAdapter, run_id: str = "", screenshot_dir: Path | None = None,
        resolve: Resolver = resolve_host, timeouts: ApplyTimeouts = ApplyTimeouts(),
        route_hook: Callable[[Any], None] | None = None,            # tests only: serves pages from a fixture
        student_hook: Callable[[Any, str], None] | None = None,     # tests only: plays the student (M5b wait loops)
        headless: bool = False,                                     # tests only; production never sets it
        lookup_endpoints: Sequence[Endpoint] | None = None,         # tests only; default checks.GREENHOUSE_LOOKUP_ENDPOINTS
        on_progress: Callable[[str, str], None] | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self.mode = mode
        self.adapter = adapter
        self.run_id = run_id
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self.timeouts = timeouts
        self.headless = headless
        self._resolve = resolve
        self._allowed: dict[str, bool] = {}
        self._route_hook = route_hook
        self._student_hook = student_hook
        self._lookup_endpoints_override = None if lookup_endpoints is None else tuple(lookup_endpoints)
        self._on_progress = on_progress
        self._heartbeat = heartbeat
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._frame: Any = None
        self._phase = PHASE_BEFORE_INPUT if mode in ("lookup", "rehearse") else PHASE_FILL
        self._endpoints: tuple[Endpoint, ...] = tuple(self._lookup_endpoints_override or ())
        self._state = RouteState(lookup_endpoints=self._endpoints)
        self._inflight: set[Any] = set()
        # What the run found.
        self._refused: list[dict[str, Any]] = []
        self._refused_total = 0
        # What ends a run by rule 1 (4.3) is kept apart from the list above, which stops growing at MAX_REFUSED: a script that
        # fills it with refused telemetry must not be able to hide the page's own navigation away from the board.
        self._offsite_first: str | None = None    # the host of the first refused navigation of the run's own page
        self._popup_seen = False                  # a refused navigation of any other window
        self._lookups_sent: dict[str, str] = {}   # the field's key -> the kind of lookup its typeahead called
        self._lookups_typed: set[str] = set()     # the keys whose lookup request was seen to carry the field's own typed text
        self._typed_texts: dict[str, set[str]] = {}   # the search text typed into each typeahead, guarded like any planned value
        self._submit_path_hit = False
        self._screenshots: list[dict[str, Any]] = []
        self._reasons: list[str] = []
        self._join_problems: list[dict[str, Any]] = []
        self._check_problems: list[dict[str, Any]] = []
        self._options: dict[str, list[str]] = {}
        # The plan keys this run filled and then read back (a file: attached and checked). Only these are said to be filled.
        self._done: set[str] = set()
        # The deferred keys this run compared with the form. Only these are said to have been checked.
        self._checked: set[str] = set()
        # The deferred keys whose comparison found the form does not offer the answer, or could not be read (a subset of the above).
        self._deferred_failed: set[str] = set()
        self._evidence_bits: dict[str, Any] = {
            "page": "", "loader": {"submit_path": False, "confirmation_path": False}, "uploads_on_attach": False, "captcha_widget": False,
        }
        # The run's inputs.
        self._plan: Any = None
        self._schema: list[Any] = []
        self._files: dict[str, FilePayload] = {}
        self._lookup: LookupRequest | None = None
        self._cancelled: Callable[[], bool] = lambda: False
        self._hand_over: Callable[[], bool] | None = None
        self._loaded = False
        self._step = "open"
        self._doing = ""

    def __enter__(self) -> "ApplyAgent":
        return self

    def __exit__(self, *_exc: Any) -> None:
        close_browser(self)

    # --- launch ------------------------------------------------------------------------------------------------

    @staticmethod
    def launch_options(headless: bool) -> dict[str, Any]:
        """A visible window, and the switches that remove the features a page could send a value out through (LAUNCH_ARGS).
        No channel, no flag that changes how the browser presents itself, nothing that disguises it."""
        return {"headless": headless, "args": list(LAUNCH_ARGS)}

    @staticmethod
    def context_options() -> dict[str, Any]:
        """No service workers, no downloads, no permissions (so "Locate me" is never granted), and the window's natural size."""
        return {"service_workers": "block", "accept_downloads": False, "permissions": [], "no_viewport": True}

    def _start(self) -> None:
        if self._context is not None:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = _launch_browser(self._playwright, **self.launch_options(self.headless))
        self._context = _new_context(self._browser, **self.context_options())
        self._context.on("request", lambda request: self._inflight.add(id(request)))
        self._context.on("requestfinished", lambda request: self._inflight.discard(id(request)))
        self._context.on("requestfailed", lambda request: self._inflight.discard(id(request)))
        self._context.add_init_script(NO_SIDE_CHANNELS)
        self._context.route("**/*", self._route)
        self._context.route_web_socket("**/*", self._refuse_socket)
        self._page = self._context.new_page()

    # --- the request policy -----------------------------------------------------------------------------------

    def _route(self, route: Any) -> None:
        """Gather the facts about one request, ask ``route_decision`` and do what it says. Anything unexpected is refused."""
        request = route.request
        try:
            own_page = False
            try:
                navigation = bool(request.is_navigation_request() and request.frame.parent_frame is None)
                # The run's own page, as against a popup's first load or any other window: only the first leaving the board is "the posting".
                own_page = navigation and request.frame is getattr(self._page, "main_frame", None)
            except Exception:  # noqa: BLE001 - a navigation whose frame is not known yet (a popup's first load) is a main-frame one: fail closed
                navigation = True
            try:
                body = request.post_data_buffer
            except Exception:  # noqa: BLE001 - no body
                body = None
            facts = RouteRequest(
                method=request.method, url=request.url, resource_type=request.resource_type, is_navigation=navigation,
                public=True, headers=request.headers, body=body,
            )
            decision = route_decision(self.mode, self._phase, facts, self._state)
            # Resolved last, and only for a request the policy would let through: the name of a request that is refused anyway
            # (after the first input, anything but Greenhouse's own hosts) is never sent to a resolver, since a hostname can carry a value.
            if not isinstance(decision, Abort) and self._route_hook is None and not request_allowed(request.url, self._resolve, self._allowed):
                decision = Abort(
                    "non_public_address", "The address is not a public one",
                    safe_host((urlsplit(request.url).hostname or "").lower().rstrip("."), self._state.values),
                )
            if isinstance(decision, Abort):
                record = decision.record(request.method)
                if decision.rule == "offsite_navigation":
                    if own_page:
                        if self._offsite_first is None:
                            self._offsite_first = str(record.get("host") or "")
                    else:
                        record["popup"] = True
                        self._popup_seen = True
                self._refuse(record)
                self._inflight.discard(id(request))
                route.abort("blockedbyclient")
                return
            self._state.record(decision)
            if decision.rule == "lookup" and self._state.typing_key:
                key = self._state.typing_key
                self._lookups_sent[key] = self._state.typing_lookup
                # Measured, not assumed from the kind of list: did this request carry the text typed into (or planned for) the field?
                own = self._state.values.get(key)
                if own is not None and leaked_field(facts, {key: own}):
                    self._lookups_typed.add(key)
            if decision.submit_post:
                self._submit_path_hit = True
            (self._route_hook or (lambda handled: handled.continue_()))(route)
        except Exception:  # noqa: BLE001 - fail closed
            try:
                route.abort("blockedbyclient")
            except Exception:  # noqa: BLE001 - already handled
                pass

    def _refuse_socket(self, ws: Any) -> None:
        # Refused by never calling connect_to_server(); closing from inside the handler deadlocks the sync API.
        host = safe_host((urlsplit(getattr(ws, "url", "") or "").hostname or "").lower(), self._state.values)
        self._refuse({"method": "WEBSOCKET", "host": host, "rule": "websocket"})

    def _refuse(self, record: dict[str, Any]) -> None:
        self._refused_total += 1
        if len(self._refused) < MAX_REFUSED:
            self._refused.append(record)

    def _lookup_kind_for(self, key: str) -> str:
        """The lookup kind the typeahead of this field calls, only when a pinned endpoint serves it."""
        kind = ""
        if self._lookup is not None and key == self._lookup.key:
            kind = self._lookup.field
        else:
            entry = self._entry(key)
            source = _attr(entry, "source") if entry is not None else None
            if source is not None and _attr(source, "kind") == "ats_label":
                kind = str(_attr(source, "ref") or "")
        return kind if kind and any(endpoint.kind == kind for endpoint in self._endpoints) else ""

    # --- the five helpers: the only places the page is changed ------------------------------------------------

    def _cancel_requested(self) -> bool:
        try:
            return bool(self._cancelled())
        except Exception:  # noqa: BLE001 - a callback that fails means stop
            return True

    def _begin(self, key: str) -> None:
        if self._cancel_requested():
            raise _Stop("failed", STOPPED)
        if self.mode in ("lookup", "rehearse"):
            self._phase = PHASE_AFTER_INPUT
        self._state.typing_key = key
        self._state.typing_lookup = self._lookup_kind_for(key)

    def _release(self) -> None:
        """The field is done: no lookup is let through for it any more.

        A lookup request is made by the page after the helper has returned (when the page's own script has seen the input),
        so a helper that opens or types into a typeahead leaves its field named until the adapter calls this, after the options
        have been read. The helpers that cannot start a lookup release at once.
        """
        self._state.typing_key = ""
        self._state.typing_lookup = ""

    def _type(self, locator: Any, value: str, key: str, *, search: bool = False) -> None:
        """fill() (trusted input events); then change and blur, except into a search box, whose menu a blur would close."""
        if search:
            # What is typed into a search box is a value like any other: guarded from now on, except in this field's own lookup.
            self._typed_texts.setdefault(key, set()).add(value)
            self._refresh_values()
        self._begin(key)
        try:
            locator.fill(value, timeout=ACTION_TIMEOUT_MS)
            if not search:
                locator.dispatch_event("change")
                locator.blur()
        finally:
            if not search:
                self._release()
        self._wait(self.timeouts.between_fields_s)

    def _tick(self, locator: Any, key: str, checked: bool = True) -> None:
        self._begin(key)
        try:
            locator.set_checked(checked, timeout=ACTION_TIMEOUT_MS)
        finally:
            self._release()
        self._wait(self.timeouts.between_fields_s)

    def _choose(self, locator: Any, label: str, key: str) -> None:
        """A native select takes its option by label; a react-select goes through the adapter. A refusal ends the run."""
        if self.adapter.is_react_select(self._frame, key):
            entry = self._entry(key)
            source = _attr(entry, "source") if entry is not None else None
            if source is not None and _attr(source, "kind") == "ats_label" and _attr(source, "ref") == "location":
                problem = self.adapter.fill_location(self, self._frame, key, label)
            else:
                problem = self.adapter.fill_react_select(self, self._frame, key, label)
            if problem:
                raise _Stop("needs_you", problem)
            return
        self._begin(key)
        try:
            locator.select_option(label=label, timeout=ACTION_TIMEOUT_MS)
        finally:
            self._release()
        self._wait(self.timeouts.between_fields_s)

    def _attach(self, locator: Any, payload: FilePayload, key: str) -> None:
        self._begin(key)
        try:
            locator.set_input_files(payload.as_playwright(), timeout=ACTION_TIMEOUT_MS)
        finally:
            self._release()
        self._wait(self.timeouts.between_fields_s)

    def _click(self, locator: Any, purpose: str, key: str = "") -> None:
        """Press something, only if the allowlist says this exact thing may be pressed. Raises ``ClickRefused`` otherwise."""
        if purpose == "captcha_checkbox":
            # D14 A: the app never presses a CAPTCHA box. The student ticks it, in the window, before hand-over.
            raise ClickRefused("The app does not press a CAPTCHA box")
        if purpose == "submit":
            # Submitting needs a hand-over the parent committed; neither exists in lookup or rehearse mode.
            raise ClickRefused("This kind of run does not press Submit")
        if purpose not in ("select_open", "select_option", "select_close"):
            raise ClickRefused("The app does not press that")
        if not self._clickable_key(key):
            raise ClickRefused("The app does not press a field it did not plan")
        container = self.adapter.field_container(self._frame, key)
        if not container.evaluate(_CONTAINS, locator.element_handle()):
            raise ClickRefused("The app does not press outside the field it is filling")
        text = _normalize(locator.evaluate(_ACCESSIBLE_TEXT))
        if any(word in text for word in DENYLIST):
            raise ClickRefused("The app never presses that control")
        self._begin(key)
        if purpose == "select_close":
            locator.blur()   # no pointer press: a press on the input of an open react-select does not close it
            return
        locator.click(timeout=self.timeouts.navigation_s * 1000)

    def _clickable_key(self, key: str) -> bool:
        if self._lookup is not None and key == self._lookup.key:
            return True
        entry = self._entry(key)
        return entry is not None and _attr(entry, "disposition") in ("fill", "deferred")

    def _settle_choice(self, page: Any) -> None:
        """After a choice or a typed search: wait until no request is in flight, or ``choice_settle_s``, whichever comes first."""
        deadline_ms = int(self.timeouts.choice_settle_s * 1000)
        waited = 0
        page.wait_for_timeout(50)
        while self._inflight and waited < deadline_ms:
            page.wait_for_timeout(50)
            waited += 50
        page.wait_for_timeout(100)   # the page's own script turns the answer into options

    def _wait_for_options(self, container: Any) -> None:
        """Wait for the field's menu to hold an option, at most ``choice_settle_s`` (a lookup answers after a debounce)."""
        deadline_ms = int(self.timeouts.choice_settle_s * 1000)
        waited = 0
        options = container.locator('[role="option"]')
        while not options.count() and waited < deadline_ms:
            self._page.wait_for_timeout(100)
            waited += 100

    def _wait(self, seconds: float) -> None:
        if seconds > 0 and self._page is not None:
            self._page.wait_for_timeout(int(seconds * 1000))

    # --- plan access -------------------------------------------------------------------------------------------

    def _fields(self) -> list[Any]:
        fields = _attr(self._plan, "fields", None)
        return list(fields) if fields is not None else []

    def _entry(self, key: str) -> Any | None:
        for entry in self._fields():
            if _attr(entry, "key") == key:
                return entry
        return None

    def _question(self, key: str) -> str:
        if self._lookup is not None and key == self._lookup.key:
            return self._lookup.question
        entry = self._entry(key)
        return str(_attr(entry, "question") or key) if entry is not None else key

    def _fill_entries(self) -> list[Any]:
        return [entry for entry in self._fields() if _attr(entry, "disposition") == "fill" and _attr(entry, "control") != "file"]

    def _refresh_values(self) -> None:
        """The values the request guard watches for: every planned value that is, or will be, typed into the page."""
        values: dict[str, Any] = {}
        for entry in self._fields():
            value = _attr(entry, "value")
            if _attr(entry, "disposition") in ("fill", "deferred") and _attr(entry, "control") != "file" and value is not None:
                values[str(_attr(entry, "key"))] = value
        if self._lookup is not None and self._lookup.text:
            values[self._lookup.key] = self._lookup.text
        for key, texts in self._typed_texts.items():
            held = values.get(key)
            values[key] = [*(held if isinstance(held, (list, tuple)) else [] if held is None else [held]), *sorted(texts)]
        self._state.values = values

    # --- progress ----------------------------------------------------------------------------------------------

    def _progress(self, step: str, **words: Any) -> None:
        if self._on_progress is not None:
            self._on_progress(step, PROGRESS_STEPS[step].format(**words))

    def _beat(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat()

    def _between(self) -> None:
        """Between fields: a heartbeat, and the chance to stop."""
        self._beat()
        if self._cancel_requested():
            raise _Stop("failed", STOPPED)
        self._check_left_site()

    def _check_left_site(self) -> None:
        """The page's own main frame was sent to another host after it loaded (a refused navigation): the form is gone, so the run ends here."""
        host = self._offsite_host()
        if host:
            raise _Stop("needs_you", OFFSITE.format(host=host))

    # --- run ---------------------------------------------------------------------------------------------------

    def run(
        self, plan: Any, *, page_url: str, schema: list[Any], files: dict[str, FilePayload], lookup: LookupRequest | None = None,
        replan: Callable[[list[dict[str, Any]], bool], Any] | None = None, hand_over: Callable[[], bool] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> RunResult:
        """One run. Never raises: the outcome always says whether anything could have left the page (it never could)."""
        self._plan, self._schema, self._files, self._lookup = plan, list(schema or []), dict(files or {}), lookup
        self._hand_over = hand_over
        self._cancelled = cancelled or (lambda: False)
        self._step = "open"
        try:
            return self._run(page_url, replan)
        except _Stop as stop:
            reason = stop.reason
            if stop.outcome == "needs_you" and self._loaded:
                # A form whose page was sent elsewhere fails every later step in its own way; the cause is the one to give.
                reason = OFFSITE.format(host=self._offsite_host()) if self._offsite_host() else reason
                self._screenshot("needs-you")
            return self._finish(stop.outcome, [reason])
        except Exception:  # noqa: BLE001 - never a message: it may quote a value
            if self._page is not None and self._closed():
                return self._finish("failed", [WINDOW_CLOSED])
            if self._loaded and self._offsite_host():
                self._screenshot("needs-you")
                return self._finish("needs_you", [OFFSITE.format(host=self._offsite_host())])
            if self._step == "fill":
                # A Playwright error on a field (a covered element, a read-only box, a missing option): the form did not take it.
                self._screenshot("needs-you")
                return self._finish("needs_you", [FIELD_TOOK.format(question=self._doing or "a field")])
            return self._finish("failed", [self._step_sentence()])

    def _closed(self) -> bool:
        try:
            return bool(self._page.is_closed())
        except Exception:  # noqa: BLE001 - a page that cannot answer is gone
            return True

    def _step_sentence(self) -> str:
        return {"open": OPEN_FAILED, "read": READ_FAILED, "check": CHECK_FAILED}.get(self._step, PLAN_FAILED)

    def _run(self, page_url: str, replan: Callable[[list[dict[str, Any]], bool], Any] | None) -> RunResult:
        if self.mode not in BUILT_MODES:
            return self._finish("failed", [NOT_BUILT])
        if (urlsplit(page_url).hostname or "").lower().rstrip(".") not in BOARD_HOSTS:
            return self._finish("failed", [NOT_BOARD])
        if self.mode == "lookup" and self._lookup is None:
            return self._finish("failed", [PLAN_FAILED])
        self._endpoints = (
            self._lookup_endpoints_override if self._lookup_endpoints_override is not None
            else bind_endpoints(GREENHOUSE_LOOKUP_ENDPOINTS, board_token(page_url))
        )
        self._state.lookup_endpoints = self._endpoints
        self._refresh_values()
        self._check_stopped()

        # 1 and 2: the browser and the page.
        self._progress("open")
        try:
            self._start()
        except Exception:  # noqa: BLE001 - no package, no browser build, no display
            raise _Stop("failed", INSTALL)
        html = self._open(page_url)
        self._loaded = True
        self._check_stopped()

        # 3: what kind of page it is.
        frame = self._frame = self.adapter.form_frame(self._page)
        kind = self._offsite_kind() or self.adapter.detect_page(self._page)
        self._evidence_bits["page"] = kind
        if kind == "application_form_legacy" and not LEGACY_ENABLED:
            raise _Stop("needs_you", LEGACY)
        if kind == "closed":
            raise _Stop("failed", CLOSED)
        if kind == "offsite":
            raise _Stop("needs_you", OFFSITE.format(host=self._offsite_host() or (urlsplit(self._page.url).hostname or "another site")))
        if self._popup_refused():
            raise _Stop("needs_you", POPUP)
        asked, landed = posting_ids(page_url), posting_ids(self._page.url)
        if asked != ("", "") and landed != ("", "") and landed != asked:
            # A board that redirects a posting to another one: what is filled and checked here would be that other posting.
            raise _Stop("needs_you", DIFFERENT_POSTING)
        if kind != "application_form_new":
            raise _Stop("needs_you", UNKNOWN_PAGE)
        submit_path, confirmation_path = self.adapter.loader_paths(html)
        self._state.submit_path = submit_path
        self._evidence_bits["loader"] = {"submit_path": bool(submit_path), "confirmation_path": bool(confirmation_path)}
        if not (submit_path and confirmation_path):
            self._reasons.append(NO_LOADER)
        uploads = self.adapter.uploads_on_attach(frame)
        self._evidence_bits["uploads_on_attach"] = uploads
        if uploads and self.mode == "rehearse":
            self._reasons.append(S3_NOTE)

        if self.mode == "lookup":
            return self._lookup_options(frame)
        return self._rehearse(frame, replan, uploads)

    def _check_stopped(self) -> None:
        if self._cancel_requested():
            raise _Stop("failed", STOPPED)

    def _open(self, page_url: str) -> str:
        """goto, the status, the settle, and the served HTML (for the loader's paths)."""
        page = self._page
        try:
            response = page.goto(page_url, wait_until="domcontentloaded", timeout=int(self.timeouts.navigation_s * 1000))
        except Exception:  # noqa: BLE001 - an aborted navigation, a timeout, a dropped connection
            host = self._offsite_host()
            if host:
                raise _Stop("needs_you", OFFSITE.format(host=host))
            raise _Stop("failed", OPEN_FAILED)
        if response is not None and response.status >= 400:
            raise _Stop("failed", HTTP_STATUS.format(status=response.status))
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:  # noqa: BLE001 - a busy page is read as it stands
            pass
        try:
            return str(response.text()) if response is not None else ""
        except Exception:  # noqa: BLE001 - a body the browser no longer holds
            return ""

    def _offsite_host(self) -> str:
        """The host the run's own page was sent to, from the first refused navigation of that page (a popup's is not it). Not read from
        the refused list, which is capped."""
        return self._offsite_first or ""

    def _popup_refused(self) -> bool:
        return self._popup_seen

    def _offsite_kind(self) -> str:
        return "offsite" if self._offsite_host() else ""

    # --- lookup ------------------------------------------------------------------------------------------------

    def _lookup_options(self, frame: Any) -> RunResult:
        lookup = self._lookup
        self._step = "read"
        self._progress("lookup", question=lookup.question)
        self._beat()
        self._wait(self.timeouts.settle_s)   # as before the first input of a rehearsal: the page's late scripts get time to arrive
        self._doing = lookup.question
        if not self.adapter.control(frame, lookup.key).count():
            raise _Stop("needs_you", NO_CONTROL.format(question=lookup.question))
        options = self.adapter.read_options(self, frame, lookup.key, typed=lookup.text)
        self._options = {lookup.field: options}
        if not options:
            self._reasons.append(NO_OPTIONS)
            if not any(endpoint.kind == lookup.field for endpoint in self._endpoints):
                self._reasons.append(NO_ENDPOINT)
        return self._finish("looked_up")

    # --- rehearse ----------------------------------------------------------------------------------------------

    def _rehearse(self, frame: Any, replan: Callable[[list[dict[str, Any]], bool], Any] | None, uploads: bool) -> RunResult:
        # 4: inject, scan (structure only: no student value ever enters the page's JavaScript), snapshot.
        self._step = "read"
        self._progress("read")
        if not ENGINE_SOURCE:
            raise _Stop("failed", READ_FAILED)
        frame.evaluate(ENGINE_SOURCE)
        scan = self._scan(frame)
        initial = self._snapshot(frame)
        self._check_stopped()

        # 5: the plan from what the page actually holds.
        self._step = "plan"
        self._plan_from(scan, replan, uploads)
        keys = self._scan_keys(scan)

        # 6: fill. Choices first, because a choice can redraw the form.
        self._beat()
        self._wait(self.timeouts.settle_s)
        self._step = "fill"
        entries = self._fill_entries()
        self._progress("fill", n=len(entries))
        done: set[str] = set()
        self._fill_choices(frame, done)

        # 7: the form may have drawn new fields in answer to a choice. Plan again once; a second change is too many.
        again = self._scan(frame)
        if self._scan_keys(again) != keys:
            self._plan_from(again, replan, uploads)
            keys = self._scan_keys(again)
            self._fill_choices(frame, done)
            if self._scan_keys(self._scan(frame)) != keys:
                raise _Stop("needs_you", KEPT_CHANGING)
        self._fill_ticks(frame)
        self._fill_text(frame)
        self._check_left_site()
        self._deferred_checks(frame)

        # 8: read every filled field back through Playwright.
        self._step = "fill"
        self._read_back(frame)

        # 9: the résumé.
        self._attach_files(frame)
        self._check_left_site()

        # 10: a CAPTCHA checkbox is noted and never touched.
        widget = self.adapter.captcha_widget(frame)
        if widget:
            self._reasons.append(CAPTCHA_NOTE)
            self._evidence_bits["captcha_widget"] = True

        # 11: the independent check, as a submit would run it.
        self._step = "check"
        self._progress("check")
        self._check_stopped()
        seen = frame.evaluate(REQUIRED_CHECK_SCRIPT)
        self._check_problems.extend(problem_dict(problem) for problem in check_required(
            seen.get("items", []), self._plan, self._schema, initial, controls=seen.get("controls", []), invalid=seen.get("invalid", []),
        ))

        # 12: a picture, and the rehearsal ends here.
        self._progress("picture")
        self._screenshot("filled")
        return self._finish("rehearsed")

    def _scan(self, frame: Any) -> list[dict[str, Any]]:
        found = frame.evaluate(_SCAN, {"profile": {}, "answers": []})
        fields = found.get("fields", []) if isinstance(found, dict) else found
        return [field for field in (fields or []) if isinstance(field, dict)]

    @staticmethod
    def _scan_keys(fields: list[dict[str, Any]]) -> frozenset[str]:
        return frozenset(str(field.get("id") or field.get("name")) for field in fields if field.get("id") or field.get("name"))

    def _snapshot(self, frame: Any) -> dict[str, Any]:
        """Each control's value before anything is typed: {key: value text, or the labels of what is checked}."""
        seen = frame.evaluate(REQUIRED_CHECK_SCRIPT)
        initial: dict[str, Any] = {}
        for control in seen.get("controls", []):
            key = control.get("key") or ""
            if not key:
                continue
            if control.get("kind") in ("radio", "checkbox"):
                labels = initial.setdefault(key, [])
                if control.get("checked"):
                    labels.extend([control.get("value_text")] if isinstance(control.get("value_text"), str) else list(control.get("value_text") or []))
            else:
                initial[key] = control.get("value_text")
        return initial

    def _plan_from(self, scan: list[dict[str, Any]], replan: Callable[[list[dict[str, Any]], bool], Any] | None, uploads: bool) -> None:
        if replan is None:
            raise _Stop("failed", PLAN_FAILED)
        try:
            plan = replan(scan, uploads)
        except Exception:  # noqa: BLE001 - never its message
            raise _Stop("failed", PLAN_FAILED)
        if plan is None:
            raise _Stop("failed", PLAN_FAILED)
        self._plan = plan
        self._join_problems = [problem_dict(problem) for problem in (_attr(plan, "problems") or []) if _attr(problem, "kind") in JOIN_KINDS]
        self._refresh_values()

    def _control_of(self, frame: Any, entry: Any) -> tuple[str, Any]:
        key = str(_attr(entry, "key"))
        self._doing = str(_attr(entry, "question") or key)
        kind = self.adapter.control_kind(frame, key)
        if kind == "missing":
            if _attr(entry, "required"):
                raise _Stop("needs_you", NO_CONTROL.format(question=self._doing))
            # An optional field the page does not draw (yet) is a gap in the rehearsal, never a stop: nothing is filled into it.
            if not any(item.get("kind") == "missing_control" and item.get("key") == key for item in self._check_problems):
                self._check_problems.append(problem_dict(Problem("missing_control", key, NO_CONTROL.format(question=self._doing), self._doing, False)))
        return kind, self.adapter.control(frame, key)

    def _fill_choices(self, frame: Any, done: set[str]) -> None:
        for entry in self._fill_entries():
            key = str(_attr(entry, "key"))
            if key in done:
                continue
            kind, control = self._control_of(frame, entry)
            if kind not in ("react_select", "select"):
                continue
            self._between()
            value = _attr(entry, "value")
            for label in (value if isinstance(value, (list, tuple)) else [value]):
                self._choose(control.first, str(label), key)
            done.add(key)

    def _fill_ticks(self, frame: Any) -> None:
        for entry in self._fill_entries():
            key = str(_attr(entry, "key"))
            kind, control = self._control_of(frame, entry)
            if kind not in ("radio", "checkbox"):
                continue
            self._between()
            value = _attr(entry, "value")
            if isinstance(value, bool):
                if control.first.is_checked() != value:
                    self._tick(control.first, key, value)
                continue
            for label in (value if isinstance(value, (list, tuple)) else [value]):
                found = [index for index, choice in enumerate(self.adapter.choices(frame, key)) if _normalize(choice["label"]) == _normalize(label)]
                if len(found) != 1:
                    raise _Stop("needs_you", FIELD_TOOK.format(question=self._doing))
                self._tick(control.nth(found[0]), key, True)

    def _fill_text(self, frame: Any) -> None:
        for entry in self._fill_entries():
            key = str(_attr(entry, "key"))
            kind, control = self._control_of(frame, entry)
            if kind not in ("text", "textarea"):
                continue
            self._between()
            self._type(control.first, str(_attr(entry, "value")), key)

    def _deferred_checks(self, frame: Any) -> None:
        """A deferred field is checked against the page and never put in it: the page must offer exactly what the plan would give."""
        for entry in self._fields():
            if _attr(entry, "disposition") != "deferred" or _attr(entry, "control") == "file":
                continue
            key = str(_attr(entry, "key"))
            before = len(self._check_problems)
            try:
                self._deferred_check(frame, entry)
            except _Stop as stop:
                # A menu that will not close is a form whose options could not be read. A stop for any other reason (the student's, or a page
                # that left the board) ends the run as it would anywhere else.
                if stop.outcome != "needs_you" or self._offsite_host():
                    raise
                self._unreadable(entry)
            except Exception:  # noqa: BLE001 - never its message; a closed window or a page that left the board is the run's to report
                if self._closed() or self._offsite_host():
                    raise
                self._unreadable(entry)
            self._checked.add(key)   # only reached when the comparison ran to its end
            if len(self._check_problems) > before:
                self._deferred_failed.add(key)

    def _unreadable(self, entry: Any) -> None:
        """The form's options for a deferred field could not be read: a problem for that field (nothing was ever put in it), and the run goes on."""
        key = str(_attr(entry, "key"))
        question = str(_attr(entry, "question") or key)
        self._check_problems.append(problem_dict(Problem("deferred", key, DEFERRED_UNREADABLE.format(question=question), question, bool(_attr(entry, "required")))))

    def _deferred_check(self, frame: Any, entry: Any) -> None:
        key = str(_attr(entry, "key"))
        question = str(_attr(entry, "question") or key)
        self._doing = question
        kind = self.adapter.control_kind(frame, key)
        value = _attr(entry, "value")
        problem = Problem("deferred", key, DEFERRED_MISSING.format(question=question), question, bool(_attr(entry, "required")))
        if kind == "missing":
            self._check_problems.append(problem_dict(problem))
            return
        if kind in ("text", "textarea"):
            # A free-text answer has no list of options: the form must only have the box, and the box must be usable (6.7).
            self._between()
            box = self.adapter.control(frame, key).first
            if not (box.is_visible() and box.is_editable()):
                self._check_problems.append(problem_dict(problem))
            return
        if kind in ("checkbox", "radio") and isinstance(value, bool):
            # A statement box: its label on the form must be one the stored statement contains (the statement may add the
            # heading and description around the option's own words, never replace them).
            statement = str(_attr(entry, "statement") or "")
            if kind == "checkbox" and statement:
                labels = [question_key(choice["label"]) for choice in self.adapter.choices(frame, key)]
                if not any(label and label in question_key(statement) for label in labels):
                    self._check_problems.append(problem_dict(problem))
            return
        self._between()
        wanted = [_normalize(label) for label in (value if isinstance(value, (list, tuple)) else [value])]
        offered = [_normalize(text) for text in self.adapter.read_options(self, frame, key)]
        if any(offered.count(label) != 1 for label in wanted):
            self._check_problems.append(problem_dict(problem))

    def _read_back(self, frame: Any) -> None:
        for entry in self._fill_entries():
            key = str(_attr(entry, "key"))
            kind, control = self._control_of(frame, entry)
            if kind == "missing":
                continue   # an optional field the page does not have: nothing was filled, so nothing is read back or said to be done
            value = _attr(entry, "value")
            held_ok = True
            if kind in ("text", "textarea"):
                held_ok = not formatting_problem(str(value), control.first.input_value())
            elif kind == "react_select":
                shown = self.adapter.react_values(self.adapter.field_container(frame, key))
                wanted = [_normalize(label) for label in (value if isinstance(value, (list, tuple)) else [value])]
                held_ok = sorted(shown) == sorted(wanted)
            elif kind == "select":
                wanted = _normalize(value)
                held_ok = _normalize(control.first.evaluate(_NATIVE_VALUE)) == wanted
            elif kind in ("radio", "checkbox"):
                choices = self.adapter.choices(frame, key)
                if isinstance(value, bool):
                    held_ok = any(choice["checked"] for choice in choices) == value
                else:
                    wanted = sorted(_normalize(label) for label in (value if isinstance(value, (list, tuple)) else [value]))
                    held_ok = sorted(_normalize(choice["label"]) for choice in choices if choice["checked"]) == wanted
            if not held_ok:
                raise _Stop("needs_you", FIELD_TOOK.format(question=self._doing))
            self._done.add(key)

    def _attach_files(self, frame: Any) -> None:
        for entry in self._fields():
            source = _attr(entry, "source")
            source_kind = _attr(source, "kind") if source is not None else ""
            if _attr(entry, "disposition") != "fill" or source_kind not in ("resume", "cover_letter"):
                continue
            key = str(_attr(entry, "key"))
            question = str(_attr(entry, "question") or key)
            self._doing = question
            payload = self._files.get("resume" if source_kind == "resume" else "cover_letter")
            if source_kind == "cover_letter":
                # Attached from M7 on. Until then the field stays empty, and the sentence says so: the app never tried to read the letter.
                self._check_problems.append(problem_dict(Problem("file", key, NO_COVER_LETTER.format(question=question), question, bool(_attr(entry, "required")))))
                continue
            if payload is None:
                # A missing résumé file is a gap in the rehearsal, not a stop.
                self._check_problems.append(problem_dict(Problem("file", key, NO_FILE.format(question=question), question, bool(_attr(entry, "required")))))
                continue
            self._between()
            if hashlib.sha256(payload.buffer).hexdigest() != _attr(entry, "file_sha256"):
                raise _Stop("needs_you", FILE_CHANGED)
            control = self.adapter.control(frame, key)
            if not control.count():
                raise _Stop("needs_you", NO_CONTROL.format(question=question))
            accept = str(control.first.evaluate(_FILE_STATE).get("accept") or "")
            if accept and not self._accepts(accept, payload):
                raise _Stop("needs_you", FILE_TYPE.format(question=question))
            self._attach(control.first, payload, key)
            state = control.first.evaluate(_FILE_STATE)
            group_text = self.adapter.field_container(frame, key).inner_text(timeout=ACTION_TIMEOUT_MS)
            if not (state["count"] == 1 and state["name"] == payload.name and state["size"] == len(payload.buffer) and payload.name in group_text):
                raise _Stop("needs_you", FIELD_TOOK.format(question=question))
            self._done.add(key)

    @staticmethod
    def _accepts(accept: str, payload: FilePayload) -> bool:
        suffix = Path(payload.name).suffix.lower()
        for token in (part.strip().lower() for part in accept.split(",") if part.strip()):
            if token.startswith(".") and token == suffix:
                return True
            if "/" in token and (token == payload.mime_type.lower() or (token.endswith("/*") and payload.mime_type.lower().startswith(token[:-1]))):
                return True
        return False

    # --- the picture and the result ------------------------------------------------------------------------------

    def _screenshot(self, step: str) -> None:
        """A full-page picture with every sensitive field's container covered. A failure here changes nothing."""
        if self.screenshot_dir is None or self._page is None:
            return
        try:
            frame = self._frame or self._page.main_frame
            keys: list[str] = []
            masks: list[Any] = []
            for entry in self._fields():
                if not _attr(entry, "sensitive"):
                    continue
                key = str(_attr(entry, "key"))
                if self.adapter.control(frame, key).count():
                    masks.append(self.adapter.field_container(frame, key))
                    keys.append(key)
            # A scrolled page puts the covers where the fields were before the scroll; the top of the page is where they are true.
            self._page.evaluate("() => window.scrollTo(0, 0)")
            data = self._page.screenshot(full_page=True, mask=masks, mask_color=SCREENSHOT_MASK_COLOR)
            path = self.screenshot_dir / f"{self.run_id or 'run'}-{step}.png"
            # Never recreates the folder: it was made when the run started, and a student whose account was deleted since has none
            # (the write then fails, and a picture never changes the outcome).
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, path)
            self._screenshots.append(
                {"step": step, "path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(), "masked": keys},
            )
        except Exception:  # noqa: BLE001 - a picture never changes the outcome
            return

    def _evidence(self) -> dict[str, Any]:
        lookups = []
        for key in sorted(self._lookups_sent):
            kind = self._lookups_sent[key]
            # "typed": whether what was typed went with the lookup. True for a kind of list that is searched by the text (location,
            # school) and for any request seen to carry the field's own text; False only when neither holds (a whole list was fetched).
            typed = kind in TYPED_LOOKUP_KINDS or key in self._lookups_typed
            lookups.append({"key": key, "question": self._question(key), "kind": kind, "typed": typed})
        return {
            **self._evidence_bits, "lookups": lookups, "submit_path_hit": self._submit_path_hit, "refused_total": self._refused_total,
            "filled_keys": sorted(self._done), "checked_keys": sorted(self._checked),
            "deferred_failed_keys": sorted(self._deferred_failed),
        }

    def _plan_entries(self) -> tuple[list[dict[str, Any]], str]:
        """The value-free entries of the plan (``policy.plan_entries``), and its hash. Never raises."""
        if self.mode == "lookup" or not self._fields():
            return [], ""   # a lookup plans nothing
        try:
            from .policy import plan_entries   # lazy: the child imports the policy only when it has a plan to describe

            entries = plan_entries(self._plan)
        except Exception:  # noqa: BLE001 - a plan that is not a policy.Plan (a test's stand-in) is described by name
            entries = []
            for item in self._fields():
                source = _attr(item, "source") or {}
                entries.append({
                    "key": _attr(item, "key"), "question": _attr(item, "question"), "control": _attr(item, "control"),
                    "required": bool(_attr(item, "required")), "options": list(_attr(item, "options") or ()), "sensitive": _attr(item, "sensitive"),
                    "disposition": _attr(item, "disposition"),
                    "source": {"kind": _attr(source, "kind", "none"), "ref": _attr(source, "ref", ""), "company": "", "reusable": False, "links": []},
                    "value_mac": _attr(item, "value_mac", "") or "", "file_sha256": _attr(item, "file_sha256", "") or "",
                    "problem": _attr(item, "problem", "") or "",
                })
        return entries, str(_attr(self._plan, "plan_hash") or "")

    def _finish(self, outcome: str, first: Sequence[str] = ()) -> RunResult:
        reasons: list[str] = []
        for sentence in [*first, *self._reasons]:
            if sentence and sentence not in reasons:
                reasons.append(sentence)
        entries, plan_hash = self._plan_entries()
        return RunResult(
            outcome=outcome, reasons=reasons, plan=entries, plan_hash=plan_hash, join_problems=list(self._join_problems),
            check_problems=list(self._check_problems), options=dict(self._options), screenshots=list(self._screenshots),
            refused=list(self._refused), requests=[], evidence=self._evidence(), handed_over=False, after_click=False,
        )


class DefaultApplyAgentFactory:
    """What the real app builds an agent from. A module-level object with no state, so it pickles into the child process."""

    isolation = "process"

    def available(self) -> str:
        return PlaywrightProbe().available()

    def __call__(
        self, *, mode: str, run_id: str, screenshot_dir: Path | None, timeouts: ApplyTimeouts,
        on_progress: Callable[[str, str], None], heartbeat: Callable[[], None],
    ) -> ApplyAgent:
        return ApplyAgent(
            mode=mode, adapter=GreenhouseAdapter(), run_id=run_id, screenshot_dir=screenshot_dir, timeouts=timeouts,
            on_progress=on_progress, heartbeat=heartbeat,
        )
