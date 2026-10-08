"""The browser driver of Apply for me: Playwright, a Greenhouse adapter, and one rule for what may leave the page.

``ApplyAgent`` runs in three modes: ``lookup`` (type what the student typed into one typeahead and read the options it
offers), ``rehearse`` (fill the whole form in a visible window, read every field back, check the form the way a submit
would, take a picture with the sensitive fields covered, and stop) and ``handoff`` ("Finish in browser": the same fill, then
the window is the student's, and only the student's own press of Submit application can send anything). ``submit`` exists
as a name and returns ``failed`` without opening a browser. **The agent never presses Submit, in any mode, not even the
second one after a security code.** Before the student's turn, and in lookup and rehearse always, only GET, HEAD and
OPTIONS leave the page, after the first input only the typed field's own lookup and Greenhouse's static assets do, and
``checks.route_decision`` is the only thing that says so. In a handoff the student's Submit is asked of the parent first
(``hand_over``): the request continues only when the parent committed it, so the claim says "clicking" before a byte leaves.

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

import fnmatch
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from .. import ROOT
from ..integrations.web_fetch import Resolver, close_browser, resolve_host
from ..outreach.forms import CAPTCHA_WIDGETS, formatting_problem
from ..outreach.render import request_allowed
from .agent_types import (
    BUILT_MODES,
    HANDOFF_EARLY,
    HANDOFF_ELSEWHERE,
    HANDOFF_HIDDEN,
    HANDOFF_NO_LOADER,
    HANDOFF_CRASHED,
    HANDOFF_NOT_SUBMITTED,
    HANDOFF_S3,
    HANDOFF_UNRECORDED,
    HANDOFF_UPLOAD,
    LEFT_CAPTCHA,
    LEFT_COVER_LETTER_CHANGED,
    LEFT_FIELD,
    LEFT_UNPLANNED,
    MAX_LOOKUP_OPTIONS,
    OP_HANDOFF_READY,
    PROGRESS_STEPS,
    STOPPED,
    WINDOW_CLOSED,
    ApplyTimeouts,
    FilePayload,
    HandoffLink,
    LookupRequest,
    RunResult,
    problem_dict,
)
from .checks import (
    CAPTCHA_ENDPOINTS,
    CONFIRMED_CAPTCHA_HOSTS,
    FORM_POST_HOSTS,
    GREENHOUSE_LOOKUP_ENDPOINTS,
    PHASE_AFTER_HAND_OVER,
    STATIC_ASSET_HOSTS,
    TYPED_LOOKUP_KINDS,
    PHASE_AFTER_INPUT,
    PHASE_BEFORE_INPUT,
    PHASE_FILL,
    PHASE_STUDENT,
    REQUIRED_CHECK_SCRIPT,
    SAFE_METHODS,
    TELEMETRY_HOSTS,
    UNCONFIRMED_NOTE,
    Abort,
    Endpoint,
    Observation,
    Outcome,
    Problem,
    RouteRequest,
    RouteState,
    SeenRequest,
    check_required,
    confirmation_reached,
    decide_outcome,
    is_upload,
    leaked_field,
    new_code_prompt,
    question_key,
    route_decision,
    safe_host,
    student_submit_elsewhere,
)
from .greenhouse import BOARD_HOSTS, SUBMIT_HOST
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
LETTER_CHANGED = 'Your cover letter for this role changed while the rehearsal ran, so the app did not attach one for "{question}". Approve the one you want and run again'
NO_CONTROL = 'The form has no field for "{question}"'
NO_OPTIONS = "No options came back for what you typed"
NO_ENDPOINT = "The app has not confirmed Greenhouse's lookup service for this list yet, so it did not ask it"
PLAN_FAILED = "The app could not plan this form"
# Not in the shared list: what the agent says when a whole step, not one field, went wrong.
OPEN_FAILED = "The app could not open the Greenhouse form"
READ_FAILED = "The app could not read the form"
CHECK_FAILED = "The app could not check the filled form"
# agent_types.WINDOW_CLOSED is the student closing the window during a handoff; this is the window going away in any other run.
WINDOW_GONE = "The browser window was closed before the run finished"
DIFFERENT_POSTING = "Greenhouse opened a different posting from the one the app was asked to open"
DEFERRED_MISSING = 'The form does not offer the answer the app would give for "{question}"'
DEFERRED_UNREADABLE = 'The app could not read the options of "{question}", so it could not check the answer it would give'
NOT_FILLED = "The run stopped before the app filled and checked this field, so nothing was put in it"
# A field the app did act on (typed, ticked, chosen or attached) before the run stopped, and had not yet read back.
TYPED_NOT_CHECKED = "The run stopped after the app typed into this field and before it checked it, so the page may still hold what was typed"

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
MAX_TELEMETRY_RECORDED = 20
MAX_REQUESTS = 300
CODE_PATTERN = re.compile(r"[A-Za-z0-9]{8}")
CODE_GUARD_S = 2.0       # after the app typed a security code, no submit POST passes for this long (a widget that submits by itself)
CODE_SETTLE_S = 0.3      # after typing the code: time for a widget that sends by itself to try, before the student is told what to do
TURN_POLL_MS = 250
OUTCOME_POLL_MS = 500

# Things in Chromium that send bytes without any request the route handler sees, so a script on the board could carry a
# filled-in answer out through them: WebRTC (STUN and TURN traffic over sockets of its own, to an ICE server named by an IP address
# too, which no name lookup stops), fetchLater (a request queued now and sent when the page goes away), a beacon and a keepalive fetch (the
# same: a request the browser sends as the page closes, after the route handler is no longer asked), FedCM (the browser itself fetches a
# well-known file and a config from a host the script names), WebTransport (QUIC), WebSocketStream, and any dedicated or shared worker
# (Playwright's WebSocket guard patches the page's own WebSocket only, and an init script does not run inside a worker, so a worker's
# WebSocket and WebTransport are out of reach). Speculation rules and a prerender link make the browser itself fetch (or load) a page, a
# prefetch to any site a script names; the Protected Audience calls (joinAdInterestGroup and the rest) make it look up and call the
# owner's host; Shared Storage fetches a module. Nothing on a Greenhouse form needs one of them, so each is removed in every frame before
# the page's own scripts run, and some are also switched off at launch (LAUNCH_ARGS), which covers a realm this script missed. It hides
# nothing about the browser: a page can tell they are not there.
#
# Everything this script does later (the sweep, the shadow-root hook, the fetch wrapper) uses copies of the page's built-ins that it took
# first, and calls them through ``call``, a bound copy of Function.prototype.call: a page that overwrites Element.prototype.remove,
# querySelectorAll, NodeList.prototype.forEach, Function.prototype.call and the rest cannot turn a sweep into a no-op.
NO_SIDE_CHANNELS = """(() => {
  const call = Function.prototype.call.bind(Function.prototype.call);
  const apply = Reflect.apply, construct = Reflect.construct, reflectGet = Reflect.get, define = Object.defineProperty;
  const names = [
    'RTCPeerConnection', 'webkitRTCPeerConnection', 'RTCDataChannel', 'RTCSessionDescription', 'RTCIceCandidate',
    'fetchLater', 'FetchLaterResult', 'IdentityCredential', 'IdentityProvider', 'IdentityCredentialError',
    'WebTransport', 'WebTransportBidirectionalStream', 'WebTransportDatagramDuplexStream', 'WebTransportError', 'WebSocketStream',
    'Worker', 'SharedWorker', 'sharedStorage', 'SharedStorage', 'SharedStorageWorklet',
  ];
  for (const name of names) {
    try { delete window[name]; } catch (error) { /* already gone */ }
    try { define(window, name, {value: undefined, configurable: false, writable: false}); } catch (error) { /* kept as it is */ }
  }
  const navigatorCalls = [
    'joinAdInterestGroup', 'leaveAdInterestGroup', 'clearOriginJoinedAdInterestGroups', 'updateAdInterestGroups', 'runAdAuction',
    'createAuctionNonce', 'getInterestGroupAdAuctionData', 'canLoadAdAuctionFencedFrame',
  ];
  for (const name of navigatorCalls) {
    try { delete Navigator.prototype[name]; } catch (error) { /* already gone */ }
    try { define(Navigator.prototype, name, {value: undefined, configurable: false, writable: false}); } catch (error) { /* kept as it is */ }
  }
  const fix = (target, name, value) => { try { define(target, name, {value, configurable: false, writable: false, enumerable: false}); } catch (error) { /* kept as it is */ } };
  try {
    const get = CredentialsContainer.prototype.get;
    fix(CredentialsContainer.prototype, 'get', function get_(options) {
      if (options && options.identity) return Promise.reject(new DOMException('Not supported', 'NotSupportedError'));
      return apply(get, this, arguments);
    });
  } catch (error) { /* no credentials container */ }
  // What a page sends as it closes is sent after the route handler is no longer asked: a beacon, and a fetch that is kept alive past the
  // page. Neither is needed here, so a beacon is refused (as the browser does when it cannot queue one) and keepalive is always false.
  // A request a closing page makes without keepalive is cancelled by the browser, so a value cannot ride on one.
  try {
    fix(Navigator.prototype, 'sendBeacon', function () { return false; });
  } catch (error) { /* no beacons */ }
  try {
    const plain = (init) => (init !== null && typeof init === 'object')
      ? new Proxy(init, {get: (target, key) => (key === 'keepalive' ? false : reflectGet(target, key, target))}) : init;
    const nativeFetch = window.fetch, NativeRequest = window.Request;
    fix(window, 'fetch', function (input, init) { return apply(nativeFetch, this, [input, plain(init)]); });
    const WrappedRequest = new Proxy(NativeRequest, {
      construct: (target, args, newTarget) => construct(target, [args[0], plain(args[1])], newTarget),
    });
    fix(NativeRequest.prototype, 'constructor', WrappedRequest);
    fix(window, 'Request', WrappedRequest);
  } catch (error) { /* no fetch */ }
  // The page gets to run for a moment as it is closed, and what it asks the browser to send then is not routed: Playwright stalls every
  // request once the page is closing, and Chromium lets a stalled one go when the page's session ends (measured: an image, a plain fetch,
  // an XHR and a stylesheet link made from a pagehide, unload or visibilitychange handler all reached a listener, with a route that
  // refuses everything). So the events that announce it never reach a page script: this listener is registered first, on the window,
  // in the capture phase, and stops the event for every listener after it. Nothing a Greenhouse form needs listens for one.
  try {
    const addListener = EventTarget.prototype.addEventListener, stopNow = Event.prototype.stopImmediatePropagation;
    const swallow = (event) => { call(stopNow, event); };
    for (const type of ['pagehide', 'unload', 'beforeunload', 'visibilitychange', 'freeze', 'pageswap']) call(addListener, window, type, swallow, true);
  } catch (error) { /* no events */ }
  // Speculation rules (a script of that type) and a prerender link are acted on by the browser with no request the route handler
  // sees. A rule set is read when its element is inserted and the candidates are worked out a microtask later, and this observer's
  // microtask is queued first, so the element is gone before the browser has anything to fetch. Nothing the form needs is one.
  const SPECULATION = 'script[type="speculationrules" i], link[rel~="prerender" i]';
  const Observer = MutationObserver, observe = MutationObserver.prototype.observe;
  const removeNode = Element.prototype.remove, itemOf = NodeList.prototype.item;
  const lengthOf = Object.getOwnPropertyDescriptor(NodeList.prototype, 'length').get;
  const findIn = {document: Document.prototype.querySelectorAll, fragment: DocumentFragment.prototype.querySelectorAll};
  const sweep = (root, find) => {
    try {
      const found = call(find, root, SPECULATION);
      const count = call(lengthOf, found);
      for (let index = 0; index < count; index += 1) call(removeNode, call(itemOf, found, index));
    } catch (error) { /* gone */ }
  };
  const watch = (root, find) => {
    try {
      call(observe, new Observer(() => sweep(root, find)), root, {childList: true, subtree: true, attributes: true, characterData: true});
      sweep(root, find);
    } catch (error) { /* not observable */ }
  };
  watch(document, findIn.document);
  try {
    const attach = Element.prototype.attachShadow;
    fix(Element.prototype, 'attachShadow', function attachShadow(init) {
      const root = apply(attach, this, arguments);
      watch(root, findIn.fragment);
      return root;
    });
  } catch (error) { /* no shadow roots */ }
})();"""
# Chromium switches that turn some of those features off, and one that closes the rest at the network layer. None changes how the browser
# presents itself to a page (user agent, language, screen); they only remove features this app has no use for. WebTransport has no switch
# (measured on Playwright 1.62's Chromium: neither a feature flag nor --disable-quic stops its packets), so it, and every worker that could
# reach one, is closed by the init script alone, in every realm a page can make (tests/test_apply_agent_browser.py names each one).
#
# The resolver rule is the catch-all: the browser can look up only the hosts a Greenhouse form and its fonts, lookups, static files and
# CAPTCHA use (``RESOLVABLE_HOSTS``), and any other name, an IP address included, fails inside Chromium with no query leaving the machine
# (WebRTC to an ICE server given as an IP address is not a name lookup: the init script is the only thing that stops it. Measured on
# Playwright 1.62's Chromium, the resolver rule left ten packets reaching a loopback listener, and no switch silenced both STUN over UDP
# and TURN over TCP: ``--force-webrtc-ip-handling-policy=disable_non_proxied_udp`` stops the UDP and not the TCP, and the blink and
# feature names tried did nothing. tests/test_apply_agent_browser.py pins that, so a Chromium that gains a switch shows up there.)
# The list is only what a rehearsal needs before Submit, because a request a closing page makes is not routed, and only a name that does
# not resolve is certain to stop it: not Greenhouse's analytics collector (c.spl), my.greenhouse.io, www.google.com, or a CAPTCHA service a
# Greenhouse form has not been seen to use.
# A hint (dns-prefetch, preconnect), a prefetch, an interest-group owner or a script's own request to a host it names then goes nowhere,
# whatever channel it takes, and a value put in a host name is never looked up: not by Chromium, and not by this process either, since the
# route handler refuses a name outside the list (``unlisted_host``) before its own resolver is asked about it, in every mode and phase. The third-party widgets a board may load (Google Drive,
# Dropbox, a recruiting-analytics script) do not load either: the app presses none of them.
RESOLVABLE_HOSTS: tuple[str, ...] = tuple(sorted({
    *BOARD_HOSTS, *(endpoint.host for endpoint in GREENHOUSE_LOOKUP_ENDPOINTS), *STATIC_ASSET_HOSTS,
    "s?-recruiting.cdn.greenhouse.io", "s??-recruiting.cdn.greenhouse.io", "s???-recruiting.cdn.greenhouse.io",
    *CONFIRMED_CAPTCHA_HOSTS, "fonts.googleapis.com", "fonts.gstatic.com",
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
    """The run ends here, with this outcome and this sentence. Never carries a value. ``end`` is a handoff's ``handoff_end``."""

    def __init__(self, outcome: str, reason: str, end: str = "") -> None:
        super().__init__(outcome)
        self.outcome = outcome
        self.reason = reason
        self.end = end


class _FieldDidNotTake(Exception):
    """Handoff only: a field did not take the answer. The agent clears it and leaves it for the student."""


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


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


PATH_WITHHELD = "[path withheld]"


def _captcha_path(host: str, path: str) -> bool:
    return any(endpoint.host == host and path.startswith(endpoint.path_prefix) for endpoint in CAPTCHA_ENDPOINTS)


def _job_id(page_url: str) -> str:
    """The numeric job id of a posting address, or the embed's ``token``, or ""."""
    try:
        parts = urlsplit(page_url)
    except ValueError:
        return ""
    match = re.search(r"/jobs/(\d+)/?$", parts.path)
    if match:
        return match.group(1)
    token = (parse_qs(parts.query).get("token") or [""])[0]
    return token if token.isdigit() else ""


def _source_kind(entry: Any) -> str:
    source = _attr(entry, "source")
    return str(_attr(source, "kind") or "none") if source is not None else "none"


def _texts(value: Any) -> list[str]:
    """A control's value as a list of non-empty strings (a list stays a list; None and "" are empty)."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value if str(item).strip()]
    return [] if value is None or not str(value).strip() else [str(value)]


class _Overlay:
    """A plan entry as the agent now sees it: its own disposition (and problem) replaced, every other attribute the entry's own."""

    def __init__(self, entry: Any, changed: tuple[str, str]) -> None:
        self._entry = entry
        self.disposition, self._reason = changed

    @property
    def problem(self) -> str:
        return self._reason if self.disposition == "left_for_you" else str(_attr(self._entry, "problem") or "")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        missing = object()
        found = _attr(self._entry, name, missing)
        if found is missing:
            raise AttributeError(name)
        return found


class _View:
    """What ``check_required`` reads of a plan: its entries and its hash."""

    def __init__(self, fields: list[Any], plan_hash: str) -> None:
        self.fields = fields
        self.plan_hash = plan_hash


class _Observer:
    """Everything a handoff watches after the student's Submit, kept append-only from the moment the turn begins.

    The route handler tells it about every non-GET request after hand-over and whether the route let it through (``track``): that
    record, never the browser's ``requestfailed``, says what was refused. The status of an answer and a main-frame navigation come
    from page events, which only set values here; every decision is made in the agent's own loop.
    """

    def __init__(
        self, page: Any, active: Callable[[], bool], values: Callable[[], Mapping[str, Any]] = lambda: {},
        submit_path: Callable[[], str] = lambda: "",
    ) -> None:
        self.page = page
        self._active = active
        self._values = values
        self._submit_path = submit_path
        self.tracked: list[tuple[Any, str, str, str, bool]] = []
        self.statuses: dict[int, int] = {}
        self.navigated = False

    def start(self) -> None:
        self.page.on("response", self._response)
        self.page.on("framenavigated", self._navigated)

    def track(self, request: Any, *, passed: bool) -> None:
        if not passed and len(self.tracked) >= MAX_REQUESTS:
            return   # a request that passed is always kept; a flood of refused ones is only counted elsewhere
        parts = urlsplit(request.url)
        self.tracked.append((request, request.method.upper(), (parts.hostname or "").lower(), parts.path, passed))

    def _response(self, response: Any) -> None:
        self.statuses[id(response.request)] = int(response.status)

    def _navigated(self, frame: Any) -> None:
        try:
            if self._active() and frame == self.page.main_frame:
                self.navigated = True
        except Exception:  # noqa: BLE001 - a page that is going away
            pass

    def seen(self) -> tuple[SeenRequest, ...]:
        return tuple(SeenRequest(method, host, path, self.statuses.get(id(request)), passed) for request, method, host, path, passed in self.tracked)

    def records(self) -> list[dict[str, Any]]:
        """Every non-GET after hand-over, value-free: method, host, path (never a query or a body), status, whether it passed.

        These cross the pipe and are written to the database, so they follow the rule every refused-request record follows: a host that
        holds a planned value is withheld, and the path of a request is kept only when it is the one the app can name (the submit address
        on the board, a CAPTCHA service's) or when nothing planned is in it and the request passed. A script chooses both of a refused
        request's host and path.
        """
        values = self._values()
        submit_path = self._submit_path()
        records = []
        for request, method, host, path, passed in self.tracked:
            shown = safe_host(host, values)
            if host == SUBMIT_HOST and submit_path and path == submit_path:
                kept = path
            elif host in (*BOARD_HOSTS, SUBMIT_HOST) or _captcha_path(host, path):
                kept = path if passed and not (values and leaked_field(RouteRequest(method="GET", url=path), values)) else PATH_WITHHELD
            else:
                kept = PATH_WITHHELD
            records.append({"method": method, "host": shown, "path": kept, "status": self.statuses.get(id(request)), "passed": passed})
        return records


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
    def loader_paths(html: str) -> tuple[str, str, str]:
        """(the submit host, "submitPath", "confirmationPath") from the served HTML; "" for one that is absent or on another host.

        The host is the one the submit path names (``job-boards.greenhouse.io`` is a board host the request rules do not accept
        as the submit host), or the submit host itself for a relative path, which is how the board's own script posts it.
        """
        found = []
        host = ""
        for name in ("submitPath", "confirmationPath"):
            match = re.search(_LOADER_KEY.format(name=name), html or "")
            path = ""
            if match:
                try:
                    value = str(json.loads(match.group(1)))
                    parts = urlsplit(value)
                    if not parts.hostname or (parts.hostname.lower() in BOARD_HOSTS):
                        path = parts.path
                        if name == "submitPath" and path:
                            host = (parts.hostname or SUBMIT_HOST).lower()
                except ValueError:
                    path = ""
            found.append(path)
        return host, found[0], found[1]

    def uploads_on_attach(self, frame: Any) -> bool:
        """The form (or an upload group in it) says a file is uploaded the moment it is attached."""
        return bool(frame.locator(
            'form#application-form[data-allow-s3="true"], form#application-form [data-allow-s3="true"]').count())

    def security_code_prompt(self, frame: Any) -> bool:
        """The emailed security code's first box is on the form and visible (the form asks for the code)."""
        try:
            box = frame.locator("form#application-form #security-input-0")
            return bool(box.count() and box.first.is_visible())
        except Exception:  # noqa: BLE001 - a page that cannot answer shows no prompt
            return False

    def security_code_inputs(self, frame: Any) -> list[Any] | None:
        """Exactly eight visible boxes ``#security-input-0`` to ``-7`` inside the application form, else None."""
        boxes = []
        for index in range(8):
            box = frame.locator(f'form#application-form [id="security-input-{index}"]')
            if box.count() != 1 or not box.first.is_visible():
                return None
            boxes.append(box.first)
        return boxes

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
        self._check_file: Callable[[str, str, str], bool] | None = None
        self._loaded = False
        self._step = "open"
        self._doing = ""
        # Finish in browser (handoff). The route handler and the student's turn share these; one thread, so no lock.
        self._link: HandoffLink | None = None
        self._ends_at = 0.0
        self._closing = False             # every non-GET is aborted from here on (set before the browser is closed)
        self._why_closing = ""            # "refused" | "elsewhere" | "upload" | "stopped"
        self._early = False               # a submit POST was aborted during the fill
        self._upload_refused: dict[str, str] | None = None
        self._handed_over = False         # the parent committed the hand-over and the student's POST was continued
        self._submit_continued = False    # set just before route.continue_(): unsure means sent
        self._observer: _Observer | None = None
        self._overrides: dict[str, tuple[str, str]] = {}   # key -> (disposition, reason): what the agent changed in the plan
        self._extra_left: list[dict[str, str]] = []        # things the student must do that are not plan entries
        self._captcha_left: list[dict[str, str]] = []
        self._page_defaults: list[str] = []
        self._initial: dict[str, Any] = {}
        self._handoff_end = ""
        self._browser_closed = False
        self._parent_gone = False
        self._code_typed_once = False
        # A refused submit POST counts as the widget sending the code by itself only before this instant (inf while the app types).
        self._code_auto_until = 0.0
        # Handoff: the keys of the fields the agent filled and read back. Only these are described as filled in the result.
        self._filled: set[str] = set()
        # Handoff: the keys the agent acted on at all, recorded as each action starts. A key here and not in ``_filled`` was typed (or may
        # have been) and never read back: it is never described as untouched.
        self._typed: set[str] = set()
        self._evidence_code: dict[str, Any] = {
            "prompted": False, "typed": False, "fallback": False, "posted": False, "rounds": 0, "auto_submit_blocked": False, "reason": "",
        }
        self._challenge = False
        self._outcome_evidence: dict[str, Any] = {}
        self._last_seen: tuple[str, str, bool] = ("", "", True)
        self._last_beat = 0.0
        self._telemetry_recorded = 0
        self._hook_error: BaseException | None = None
        self._job_path = ""
        self._filled_shot: dict[str, Any] | None = None
        self._captcha_widget_seen = False
        self._page_url = ""
        self._confirmation_path = ""

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
            method = request.method.upper()
            if self._closing and method not in SAFE_METHODS:
                # The run is ending and the browser is about to be closed: nothing that could carry anything leaves.
                self._refuse({"method": method, "host": safe_host(_host_of(request.url), self._state.values), "rule": "closing"})
                route.abort("blockedbyclient")
                return
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
            # A name the browser itself could not look up is not looked up here either: the resolver below runs in this process, outside the
            # --host-resolver-rules switch, so a name made up from something typed would leave the machine as a DNS query. Every mode, every phase.
            if not isinstance(decision, Abort) and not self._resolvable(_host_of(request.url)):
                decision = Abort("unlisted_host", "The address is not one this form uses", safe_host(_host_of(request.url), self._state.values))
            if not isinstance(decision, Abort) and self._route_hook is None and not request_allowed(request.url, self._resolve, self._allowed):
                decision = Abort(
                    "non_public_address", "The address is not a public one",
                    safe_host((urlsplit(request.url).hostname or "").lower().rstrip("."), self._state.values),
                )
            if isinstance(decision, Abort):
                self._abort_request(route, request, facts, decision, own_page=own_page)
                return
            if getattr(decision, "requires_hand_over", False):
                self._hand_over_and_continue(route, request, decision)
                return
            self._state.record(decision)
            if self._phase == PHASE_AFTER_HAND_OVER and method not in SAFE_METHODS and self._observer is not None:
                self._observer.track(request, passed=True)
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

    def _resolvable(self, host: str) -> bool:
        """Whether this host is one of the names the browser may look up (``RESOLVABLE_HOSTS`` and a test's own lookup endpoints)."""
        if not host:
            return False
        patterns = (*RESOLVABLE_HOSTS, *(endpoint.host for endpoint in self._endpoints))
        return any(fnmatch.fnmatchcase(host, pattern) for pattern in patterns)

    def _abort_request(self, route: Any, request: Any, facts: RouteRequest, decision: Abort, *, own_page: bool = False) -> None:
        record = decision.record(request.method)
        if decision.rule == "offsite_navigation":
            # Kept apart from the refused list (which keeps only its first records), so a late off-site load still ends the run.
            if own_page:
                if self._offsite_first is None:
                    self._offsite_first = str(record.get("host") or "")
            else:
                record["popup"] = True
                self._popup_seen = True
        self._refuse(record)
        self._inflight.discard(id(request))
        unsafe = facts.method.upper() not in SAFE_METHODS
        if self.mode == "handoff":
            if self._phase == PHASE_AFTER_HAND_OVER and unsafe and self._observer is not None:
                self._observer.track(request, passed=False)
            if decision.rule == "code_post_while_typing" and time.monotonic() < self._code_auto_until:
                # Only a POST that arrives while the code is being typed, or within CODE_SETTLE_S of it, is the widget's own: a press
                # by the student a moment later is refused too (the guard runs CODE_GUARD_S) but says nothing about the widget.
                self._evidence_code["auto_submit_blocked"] = True
            if unsafe and self._phase == PHASE_FILL:
                if decision.rule == "before_hand_over":
                    self._early = True
                elif _host_of(request.url) not in TELEMETRY_HOSTS and (is_upload(facts) or _host_of(request.url) in FORM_POST_HOSTS):
                    # During the fill either one is fatal: a page that uploads or posts as it is filled is not one the app can leave alone.
                    # (The page's own usage reporting is neither: it is refused, recorded a few times, and the fill goes on.)
                    self._upload_refused = {"host": safe_host(_host_of(request.url), self._state.values), "rule": decision.rule}
            elif unsafe and self._phase == PHASE_STUDENT:
                # A POST to a form address (with or without a file in it) is the form sending somewhere the app did not agree to;
                # an upload to any other address is a file leaving. Either ends the turn: the window is closed, nothing was sent.
                # The form's own submission carries the résumé, so a file in a POST to the submit address is the form sending somewhere
                # the app did not agree to. A file going to any other address (a résumé or cover-letter parse on a board's API, a
                # storage host) is a file leaving, whatever the address: that one is an upload, and gets the upload sentence.
                host = _host_of(request.url)
                file_leaving = is_upload(facts) and host not in TELEMETRY_HOSTS
                if file_leaving and host != SUBMIT_HOST:
                    self._closing, self._why_closing = True, "upload"
                elif student_submit_elsewhere(facts, self._state):
                    self._closing, self._why_closing = True, "elsewhere"
                elif file_leaving:
                    self._closing, self._why_closing = True, "upload"
        route.abort("blockedbyclient")

    def _hand_over_and_continue(self, route: Any, request: Any, decision: Any) -> None:
        """The student's own Submit. It goes on only if the parent committed the hand-over first (I1)."""
        if self._cancel_requested():
            self._refuse({"method": "POST", "host": safe_host(_host_of(request.url), self._state.values), "rule": "stopped"})
            self._closing, self._why_closing = True, "stopped"
            route.abort("blockedbyclient")
            return
        accepted = False
        try:
            accepted = self._hand_over is not None and self._hand_over() is True
        except Exception:  # noqa: BLE001 - no answer is no
            accepted = False
        if not accepted:
            self._refuse({"method": "POST", "host": safe_host(_host_of(request.url), self._state.values), "rule": "hand_over_refused"})
            self._closing, self._why_closing = True, "refused"
            route.abort("blockedbyclient")
            return
        self._phase = PHASE_AFTER_HAND_OVER      # before continuing: a second POST is a second one
        self._handed_over = True
        self._state.record(decision)
        self._submit_path_hit = True
        if self._observer is not None:
            self._observer.track(request, passed=True)
        self._submit_continued = True            # just before continuing: unsure means sent
        (self._route_hook or (lambda handled: handled.continue_()))(route)

    def _refuse_socket(self, ws: Any) -> None:
        # Refused by never calling connect_to_server(); closing from inside the handler deadlocks the sync API.
        host = safe_host((urlsplit(getattr(ws, "url", "") or "").hostname or "").lower(), self._state.values)
        self._refuse({"method": "WEBSOCKET", "host": host, "rule": "websocket"})

    def _refuse(self, record: dict[str, Any]) -> None:
        self._refused_total += 1
        if record.get("rule") == "telemetry":
            # The page reports on itself with every keystroke: it is refused every time and recorded a few times, so it cannot
            # crowd out the refusals that matter.
            self._telemetry_recorded += 1
            if self._telemetry_recorded > MAX_TELEMETRY_RECORDED:
                return
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
        # After hand-over a cancel means the parent is gone or the server is shutting down, never "nothing was sent" (4.5 handles it).
        if self._phase != PHASE_AFTER_HAND_OVER and self._cancel_requested():
            raise _Stop("failed", STOPPED, "stopped" if self.mode == "handoff" else "")
        if self.mode == "handoff" and key and key != "security_code":
            self._typed.add(key)
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
        """fill() (trusted input events); then change and blur, except into a search box, whose menu a blur would close.

        The one key that is not a plan field is ``security_code``, accepted only while the agent is typing the emailed code
        (``RouteState.code_typing``), one character to a box.
        """
        if key == "security_code" and not self._state.code_typing:
            raise ClickRefused("The app types the security code only when the page asks for it")
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
        """Set a box or a radio. ``checked`` False unticks a box the agent ticked when its read-back failed."""
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
                if self.mode == "handoff":
                    raise _FieldDidNotTake(problem)
                raise _Stop("needs_you", problem)
            return
        self._begin(key)
        try:
            locator.select_option(label=label, timeout=ACTION_TIMEOUT_MS)
        finally:
            self._release()
        self._wait(self.timeouts.between_fields_s)

    def _attach(self, locator: Any, payload: FilePayload | None, key: str) -> None:
        """Attach the file, or (``payload`` None) clear the input: used only to undo an attach that did not take."""
        self._begin(key)
        try:
            locator.set_input_files([] if payload is None else payload.as_playwright(), timeout=ACTION_TIMEOUT_MS)
        finally:
            self._release()
        self._wait(self.timeouts.between_fields_s)

    def _click(self, locator: Any, purpose: str, key: str = "") -> None:
        """Press something, only if the allowlist says this exact thing may be pressed. Raises ``ClickRefused`` otherwise."""
        if purpose == "captcha_checkbox":
            # D14 A: the app never presses a CAPTCHA box. The student ticks it, in the window, before hand-over.
            raise ClickRefused("The app does not press a CAPTCHA box")
        if purpose == "submit":
            # Finish in browser: the student presses Submit, D1 B. The agent never does, in any mode, not even the second
            # Submit after a security code.
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

    def _raw_fields(self) -> list[Any]:
        fields = _attr(self._plan, "fields", None)
        return list(fields) if fields is not None else []

    def _fields(self) -> list[Any]:
        """The plan's entries, each seen through what the agent changed (``_overrides``): a field it left for the student reads as left."""
        entries = self._raw_fields()
        if not self._overrides:
            return entries
        return [_Overlay(entry, self._overrides[str(_attr(entry, "key"))]) if str(_attr(entry, "key")) in self._overrides else entry for entry in entries]

    def _entry(self, key: str) -> Any | None:
        for entry in self._fields():
            if _attr(entry, "key") == key:
                return entry
        return None

    def _leave(self, key: str, disposition: str, reason: str) -> None:
        """Handoff: this field is the student's (``left_for_you``) or stays empty (``blank``), for this reason."""
        self._overrides[key] = (disposition, reason)

    def _question(self, key: str) -> str:
        if self._lookup is not None and key == self._lookup.key:
            return self._lookup.question
        entry = self._entry(key)
        return str(_attr(entry, "question") or key) if entry is not None else key

    def _fill_entries(self) -> list[Any]:
        return [entry for entry in self._fields() if _attr(entry, "disposition") == "fill" and _attr(entry, "control") != "file"]

    def _refresh_values(self) -> None:
        """The values the request guard watches for: every planned value that is, or was, typed into the page.

        A value stays guarded when the agent later cleared its field and left it for the student: the page's scripts have seen it.
        """
        values: dict[str, Any] = dict(self._state.values)
        for entry in self._raw_fields():
            value = _attr(entry, "value")
            if _attr(entry, "disposition") in ("fill", "deferred") and _attr(entry, "control") != "file" and value is not None:
                values[str(_attr(entry, "key"))] = value
        if self._lookup is not None and self._lookup.text:
            values[self._lookup.key] = self._lookup.text
        for key, texts in self._typed_texts.items():
            held = values.get(key)
            values[key] = [*(held if isinstance(held, (list, tuple)) else [] if held is None else [held]), *sorted(texts)]
        self._state.values = values

    # --- progress, time and the window --------------------------------------------------------------------------

    def _progress(self, step: str, **words: Any) -> None:
        if self._on_progress is not None:
            try:
                self._on_progress(step, PROGRESS_STEPS[step].format(**words))
            except (OSError, ValueError):   # a closed pipe: the parent is gone, and that is not an error on this path
                self._parent_gone = True

    def _beat(self) -> None:
        self._last_beat = time.monotonic()
        if self._heartbeat is not None:
            try:
                self._heartbeat()
            except (OSError, ValueError):
                self._parent_gone = True

    def _pulse(self) -> None:
        """A heartbeat when one is due: every wait loop calls this, so the claim never looks stale while the student works."""
        if time.monotonic() - self._last_beat >= self.timeouts.heartbeat_s:
            self._beat()

    def _cap(self, instant: float) -> float:
        """A wait's end, never later than the runner's cap (``ends_at``), so the agent ends before the watchdog does."""
        return min(instant, self._ends_at) if self._ends_at else instant

    def _parent_is_gone(self) -> bool:
        if self._parent_gone:
            return True
        if self._link is not None:
            try:
                return bool(self._link.parent_gone())
            except Exception:  # noqa: BLE001 - a link that cannot answer is gone
                return True
        return False

    def _to_front(self) -> None:
        """Raise the window. It does not touch the form, so it is not one of the five helpers; a closed page is not an error."""
        try:
            self._page.bring_to_front()
        except Exception:  # noqa: BLE001 - nothing to raise
            pass

    def _student(self, step: str) -> None:
        """Tests only: play the student for one tick of a wait loop."""
        if self._student_hook is None:
            return
        try:
            self._student_hook(self._page, step)
        except Exception as exc:  # noqa: BLE001 - a hook that raises (a closed page) is part of the story, not the agent's failure
            self._hook_error = exc

    def _poll(self, milliseconds: int, step: str) -> bool:
        """One tick of a wait loop after hand-over: the heartbeat, a request to raise the window, the test's student, then a wait.

        False when the page or the driver is gone (the loop then decides with what it saw).
        """
        self._pulse()
        if self._link is not None:
            try:
                if self._link.front_requested():
                    self._to_front()
            except Exception:  # noqa: BLE001 - the link going away is the parent's story
                pass
        self._student(step)
        try:
            self._page.wait_for_timeout(milliseconds)
        except Exception:  # noqa: BLE001 - the page, the browser or the driver is gone
            return False
        return True

    def _end(self) -> str:
        return "stopped" if self.mode == "handoff" else ""

    def _between(self) -> None:
        """Between fields: a heartbeat, and the chance to stop.

        In a handoff this is also where a press of Submit during the fill, an upload the route refused, a window the student
        closed and a Stop end the run. The browser is closed first (``_on_stop``), then the result is returned.
        """
        self._beat()
        if self.mode == "handoff":
            if self._early:
                raise _Stop("needs_you", HANDOFF_EARLY, "early")
            if self._upload_refused:
                raise _Stop("needs_you", HANDOFF_S3, "upload")
            if self._closed():
                raise _Stop("failed", WINDOW_CLOSED, "closed")
        if self._cancel_requested():
            raise _Stop("failed", STOPPED, self._end())
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
        cancelled: Callable[[], bool] | None = None, link: HandoffLink | None = None, ends_at: float | None = None,
        check_file: Callable[[str, str, str], bool] | None = None,
    ) -> RunResult:
        """One run. Never raises. Before hand-over nothing could have left the page; after it the outcome says so.

        ``check_file(key, ref, sha256)`` asks the runner whether the cover letter the plan names is still the latest approved version
        with the same text (D11); without it, or on anything but True, no letter is attached.
        """
        self._plan, self._schema, self._files, self._lookup = plan, list(schema or []), dict(files or {}), lookup
        self._check_file = check_file
        self._hand_over = hand_over
        self._cancelled = cancelled or (lambda: False)
        self._link = link
        # The runner's cap: an argument, or an attribute of the link the child hands in. A monotonic instant; 0 means no cap.
        self._ends_at = float(ends_at or getattr(link, "ends_at", 0.0) or 0.0)
        self._page_url = page_url
        self._job_path = urlsplit(page_url).path.rstrip("/")
        self._step = "open"
        try:
            return self._run(page_url, replan)
        except _Stop as stop:
            return self._stopped(stop.outcome, stop.reason, stop.end)
        except Exception:  # noqa: BLE001 - never a message: it may quote a value
            return self._crashed()

    def _left_site_reason(self, outcome: str, reason: str) -> str:
        """A form whose page was sent elsewhere fails every later step in its own way; the cause is the one to give."""
        if outcome == "needs_you" and self._loaded and self._offsite_host():
            return OFFSITE.format(host=self._offsite_host())
        return reason

    def _stopped(self, outcome: str, reason: str, end: str = "") -> RunResult:
        if self.mode != "handoff":
            if outcome == "needs_you" and self._loaded:
                reason = self._left_site_reason(outcome, reason)
                self._screenshot("needs-you")
            return self._finish(outcome, [reason])
        if self._handed_over:
            return self._stopped_after_hand_over()
        # Nothing that could carry the application leaves from here on, and that is set before any call into the page: a call
        # lets route handlers run, and a Submit pressed during the picture must be aborted, never handed over (I3).
        self._closing = True
        if end:
            self._handoff_end = end
        if self._phase == PHASE_FILL:
            reason = self._left_site_reason(outcome, reason)
        # No picture once the window is the student's: it would be taken while they may be pressing Submit.
        if outcome == "needs_you" and self._loaded and self._page is not None and self._phase == PHASE_FILL and not self._closed():
            self._screenshot("needs-you")
        if self._handed_over:        # defence in depth: whatever ran during that call, a continued POST is never called "not sent"
            return self._stopped_after_hand_over()
        self._close_browser()
        return self._finish(outcome, [reason])

    def _stopped_after_hand_over(self) -> RunResult:
        """The student's Submit went on. Whatever stopped the run now, nothing can be said about what was sent."""
        self._closing = True
        self._close_browser()
        return self._finish("unconfirmed", [UNCONFIRMED_NOTE], handed_over=True, after_click=True)

    def _crashed(self) -> RunResult:
        if self.mode == "handoff":
            if self._handed_over:
                return self._stopped("unconfirmed", UNCONFIRMED_NOTE)
            if self._page is not None and self._closed():
                return self._stopped("failed", WINDOW_CLOSED, "closed")
            if self._loaded and self._phase == PHASE_FILL and self._offsite_host():
                return self._stopped("needs_you", OFFSITE.format(host=self._offsite_host()))
            if self._step == "fill":
                # A Playwright error on a field the app could not even clear: the form is in a state the app will not hand over.
                return self._stopped("needs_you", FIELD_TOOK.format(question=self._doing or "a field"))
            if self._step == "turn":
                return self._stopped("needs_you", HANDOFF_NOT_SUBMITTED, self._handoff_end or self._turn_gone())
            return self._stopped("failed", self._step_sentence())
        if self._page is not None and self._closed():
            return self._finish("failed", [WINDOW_GONE])
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

    def _turn_gone(self) -> str:
        """Why a wait in the student's turn failed: "closed" when the window or the browser is gone (the student's own act), else "crashed".

        A renderer that crashed leaves the page open (``is_closed()`` False) and the browser process alive, and every wait on it
        raises. That is not the student closing the window, so it must not be recorded as theirs: no notice would be written.
        """
        if self._closed():
            return "closed"
        try:
            if self._browser is None or not self._browser.is_connected():
                return "closed"        # the whole browser was quit, or the driver is gone
        except Exception:  # noqa: BLE001 - a browser that cannot answer is gone
            return "closed"
        return "crashed"

    def _close_browser(self) -> bool:
        """Close the window: the context, the browser, then the driver. True only when ``browser.close()`` returned.

        The parent still checks by process id before it says nothing was sent (I3): this is the agent's own, honest account.
        """
        clean = True
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001 - the browser may close it anyway
                pass
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001 - shutting down, and this says it did not finish
                clean = False
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001 - shutting down
                pass
        self._playwright = self._browser = self._context = None
        self._browser_closed = clean
        return clean

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
        submit_host, submit_path, confirmation_path = self.adapter.loader_paths(html)
        self._state.submit_path = submit_path
        self._confirmation_path = confirmation_path
        self._evidence_bits["loader"] = {"submit_path": bool(submit_path), "confirmation_path": bool(confirmation_path)}
        uploads = self.adapter.uploads_on_attach(frame)
        self._evidence_bits["uploads_on_attach"] = uploads
        if self.mode == "handoff":
            # Before any input. On a board whose submit address is not the one the request rules know, every Submit would be
            # stopped: safe, and useless.
            if not (submit_path and confirmation_path and submit_host == SUBMIT_HOST):
                raise _Stop("needs_you", HANDOFF_NO_LOADER)
            if uploads:
                raise _Stop("needs_you", HANDOFF_S3)
        else:
            if not (submit_path and confirmation_path):
                self._reasons.append(NO_LOADER)
            if uploads and self.mode == "rehearse":
                self._reasons.append(S3_NOTE)

        if self.mode == "lookup":
            return self._lookup_options(frame)
        return self._fill_form(frame, replan, uploads)

    def _check_stopped(self) -> None:
        if self._cancel_requested():
            raise _Stop("failed", STOPPED, self._end())

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

    # --- fill: a rehearsal stops at the picture, a handoff goes on to the student's turn -------------------------

    def _fill_form(self, frame: Any, replan: Callable[[list[dict[str, Any]], bool], Any] | None, uploads: bool) -> RunResult:
        handoff = self.mode == "handoff"
        # 4: inject, scan (structure only: no student value ever enters the page's JavaScript), snapshot.
        self._step = "read"
        self._progress("read")
        if not ENGINE_SOURCE:
            raise _Stop("failed", READ_FAILED)
        frame.evaluate(ENGINE_SOURCE)
        scan = self._scan(frame)
        initial = self._initial = self._snapshot(frame)
        self._check_stopped()

        # 5: the plan from what the page actually holds.
        self._step = "plan"
        self._plan_from(scan, replan, uploads)
        if handoff:
            self._prepare_handoff()
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
            if handoff:
                self._prepare_handoff()
            keys = self._scan_keys(again)
            self._fill_choices(frame, done)
            if self._scan_keys(self._scan(frame)) != keys:
                raise _Stop("needs_you", KEPT_CHANGING)
        self._fill_ticks(frame)
        self._fill_text(frame)
        self._check_left_site()
        if not handoff:
            self._deferred_checks(frame)

        # 8: read every filled field back through Playwright.
        self._step = "fill"
        self._read_back(frame)
        if handoff:
            self._between()

        # 9: the résumé.
        self._attach_files(frame)
        self._check_left_site()
        if handoff:
            self._between()

        # 10: a CAPTCHA checkbox is noted and never touched.
        widget = self.adapter.captcha_widget(frame)
        if widget:
            self._evidence_bits["captcha_widget"] = True
            if handoff:
                self._captcha_left = [{"key": "captcha", "question": "CAPTCHA", "reason": LEFT_CAPTCHA}]
            else:
                self._reasons.append(CAPTCHA_NOTE)

        # 11: the independent check, as a submit would run it.
        self._step = "check"
        self._progress("check")
        self._check_stopped()
        seen = frame.evaluate(REQUIRED_CHECK_SCRIPT)
        problems = check_required(
            seen.get("items", []), self._check_view(), self._schema, initial, controls=seen.get("controls", []), invalid=seen.get("invalid", []),
        )
        if handoff:
            self._resolve_check(frame, problems)
        else:
            self._check_problems.extend(problem_dict(problem) for problem in problems)
        self._page_defaults = self._defaults(seen, initial)
        if handoff:
            self._between()

        # 12: a picture, and a rehearsal ends here.
        self._progress("picture")
        self._screenshot("filled")
        if not handoff:
            return self._finish("rehearsed")
        self._between()
        return self._student_turn()

    def _check_view(self) -> Any:
        """The plan as the check should read it: the agent's own changes included (a field it left for the student is skipped)."""
        return _View(self._fields(), str(_attr(self._plan, "plan_hash") or ""))

    # --- handoff: what the agent changes about the plan, and what it does with a field that does not take --------

    def _prepare_handoff(self) -> None:
        """Before the first input: stop on a hidden field the app would have filled, and fix up the plan's dispositions."""
        raw = {str(_attr(entry, "key")): entry for entry in self._raw_fields()}
        for problem in self._join_problems:
            entry = raw.get(problem["key"])
            if problem["kind"] == "hidden_control" and entry is not None and _source_kind(entry) != "none":
                raise _Stop("needs_you", HANDOFF_HIDDEN.format(question=problem["question"] or self._question(problem["key"])))
        for key, entry in raw.items():
            if key in self._overrides:
                continue
            question = str(_attr(entry, "question") or key)
            disposition, source_kind = _attr(entry, "disposition"), _source_kind(entry)
            if disposition == "deferred":
                self._leave(key, "left_for_you", str(_attr(entry, "problem") or _attr(entry, "note") or LEFT_FIELD.format(question=question)))
        for problem in self._join_problems:
            if problem["key"] not in raw and problem["kind"] != "hidden_control":
                self._add_left(problem["key"], problem["question"] or problem["key"], LEFT_FIELD.format(question=problem["question"] or "a field"))

    def _add_left(self, key: str, question: str, reason: str) -> None:
        if not any(item["key"] == key for item in self._extra_left):
            self._extra_left.append({"key": key, "question": question, "reason": reason})

    def _permitted(self, entry: Any) -> bool:
        """Handoff: a sensitive field is typed only from the student's own stored answer (D5 C), a consent box only from a statement."""
        if self.mode != "handoff":
            return True
        if _attr(entry, "sensitive") and _source_kind(entry) != "sensitive":
            key = str(_attr(entry, "key"))
            self._leave(key, "left_for_you", LEFT_FIELD.format(question=str(_attr(entry, "question") or key)))
            return False
        return True

    def _took_not(self, question: str) -> Exception:
        """What a field that did not take the answer raises: a stop in a rehearsal, a field for the student in a handoff."""
        if self.mode == "handoff":
            return _FieldDidNotTake(question)
        return _Stop("needs_you", FIELD_TOOK.format(question=question))

    def _do(self, frame: Any, entry: Any, action: Callable[[], None]) -> bool:
        """Run one field's action. In a handoff a field that fails is cleared and left for the student; True when it took."""
        if self.mode != "handoff":
            action()
            return True
        try:
            action()
            return True
        except _Stop:
            raise
        except Exception:  # noqa: BLE001 - a field that did not take; a closed page is not one
            if self._closed():
                raise
        self._leave_field(frame, entry)
        return False

    def _leave_field(self, frame: Any, entry: Any, reason: str = "") -> None:
        """Take what the agent put in a field out again, and leave the field for the student.

        Only through the helpers: an empty ``fill`` for text, an untick for a box the agent ticked, an empty file list. A chosen
        option cannot be taken back without pressing keys, so the run stops (the form holds an answer nobody confirmed).
        """
        key = str(_attr(entry, "key"))
        question = str(_attr(entry, "question") or key)
        kind = self.adapter.control_kind(frame, key)
        control = self.adapter.control(frame, key)
        cleared = False
        if kind in ("text", "textarea"):
            self._type(control.first, "", key)
            cleared = control.first.input_value() == ""
        elif kind == "checkbox":
            if control.first.is_checked():
                self._tick(control.first, key, False)
            cleared = not control.first.is_checked()
        elif kind == "radio":
            cleared = not any(choice["checked"] for choice in self.adapter.choices(frame, key))
        elif kind == "react_select":
            if not self.adapter.react_values(self.adapter.field_container(frame, key)):
                self._type(control.first, "", key, search=True)   # the search text, which is not an answer
                self._release()
                cleared = True
        elif kind == "select":
            cleared = _normalize(control.first.evaluate(_NATIVE_VALUE)) == _normalize(self._initial.get(key) or "")
        elif kind == "file":
            self._attach(control.first, None, key)
            cleared = int(control.first.evaluate(_FILE_STATE).get("count") or 0) == 0
        if not cleared:
            raise _Stop("needs_you", FIELD_TOOK.format(question=question))
        self._leave(key, "left_for_you", reason or LEFT_FIELD.format(question=question))

    def _resolve_check(self, frame: Any, problems: Sequence[Problem]) -> None:
        """Handoff: what the independent check found. A field the agent filled and the page does not hold goes back to the student."""
        for problem in problems:
            self._check_problems.append(problem_dict(problem))
            key = problem.key
            if not key:
                continue
            entry = self._entry(key)
            question = problem.question or (str(_attr(entry, "question") or key) if entry is not None else key)
            if entry is not None and _attr(entry, "disposition") == "fill":
                self._leave_field(frame, entry)
            elif problem.kind == "unplanned_value":
                self._add_left(key, question, LEFT_UNPLANNED.format(question=question))
            elif entry is None or _attr(entry, "disposition") not in ("left_for_you", "blank"):
                self._add_left(key, question, LEFT_FIELD.format(question=question))

    def _defaults(self, seen: Mapping[str, Any], initial: Mapping[str, Any]) -> list[str]:
        """Optional controls that still hold what the page itself put there, which the plan did not set (6.10, item 4)."""
        filled = {str(_attr(entry, "key")) for entry in self._fields() if _attr(entry, "disposition") == "fill"}
        groups: dict[str, dict[str, Any]] = {}
        for control in seen.get("controls", []):
            key = str(control.get("key") or "")
            kind = str(control.get("kind") or "")
            if not key or control.get("mirror") or kind in ("file", "hidden"):
                continue
            slot = groups.setdefault(key, {"required": False, "choice": kind in ("radio", "checkbox"), "held": []})
            slot["required"] = bool(slot["required"] or control.get("required"))
            if slot["choice"]:
                if control.get("checked"):
                    slot["held"].extend(_texts(control.get("value_text")))
            else:
                slot["held"] = _texts(control.get("value_text"))
        found = []
        for key, slot in groups.items():
            entry = self._entry(key)
            if key in filled or slot["required"] or (entry is not None and _attr(entry, "required")) or not slot["held"]:
                continue
            if key in initial and sorted(_normalize(text) for text in _texts(initial[key])) == sorted(_normalize(text) for text in slot["held"]):
                found.append(key)
        return found

    # --- fill steps --------------------------------------------------------------------------------------------------

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
            if key in done or not self._permitted(entry):
                continue
            kind, control = self._control_of(frame, entry)
            if kind not in ("react_select", "select"):
                continue
            self._between()
            value = _attr(entry, "value")

            def choose(control=control, key=key, value=value) -> None:
                for label in (value if isinstance(value, (list, tuple)) else [value]):
                    self._choose(control.first, str(label), key)

            if self._do(frame, entry, choose):
                done.add(key)

    def _fill_ticks(self, frame: Any) -> None:
        for entry in self._fill_entries():
            key = str(_attr(entry, "key"))
            if not self._permitted(entry):
                continue
            kind, control = self._control_of(frame, entry)
            if kind not in ("radio", "checkbox"):
                continue
            self._between()
            value = _attr(entry, "value")

            def tick(control=control, key=key, value=value, question=self._doing) -> None:
                if isinstance(value, bool):
                    if control.first.is_checked() != value:
                        self._tick(control.first, key, value)
                    return
                for label in (value if isinstance(value, (list, tuple)) else [value]):
                    found = [index for index, choice in enumerate(self.adapter.choices(frame, key)) if _normalize(choice["label"]) == _normalize(label)]
                    if len(found) != 1:
                        raise self._took_not(question)
                    self._tick(control.nth(found[0]), key, True)

            self._do(frame, entry, tick)

    def _fill_text(self, frame: Any) -> None:
        for entry in self._fill_entries():
            key = str(_attr(entry, "key"))
            if not self._permitted(entry):
                continue
            kind, control = self._control_of(frame, entry)
            if kind not in ("text", "textarea"):
                continue
            self._between()
            self._do(frame, entry, lambda control=control, key=key, entry=entry: self._type(control.first, str(_attr(entry, "value")), key))

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
                if self.mode == "handoff":
                    self._leave_field(frame, entry)   # cleared and left for the student, or the run stops if it cannot be cleared
                    continue
                raise _Stop("needs_you", FIELD_TOOK.format(question=self._doing))
            self._done.add(key)
            if self.mode == "handoff":
                self._filled.add(key)        # typed and read back: only now is it described as filled
                self._between()

    def _attach_files(self, frame: Any) -> None:
        handoff = self.mode == "handoff"
        for entry in self._fields():
            source = _attr(entry, "source")
            source_kind = _attr(source, "kind") if source is not None else ""
            if _attr(entry, "disposition") != "fill" or source_kind not in ("resume", "cover_letter"):
                continue
            key = str(_attr(entry, "key"))
            question = str(_attr(entry, "question") or key)
            letter = source_kind == "cover_letter"
            self._doing = question
            payload = self._files.get("cover_letter" if letter else "resume")
            if payload is None:
                # A missing file is a gap, not a stop.
                if handoff:
                    self._leave(key, "left_for_you", NO_FILE.format(question=question))
                    continue
                self._check_problems.append(problem_dict(Problem("file", key, NO_FILE.format(question=question), question, bool(_attr(entry, "required")))))
                continue
            self._between()
            if not self._file_matches(entry, payload, letter):
                # Nothing is attached; the student can attach their own.
                if handoff:
                    self._leave(key, "left_for_you", LEFT_COVER_LETTER_CHANGED if letter else FILE_CHANGED)
                    continue
                if letter:
                    self._left_letter(key, question, entry)
                    continue
                raise _Stop("needs_you", FILE_CHANGED)
            if letter and not self._letter_current(key, entry):
                # Asked again just before the file goes in: the letter was edited, replaced by a newer draft or unapproved since the run started.
                if handoff:
                    self._leave(key, "left_for_you", LEFT_COVER_LETTER_CHANGED)
                else:
                    self._left_letter(key, question, entry)
                continue
            control = self.adapter.control(frame, key)
            if not control.count():
                raise _Stop("needs_you", NO_CONTROL.format(question=question))
            accept = str(control.first.evaluate(_FILE_STATE).get("accept") or "")
            if accept and not self._accepts(accept, payload):
                if handoff:
                    self._leave(key, "left_for_you", FILE_TYPE.format(question=question))
                    continue
                raise _Stop("needs_you", FILE_TYPE.format(question=question))
            self._attach(control.first, payload, key)
            state = control.first.evaluate(_FILE_STATE)
            group_text = self.adapter.field_container(frame, key).inner_text(timeout=ACTION_TIMEOUT_MS)
            if not (state["count"] == 1 and state["name"] == payload.name and state["size"] == len(payload.buffer) and payload.name in group_text):
                if handoff:
                    self._leave_field(frame, entry)   # clears the input
                    continue
                raise _Stop("needs_you", FIELD_TOOK.format(question=question))
            self._done.add(key)
            self._filled.add(key)

    @staticmethod
    def _file_matches(entry: Any, payload: FilePayload, letter: bool) -> bool:
        """The bytes are the ones the plan was made from. A résumé: they hash to the plan's hash. A letter: they hash to what the runner
        stored for them, the text they were rendered from is the text the plan names, and the file's name is the one the plan shows."""
        digest = hashlib.sha256(payload.buffer).hexdigest()
        if letter:
            return (
                bool(payload.sha256) and digest == payload.sha256 and bool(payload.content_sha256) and payload.content_sha256 == _attr(entry, "file_sha256")
                and bool(payload.name) and payload.name == _attr(entry, "file_name")
            )
        return digest == _attr(entry, "file_sha256")

    def _letter_current(self, key: str, entry: Any) -> bool:
        """Ask the runner, once more, whether the letter is still the latest approved version with this text. No answer is a no."""
        if self._check_file is None:
            return False
        source = _attr(entry, "source")
        try:
            return self._check_file(key, str(_attr(source, "ref") or ""), str(_attr(entry, "file_sha256") or "")) is True
        except Exception:  # noqa: BLE001 - never its message
            return False

    def _left_letter(self, key: str, question: str, entry: Any) -> None:
        """A rehearsal could not attach the letter: a problem, said once, and the rehearsal goes on."""
        self._check_problems.append(problem_dict(Problem("file", key, LETTER_CHANGED.format(question=question), question, bool(_attr(entry, "required")))))

    @staticmethod
    def _accepts(accept: str, payload: FilePayload) -> bool:
        suffix = Path(payload.name).suffix.lower()
        for token in (part.strip().lower() for part in accept.split(",") if part.strip()):
            if token.startswith(".") and token == suffix:
                return True
            if "/" in token and (token == payload.mime_type.lower() or (token.endswith("/*") and payload.mime_type.lower().startswith(token[:-1]))):
                return True
        return False

    # --- the student's turn (handoff) ------------------------------------------------------------------------------

    def _left_items(self) -> list[dict[str, str]]:
        """What the student has to do in the window: plan entries left for them, anything else the check found, the CAPTCHA last."""
        items: list[dict[str, str]] = []
        for entry in self._plan_entries()[0]:
            if entry["disposition"] != "left_for_you":
                continue
            question = str(entry.get("question") or entry["key"])
            reason = str(entry.get("problem") or entry.get("note") or LEFT_FIELD.format(question=question))
            items.append({"key": str(entry["key"]), "question": question, "reason": reason})
        listed = {item["key"] for item in items}
        items.extend(item for item in self._extra_left if item["key"] not in listed)
        items.extend(self._captcha_left)
        return items

    def _student_turn(self) -> RunResult:
        """The form is filled and the window is the student's. Nothing here presses Submit: only the student's press can send."""
        self._step = "turn"
        entries, plan_hash = self._plan_entries()
        shot = next((item for item in reversed(self._screenshots) if item["step"] == "filled"), None)
        self._observer = _Observer(self._page, lambda: self._handed_over, lambda: self._state.values, lambda: self._state.submit_path)
        self._observer.start()
        # One last look before the turn: a press in the gap after the check is caught here, with the observer already listening.
        self._between()
        t = self.timeouts
        until = time.monotonic() + t.handoff_s
        if self._ends_at:
            # A slow fill shortens the turn, never the time left for what comes after the press.
            until = min(until, self._ends_at - t.after_hand_over_s - 3 * t.outcome_s)
        if self._link is not None:
            try:
                self._link.ready({
                    "op": OP_HANDOFF_READY, "plan": entries, "plan_hash": plan_hash, "left": self._left_items(), "screenshot": shot,
                    "captcha_widget": bool(self._evidence_bits.get("captcha_widget")), "page_defaults": list(self._page_defaults),
                    # How long the window stays the student's, as the agent will really keep it: the parent shows the closing time.
                    "handoff_in_s": max(0.0, until - time.monotonic()),
                })
            except Exception:  # noqa: BLE001 - the turn still runs; the parent then shows no list
                pass
        self._phase = PHASE_STUDENT
        self._progress("your_turn")
        self._to_front()
        while True:
            if self._handed_over:
                return self._outcome()
            end = ""
            if self._closing:
                end = self._why_closing
            elif self._early:
                end = "early"             # a press in the gap between the last look and the turn: caught here, still nothing sent
            elif self._cancel_requested():
                end = "stopped"
            elif self._closed():
                end = "closed"
            elif time.monotonic() >= until:
                end = "timeout"
            elif self._poll(TURN_POLL_MS, "handoff"):
                continue
            elif self._handed_over:
                continue
            else:
                end = self._turn_gone()     # the page or the driver is gone: the parent verifies by process id
            break
        self._closing = True          # every route from now on is aborted
        self._handoff_end = end
        self._close_browser()         # FIRST (I3): the result is returned only after the window is gone
        sentence = {
            "crashed": HANDOFF_CRASHED, "refused": HANDOFF_UNRECORDED, "elsewhere": HANDOFF_ELSEWHERE, "upload": HANDOFF_UPLOAD, "early": HANDOFF_EARLY,
        }.get(end, HANDOFF_NOT_SUBMITTED)
        return self._finish("needs_you", [sentence])

    # --- after the press: the outcome, and the security code ----------------------------------------------------------

    def _decide(self, obs: Observation, *, code_wait_over: bool) -> Outcome:
        out = decide_outcome(obs, code_wait_over=code_wait_over)
        if self._submit_continued and out.outcome == "failed" and not out.after_click:
            # The POST was continued, whatever the tracker shows: "nothing was sent" is not a thing this run can say.
            return Outcome("unconfirmed", 1, UNCONFIRMED_NOTE, evidence=out.evidence, settled=False)
        return out

    def _outcome(self) -> RunResult:
        t = self.timeouts
        self._step = "outcome"
        self._progress("submitting")
        start = time.monotonic()
        window = self._cap(start + t.outcome_s)
        after_until = self._cap(start + t.after_hand_over_s)    # ONE budget for the code rounds and the challenge
        code_round, challenge_waited = 0, False
        while True:
            alive = self._poll(OUTCOME_POLL_MS, "outcome")
            now = time.monotonic()
            obs = self._observation()
            out = self._decide(obs, code_wait_over=code_round >= 2 or now >= after_until)
            self._outcome_evidence = dict(out.evidence)
            if alive and out.outcome == "waiting" and code_round < 2 and now < after_until:
                # A prompt is counted from evidence: the first 428, then a new 428 (or the boxes again) after the code POST was
                # answered. The boxes stay on the page while the code POST is on its way, which is not a second prompt: it would
                # open a second code POST nobody asked for and tell the student to press Submit again.
                if code_round == 0 or new_code_prompt(obs):
                    code_round += 1
                    self._security_code(reader=(code_round == 1), until=after_until)
                    window = self._cap(time.monotonic() + t.outcome_s)
                    continue
                if now < window:
                    continue          # the code POST has no answer yet, or its answer is on its way: wait for it
            if alive and out.outcome == "needs_you" and obs.challenge_frame and not challenge_waited and now < after_until:
                challenge_waited = True
                self._challenge = True
                self._progress("challenge")
                self._to_front()
                self._wait_challenge(after_until)
                window = self._cap(time.monotonic() + t.outcome_s)
                continue
            if not alive or out.settled or now >= window:
                break
        # Nothing that could carry anything leaves from here on, and that is set before any call into the page (the picture and
        # the close are several round trips, and route handlers run during each): a press of Submit made now is aborted, never
        # continued under an outcome that was decided before it. Then the table is applied once more to what the observer holds,
        # so a request that passed between the last look and this line is part of the answer.
        self._closing = True
        if not self._closed():
            obs = self._observation()
        out = self._decide(obs, code_wait_over=True)
        self._outcome_evidence = dict(out.evidence)
        if not self._closed():
            self._screenshot("final")
        self._close_browser()
        return self._finish(
            out.outcome, [out.note] if out.note else [], handed_over=True, after_click=bool(out.after_click),
            confirmation_seen=(out.outcome == "submitted" and out.resolved_by == "page"),
        )

    def _wait_challenge(self, until: float) -> None:
        while time.monotonic() < until:
            if not self._poll(OUTCOME_POLL_MS, "outcome") or not self._challenge_frame():
                return

    def _observation(self) -> Observation:
        """What decide_outcome reads, gathered here in the agent's own loop (never inside an event handler)."""
        path, query, form_present = self._last_seen
        code_visible = challenge = False
        frame = None
        try:
            page = self._page
            parts = urlsplit(page.url)
            frame = self.adapter.form_frame(page)
            present = frame.locator("form#application-form").count() > 0
            code_visible = self.adapter.security_code_prompt(frame)
            challenge = self._challenge_frame()
            path, query, form_present = parts.path, parts.query, present
            self._last_seen = (path, query, form_present)
        except Exception:  # noqa: BLE001 - a page in the middle of navigating or closing: the last thing seen stands
            frame = None
        seen = self._observer.seen() if self._observer is not None else ()
        error = ""
        if frame is not None and form_present and any(item.status is not None and 400 <= item.status < 500 and item.status != 428 for item in seen):
            error = self._first_error_question(frame)
        return Observation(
            main_path=path, main_query=query, form_present=form_present, requests=seen, security_code_visible=code_visible,
            challenge_frame=challenge, submit_path=self._state.submit_path, confirmation_path=self._confirmation_path,
            board_token=board_token(self._page_url), job_id=_job_id(self._page_url),
            navigated=bool(self._observer is not None and self._observer.navigated), first_field_error=error,
        )

    def _first_error_question(self, frame: Any) -> str:
        """The QUESTION of the first field the form marks as wrong. Never the page's error text: it may quote what the student typed."""
        try:
            seen = frame.evaluate(REQUIRED_CHECK_SCRIPT)
            for item in seen.get("invalid", []):
                key = str(item.get("key") or "")
                entry = self._entry(key) if key else None
                question = str(_attr(entry, "question") or "") if entry is not None else str(item.get("question") or "")
                if question:
                    return question[:200]
        except Exception:  # noqa: BLE001 - no question is named
            pass
        return ""

    def _challenge_frame(self) -> bool:
        """A visible challenge frame: reCAPTCHA's ``bframe`` or an hCaptcha challenge. An invisible one that is only loaded is not."""
        try:
            for frame in self._page.frames:
                url = frame.url or ""
                if "bframe" in url or ("hcaptcha.com" in url and "challenge" in url):
                    if frame.frame_element().is_visible():
                        return True
        except Exception:  # noqa: BLE001 - a frame that went away is not a challenge
            return False
        return False

    def _switch_to_student(self, until: float) -> float:
        """The code is the student's to type: say so, bring the window forward, and give them the rest of the budget."""
        self._evidence_code["fallback"] = True
        self._progress("code_yours")
        self._to_front()
        return until

    def _security_code(self, *, reader: bool, until: float) -> None:
        """One security-code prompt. The reader's code is typed (never submitted); the student presses Submit in every case.

        ``reader`` False (a second prompt): the student only. The code is a local that is never kept, shown, logged or sent on.
        An ask the agent stops waiting for is abandoned on the link, so a reply that comes later is dropped there (I7).
        """
        t, ev, link = self.timeouts, self._evidence_code, self._link
        self._state.note_security_code_prompt()          # the route allows exactly one code POST for this prompt
        self._progress("security_code")
        ev["prompted"] = True
        ev["rounds"] += 1
        posted_before = self._state.code_posts_passed
        use_reader = bool(reader and link is not None and not self._parent_is_gone() and not self._code_typed_once)
        pending: int | None = None
        next_ask: float | None = None
        read_until = self._cap(min(time.monotonic() + t.code_read_s, until))
        student_until: float | None = None

        def drop() -> None:
            """The ask in flight, if any, is no longer waited for."""
            nonlocal pending
            if pending is not None:
                pending = None
                self._abandon_code()

        if use_reader:
            pending = link.ask_code()
        else:
            student_until = self._switch_to_student(until)
        try:
            while True:
                if not self._poll(TURN_POLL_MS, "security_code"):
                    return
                if self._state.code_posts_passed > posted_before or self._closed() or self._confirmation_now():
                    return                              # the student pressed Submit with a code, the page moved on, or it is gone
                if student_until is None and ev["auto_submit_blocked"]:
                    drop()
                    student_until = self._switch_to_student(until)   # the widget tried to send by itself
                if student_until is None:
                    if self._parent_is_gone():
                        drop()
                        student_until = self._switch_to_student(until)
                    else:
                        reply = None
                        if pending is not None:
                            try:
                                reply = link.code_reply(pending)    # the same id stays pending until it answers
                            except Exception:  # noqa: BLE001 - a link that cannot answer is the student's turn
                                reply = {"status": "fallback"}
                        status = reply.get("status") if reply else None
                        if status == "found":
                            typed, why = self._type_security_code(str(reply.get("code") or ""))
                            reply = None                           # the code is not kept
                            try:
                                link.code_result(pending, typed, why)
                            except Exception:  # noqa: BLE001 - the parent then does not record "typed"
                                pass
                            pending = None                         # answered: the link holds nothing for it any more
                            ev["typed"] = typed
                            if typed:
                                # The page's own script, if it sends the code by itself, does so now; the guard on submit POSTs runs a
                                # little longer. Only after it is the student told to press Submit, so that press is never refused.
                                self._wait(max(CODE_SETTLE_S, self._state.code_typing_until - time.monotonic()))
                            if typed:
                                if ev["auto_submit_blocked"]:
                                    ev["reason"] = "auto_submit_blocked"   # the code is in the boxes; the page's own send was stopped
                                self._progress("code_typed")
                            else:
                                ev["reason"] = why or "auto_submit_blocked"
                                ev["fallback"] = True
                                self._progress("code_yours")
                            self._to_front()
                            student_until = until
                        elif status == "fallback":
                            pending = None                         # the reply to it was this one
                            student_until = self._switch_to_student(until)
                        elif reply:                                 # waiting
                            pending, next_ask = None, time.monotonic() + t.code_poll_s
                        if pending is None and student_until is None and next_ask is not None and time.monotonic() >= next_ask:
                            pending, next_ask = link.ask_code(), None
                        if student_until is None and time.monotonic() >= read_until:
                            drop()
                            student_until = self._switch_to_student(until)   # a reply that comes later is dropped
                elif time.monotonic() >= student_until:
                    return
        finally:
            drop()

    def _abandon_code(self) -> None:
        """Tell the link the agent no longer waits for its security-code ask, so nothing that arrives for it is kept."""
        try:
            if self._link is not None:
                self._link.abandon_code()
        except Exception:  # noqa: BLE001 - a link that cannot be told holds nothing the agent reads again
            pass

    def _confirmation_now(self) -> bool:
        """The main frame is on Greenhouse's confirmation page (the code, if it was needed, has been accepted)."""
        try:
            parts = urlsplit(self._page.url)
            return confirmation_reached(Observation(
                main_path=parts.path, main_query=parts.query, confirmation_path=self._confirmation_path,
                board_token=board_token(self._page_url), job_id=_job_id(self._page_url),
            ))
        except Exception:  # noqa: BLE001 - a page that cannot answer is not on the confirmation page
            return False

    def _type_security_code(self, code: str) -> tuple[bool, str]:
        """Put the code in the page's eight boxes, one ``fill`` per box. (typed, why not). At most once per run."""
        if self._code_typed_once:
            return False, "already_typed"
        if not CODE_PATTERN.fullmatch(code):
            return False, "bad_code"                     # email text is data from outside: only eight letters or digits are typed
        try:
            parts = urlsplit(self._page.url)
            if (parts.hostname or "").lower() not in BOARD_HOSTS or not parts.path.rstrip("/").startswith(self._job_path):
                return False, "page_closed"
            boxes = self.adapter.security_code_inputs(self.adapter.form_frame(self._page))   # now, in the current frame
        except Exception:  # noqa: BLE001 - a page that cannot answer
            return False, "page_closed"
        if boxes is None:
            return False, "inputs_missing"
        try:
            if any(box.input_value() for box in boxes):
                return False, "inputs_not_empty"
        except Exception:  # noqa: BLE001
            return False, "inputs_missing"
        self._state.code_typing_until = math.inf
        self._code_auto_until = math.inf
        self._code_typed_once = True
        held = False
        try:
            for box, character in zip(boxes, code):
                self._type(box, character, "security_code")
            # Typed and read back, like every other field of a handoff: only now is the code described as typed. A widget that clears
            # or moves what it was given leaves the boxes empty or garbled, and the student must then be told to type it themselves.
            held = "".join(str(box.input_value()) for box in boxes).casefold() == code.casefold()
        except Exception:  # noqa: BLE001 - a box that would not take its character is the student's to finish, never a crash of the run
            held = False
        finally:
            self._state.code_typing_until = time.monotonic() + CODE_GUARD_S
            self._code_auto_until = time.monotonic() + CODE_SETTLE_S
        return (True, "") if held else (False, "typing_failed")

    # --- the picture and the result ------------------------------------------------------------------------------

    def _screenshot(self, step: str) -> None:
        """A full-page picture with every sensitive field's container covered. A failure here changes nothing."""
        if self.screenshot_dir is None or self._page is None:
            return
        try:
            frame = self._frame or self._page.main_frame
            keys: list[str] = []
            masks: list[Any] = []
            for entry in self._raw_fields():
                # A Finish in browser run leaves the questions it never answers for the student (criminal history, pay, personal topics
                # the broad net caught) for the student to type in the window, so they are covered like a precisely sensitive field.
                never = self.mode == "handoff" and (bool(_attr(entry, "net_never", ())) or _attr(entry, "problem_kind", "") == "sensitive_never")
                if not (_attr(entry, "sensitive") or never):
                    continue
                key = str(_attr(entry, "key"))
                if self.adapter.control(frame, key).count():
                    masks.append(self.adapter.field_container(frame, key))
                    keys.append(key)
            if self.mode == "handoff":
                # The emailed code, typed by the app or by the student, sits in these boxes until the page moves on: never in a picture.
                try:
                    masks.append(self.adapter.form_frame(self._page).locator('[id^="security-input-"]'))
                except Exception:  # noqa: BLE001 - a page that cannot answer has no boxes to cover
                    pass
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
        evidence = {
            **self._evidence_bits, "lookups": lookups, "submit_path_hit": self._submit_path_hit, "refused_total": self._refused_total,
            "filled_keys": sorted(self._done), "checked_keys": sorted(self._checked),
            "deferred_failed_keys": sorted(self._deferred_failed),
        }
        if self.mode == "lookup":
            return evidence
        evidence["page_defaults"] = list(self._page_defaults)
        if self.mode != "handoff":
            return evidence
        outcome = self._outcome_evidence
        evidence.update({
            "handoff_end": "posted" if self._handed_over else self._handoff_end,
            "left_for_you": self._left_items(),
            "submit_post": bool(outcome.get("submit_post", False)),
            "submit_continued": self._submit_continued,
            "submit_status": outcome.get("submit_status"),
            "confirmation_path": str(outcome.get("confirmation_path") or ""),
            "form_absent": bool(outcome.get("form_absent", False)),
            "upload_refused": dict(self._upload_refused) if self._upload_refused else None,
            "security_code": {**self._evidence_code, "posted": self._state.code_posts_passed > 0},
            "challenge": self._challenge,
            "browser_closed": self._browser_closed,
            "parent_gone": self._parent_is_gone(),
        })
        return evidence

    def _plan_entries(self) -> tuple[list[dict[str, Any]], str]:
        """The value-free entries of the plan (``policy.plan_entries``) with the agent's own changes applied, and its hash. Never raises."""
        if self.mode == "lookup" or not self._raw_fields():
            return [], ""   # a lookup plans nothing
        try:
            from .policy import plan_entries   # lazy: the child imports the policy only when it has a plan to describe

            entries = plan_entries(self._plan)
        except Exception:  # noqa: BLE001 - a plan that is not a policy.Plan (a test's stand-in) is described by name
            entries = []
            for item in self._raw_fields():
                source = _attr(item, "source") or {}
                entries.append({
                    "key": _attr(item, "key"), "question": _attr(item, "question"), "control": _attr(item, "control"),
                    "required": bool(_attr(item, "required")), "options": list(_attr(item, "options") or ()), "sensitive": _attr(item, "sensitive"),
                    "disposition": _attr(item, "disposition"),
                    "source": {"kind": _attr(source, "kind", "none"), "ref": _attr(source, "ref", ""), "company": "", "reusable": False, "links": []},
                    "value_mac": _attr(item, "value_mac", "") or "", "file_sha256": _attr(item, "file_sha256", "") or "",
                    "problem": _attr(item, "problem", "") or "",
                })
        for entry in entries:
            changed = self._overrides.get(str(entry["key"]))
            if changed is not None:
                entry["disposition"] = changed[0]
                if changed[0] == "left_for_you":
                    entry["problem"] = changed[1]
                else:
                    entry["note"] = changed[1]
            elif self.mode == "handoff" and entry.get("disposition") == "fill" and str(entry["key"]) not in self._filled:
                # The draft plan says what the app would fill. A field the run never reached, or never read back, was not filled:
                # it is never described as filled, whatever stopped the run.
                entry["disposition"] = "blank"
                entry["note"] = TYPED_NOT_CHECKED if str(entry["key"]) in self._typed else NOT_FILLED
        return entries, str(_attr(self._plan, "plan_hash") or "")

    def _finish(
        self, outcome: str, first: Sequence[str] = (), *, handed_over: bool | None = None, after_click: bool | None = None,
        confirmation_seen: bool = False,
    ) -> RunResult:
        reasons: list[str] = []
        for sentence in [*first, *self._reasons]:
            if sentence and sentence not in reasons:
                reasons.append(sentence)
        entries, plan_hash = self._plan_entries()
        handed = self._handed_over if handed_over is None else handed_over
        clicked = handed if after_click is None else after_click
        return RunResult(
            outcome=outcome, reasons=reasons, plan=entries, plan_hash=plan_hash, join_problems=list(self._join_problems),
            check_problems=list(self._check_problems), options=dict(self._options), screenshots=list(self._screenshots),
            refused=list(self._refused), requests=self._observer.records() if self._observer is not None else [],
            evidence=self._evidence(), handed_over=handed, after_click=clicked, confirmation_seen=confirmation_seen,
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
