"""The integrity-critical decisions of the apply agent, as pure functions.

Standard library only, with no browser and no database, so the default
unittest suite (which has no Playwright) covers every decision the agent
makes about whether something may leave the page, what happened after Submit,
and whether a filled form is what the student was shown. Browser tests then
only have to prove that the observations these functions read are gathered
correctly.

    route_decision   whether one browser request may go out, in each mode and phase
    decide_outcome   what happened after hand-over, from what the browser saw

The first two are written once and read each ATS's ``RoutePolicy`` (its hosts, its endpoints, what counts as its submit POST
and its confirmation page); Greenhouse's is ``GREENHOUSE_ROUTE_POLICY``, built from the constants below and registered by
``apply.ats``.
    join             whether Greenhouse's own field listing and the page agree
    check_required   the independent pre-submit check of the filled form
    clean_rehearsal  whether a rehearsal counts toward the one-click gate

Nothing here reads page text to choose an action. A page's wording is never
evidence that an application was received: only a submit POST that Greenhouse
answered, followed by its own confirmation path with the form gone, is.

The inputs are plain data. Plans, schema fields, scan fields and runs may be
dicts or objects; they are read by name (``_get``), so apply_policy and
apply_runs keep their own types.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence
from urllib.parse import parse_qs, quote, quote_plus, unquote, unquote_plus, urlsplit
from .greenhouse import BOARD_HOSTS, DISPLAY_NAME, GREENHOUSE_DOMAIN, SUBMIT_HOST
from . import lever

# ---------------------------------------------------------------------------------------------
# Hosts and endpoints
# ---------------------------------------------------------------------------------------------

# The hosts a form of the job board could post an application to. During the student's turn an aborted non-GET to one of them is
# the form's own submission going somewhere the app did not agree to (student_submit_elsewhere); telemetry is not.
FORM_POST_HOSTS = frozenset({"job-boards.greenhouse.io", "boards.greenhouse.io", "boards-api.greenhouse.io"})
# Greenhouse's page posts telemetry (Snowplow) here from a form page, on every keystroke or so (live recon, M5a). It is refused for
# every method in submit and handoff, silently, and never ends a run. Pinned from the recon; extended only from recon evidence.
TELEMETRY_HOSTS = frozenset({"c.spl.greenhouse.io"})
STATIC_RESOURCE_TYPES = frozenset({"image", "font", "stylesheet", "script", "media"})
# The hosts Greenhouse serves its own static files from (confirmed 2026-10-03, pinned in tests/fixtures/apply/greenhouse/endpoints.json):
# the board's scripts, styles and fonts, and the logos and banners (recruiting.cdn and its numbered shards). Not every host under
# greenhouse.io: its analytics collector is one, and an image request to it can carry an event.
STATIC_ASSET_HOSTS = ("job-boards.cdn.greenhouse.io", "recruiting.cdn.greenhouse.io")
STATIC_ASSET_SHARD_PATTERN = r"s\d{1,3}-recruiting\.cdn\.greenhouse\.io"
_STATIC_ASSET_SHARD = re.compile(STATIC_ASSET_SHARD_PATTERN)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# A planned value shorter than this ("Yes", "No") is not searched for in a
# request: it would match almost anything.
MIN_GUARDED_VALUE = 4

# Boards that upload a file the moment it is attached have not been seen live,
# so the app cannot tell such an upload from the application itself. While
# this is False every non-GET before hand-over is refused, uploads included,
# and a submit or handoff on such a board is refused before filling. Turning it
# on is a code change with a live check and a fixture (spec 4.3, 6.9): the only
# upload allowed would be to the exact address Greenhouse's own response names.
S3_UPLOAD_ENABLED = False


class Endpoint(NamedTuple):
    """An exact host and a path prefix. ``kind`` names the field a lookup serves. ``methods`` are the methods the request rules let through to it
    when it is a write (a CAPTCHA endpoint); empty for any."""

    host: str
    path_prefix: str
    kind: str = ""
    methods: tuple[str, ...] = ()


class DomainSet(frozenset):
    """A set of domain names that also holds every subdomain of each: ``"a.b.test" in DomainSet({"b.test"})``.

    For a page's usage reporting, which an ATS names by domain (``googletagmanager.com``) and a script may reach under any subdomain.
    It is a ``frozenset`` of the names written down, so everything that iterates or compares one sees exactly those; only ``in`` widens.
    """

    def __contains__(self, host: object) -> bool:
        return isinstance(host, str) and any(host == known or host.endswith("." + known) for known in self)


# The typeahead lookups a form calls (location, school, degree, discipline), each tied to the field it serves. Confirmed on
# 2026-10-03 against nine live job-board forms and pinned in tests/fixtures/apply/greenhouse/endpoints.json (a test makes the
# two agree). Only location and school send what was typed (the query parameters ``text`` and ``term``); degree and discipline
# are whole lists fetched when the field is first opened. ``{token}`` is the board's own token: a prefix cannot name every
# board, so the agent fills it in from the posting's address (apply.agent.bind_endpoints) and an unfilled one never matches.
GREENHOUSE_LOOKUP_ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("api-geocode-earth-proxy.greenhouse.io", "/v1/autocomplete", "location"),
    Endpoint("boards.greenhouse.io", "/v1/boards/{token}/education/schools", "school"),
    Endpoint("boards.greenhouse.io", "/v1/boards/{token}/education/degrees", "degree"),
    Endpoint("boards.greenhouse.io", "/v1/boards/{token}/education/disciplines", "discipline"),
)

# The lookup kinds whose request carries what the student typed (the others fetch a whole list when the field is opened).
TYPED_LOOKUP_KINDS = frozenset({"location", "school"})

# CAPTCHA services. Greenhouse's is reCAPTCHA Enterprise, invisible, served from www.recaptcha.net and www.gstatic.com
# (confirmed 2026-10-03, no POST to either before Submit); the other four are not seen on a Greenhouse form and are kept
# unconfirmed until M6 decides whether a board that embeds one is supported. Nothing that carries a planned value may go to them.
CAPTCHA_ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("www.recaptcha.net", "/recaptcha/enterprise"),
    Endpoint("www.gstatic.com", "/recaptcha/"),
    Endpoint("www.google.com", "/recaptcha/"),
    Endpoint("hcaptcha.com", "/"),
    Endpoint("api.hcaptcha.com", "/"),
    Endpoint("challenges.cloudflare.com", "/"),
)

# The two of them a Greenhouse form was seen to load from (the browser can look up no other CAPTCHA host: apply.agent.RESOLVABLE_HOSTS).
CONFIRMED_CAPTCHA_HOSTS = ("www.recaptcha.net", "www.gstatic.com")


@dataclass(frozen=True)
class RoutePolicy:
    """What the request rules know about one ATS: the hosts and endpoints its form uses, and what counts as its submit POST and its confirmation page.

    A frozen value each ``apply.ats.AtsSpec`` builds (docs/phase5-lever-handoff-spec.md 5.2 item 3). ``route_decision``,
    ``looks_like_a_send``, ``student_submit_elsewhere``, ``decide_outcome`` and ``new_code_prompt`` read it as an argument; the
    agent reads the same value for its own checks of an address. Nothing in it is a planned value.
    """

    display_name: str                                 # how a sentence names the ATS ("Greenhouse"); the AtsSpec's name
    navigation_hosts: frozenset[str]                  # where a main-frame navigation may go
    submit_hosts: frozenset[str]                      # the host the form's submit path belongs to
    form_post_hosts: frozenset[str]                   # the hosts a form could post an application to: an aborted non-GET to one is "elsewhere"
    telemetry_hosts: frozenset[str]                   # the page's usage reporting: refused for every method, silently
    static_asset_host: Callable[[str], bool]          # the hosts its scripts, styles, fonts and pictures load from
    lookup_endpoints: tuple[Endpoint, ...]            # the typeahead lookups, with ``{token}`` unfilled (the agent binds the posting's own)
    captcha_endpoints: tuple[Endpoint, ...]           # CAPTCHA services nothing carrying a planned value may reach
    storage_upload_suffixes: tuple[str, ...]          # a non-GET to a host ending with one is a file going to storage (``S3_UPLOAD_ENABLED``)
    resolvable_hosts: tuple[str, ...]                 # every name its form needs the browser to look up (fnmatch patterns); the union is the resolver rule
    confirmation_reached: Callable[["Observation"], bool]   # the main frame is on its own confirmation path
    # What only some ATSs have. Greenhouse leaves them at their defaults, and the rules then do what they always did.
    challenge_path_prefixes: tuple[str, ...] = ()     # non-GET paths on a navigation host that belong to the page's bot check (Cloudflare's): allowed, and never "elsewhere"
    resume_post_path: str = ""                        # the path on the posting's own host where the page reads a file as it is attached; "" for an ATS whose page does not
    submit_content_types: tuple[str, ...] = ()        # the submit POST must carry one of these content types; empty means any
    bind_submit_host: bool = False                    # the submit POST must go to the posting's own host (``RouteState.board_host``); with none bound, nothing is one
    security_code_posts: bool = True                  # one more POST to the submit path may pass for an emailed code (Greenhouse's); False where the ATS emails none (Lever, Q5)
    outcome_table: Callable[..., Any] | None = None   # this ATS's own rows of the outcome table, which ``decide_outcome`` hands over to; None means the shared one

    def is_submit_request(self, host: str, path: str, submit_path: str, board_host: str = "") -> bool:
        """Whether a request to this host and path is the one the loader's submit path names (the method is the caller's to check).

        With ``bind_submit_host`` the host must also be the posting's own (``board_host``): the same path on the other host is not it.
        """
        if host not in self.submit_hosts or not submit_path or path != submit_path:
            return False
        return not self.bind_submit_host or (bool(board_host) and host == board_host)

    def accepts_submit_body(self, content_type: str) -> bool:
        """Whether a request of this (lower-case) content type may be the submit POST: ``submit_content_types``, or any when it has none."""
        return not self.submit_content_types or content_type.startswith(self.submit_content_types)

    def is_challenge_request(self, host: str, path: str) -> bool:
        """Whether a request is to the page's own bot check, on a host of its form (``challenge_path_prefixes``)."""
        return bool(self.challenge_path_prefixes) and host in self.navigation_hosts and path.startswith(self.challenge_path_prefixes)

MODES = ("lookup", "rehearse", "submit", "handoff")
PHASE_BEFORE_INPUT = "before_input"      # lookup, rehearse: the agent has not typed anything yet
PHASE_AFTER_INPUT = "after_input"        # lookup, rehearse: from the first input on
PHASE_FILL = "before_hand_over"          # submit, handoff: the agent is filling
PHASE_STUDENT = "student"                # handoff only: the window is the student's, and their Submit asks for hand-over
PHASE_AFTER_HAND_OVER = "after_hand_over"
PHASES = {
    "lookup": (PHASE_BEFORE_INPUT, PHASE_AFTER_INPUT),
    "rehearse": (PHASE_BEFORE_INPUT, PHASE_AFTER_INPUT),
    "submit": (PHASE_FILL, PHASE_AFTER_HAND_OVER),
    "handoff": (PHASE_FILL, PHASE_STUDENT, PHASE_AFTER_HAND_OVER),
}


def _get(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def is_greenhouse_host(host: str) -> bool:
    return host == GREENHOUSE_DOMAIN or host.endswith("." + GREENHOUSE_DOMAIN)


def is_static_asset_host(host: str) -> bool:
    """One of the hosts Greenhouse's own scripts, styles, fonts and pictures load from (``STATIC_ASSET_HOSTS`` and its shards)."""
    return host in STATIC_ASSET_HOSTS or _STATIC_ASSET_SHARD.fullmatch(host) is not None


def question_key(text: Any) -> str:
    """The key a question is saved and matched under: lowercase, runs of anything but a-z, 0-9 and ' become one space.

    The same normalization as the extension engine's ``questionKey``
    (``normalizedQuestion`` in content.js today), so the browserless preflight,
    the live plan, and answers saved from the Needs you flow agree.
    """
    return re.sub(r"[^a-z0-9']+", " ", str(text if text is not None else "").lower()).strip()


# ---------------------------------------------------------------------------------------------
# What went wrong, in words for the student. Never a field's value.
# ---------------------------------------------------------------------------------------------

# An optional listed field the plan would fill and the page does not draw: the plan leaves it blank. Not a disagreement between the form
# and its listing (an optional question may be drawn only after a parent answer), so it is not a join problem and never makes a run unclean.
OPTIONAL_NOT_DRAWN = "optional_not_drawn"
OPTIONAL_NOT_DRAWN_MESSAGE = 'The form does not show the optional field "{question}", so the app left it blank'


@dataclass(frozen=True)
class Problem:
    kind: str
    key: str
    message: str
    question: str = ""
    required: bool = True


# ---------------------------------------------------------------------------------------------
# route_decision: the whole request policy of the agent's browser (spec 4.3)
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteRequest:
    """The facts about one request that the route handler gathers. The handler decides nothing itself."""

    method: str
    url: str
    resource_type: str = ""
    is_navigation: bool = False          # a main-frame navigation
    is_websocket: bool = False
    # ``outreach_render.request_allowed`` said the host resolves only to public
    # addresses (loopback and private networks are refused). The handler must
    # pass True: anything else, including leaving it unset, is refused. Tests that
    # serve pages through ``route_hook`` pass True themselves, as FormSubmitter's
    # hook replaces the rule.
    public: bool | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str | bytes | None = None


@dataclass
class RouteState:
    """What the route policy needs to know about the run so far. The handler keeps it up to date."""

    submit_path: str = ""                                 # from the loader; empty means no request can be the submit POST
    values: Mapping[str, Any] = field(default_factory=dict)   # planned values by field key, in memory only
    typing_key: str = ""                                  # the field being typed into right now
    typing_lookup: str = ""                               # the lookup kind that field's typeahead calls (Endpoint.kind); empty when it has none
    # None means the policy's own lists. The agent sets ``lookup_endpoints`` to the policy's with the posting's board token filled in.
    lookup_endpoints: Sequence[Endpoint] | None = None
    captcha_endpoints: Sequence[Endpoint] | None = None
    submit_posts_passed: int = 0
    security_code_prompts: int = 0
    code_posts_passed: int = 0
    # A time.monotonic() instant. The agent sets it to infinity while it types a security code into the page and back to 0 when it is done.
    code_typing_until: float = 0.0
    # Handoff only. Once the app has typed the emailed code (``require_code_press``), the code POST waits for the student's own press of
    # Submit: a trusted click on the form's submit control, seen after the typing finished, in a world the page's scripts cannot reach.
    # However long a widget waits, and however often it retries, a send before that press is refused (D1 B, owner decision 2026-10-08).
    # The requirement stays for the rest of the run: a code POST that passes uses up the press, so a second prompt needs another.
    code_press_required: bool = False
    code_pressed: bool = False
    # Handoff only: when (time.monotonic()) the student last pressed the form's Submit, as the press listener reports it; 0 for never.
    last_press_at: float = 0.0
    # The host the posting's own page was loaded from, for an ATS with more than one (``RoutePolicy.bind_submit_host``, ``resume_post_path``).
    board_host: str = ""
    # Lever's file read (``RoutePolicy.resume_post_path``). The app's own fill may send one such request, only when the student allowed
    # it (``resume_upload_allowed``) and only the planned file (``resume_sha256``, the hex SHA-256 of its bytes, attached under the name
    # ``resume_file_name``); in the student's turn any file the student chose may go, one read for each choice (``student_files_chosen``,
    # which the agent raises on a trusted selection in the page's file input, as the press listener reports Submit). In both the request's
    # ``accountId`` part must be the page's own (``page_account_id``).
    resume_upload_allowed: bool = False
    resume_sha256: str = ""
    resume_file_name: str = ""
    page_account_id: str = ""
    resume_posts_passed: int = 0
    student_files_chosen: int = 0
    student_file_reads_passed: int = 0

    @property
    def code_typing(self) -> bool:
        return time.monotonic() < self.code_typing_until

    def require_code_press(self) -> None:
        """The app typed the code: the press that counts is the next one, so any earlier one is forgotten."""
        self.code_press_required = True
        self.code_pressed = False

    def note_student_press(self) -> None:
        """A trusted click on the form's submit control was seen. For the code POST it counts only once the app has typed the code and finished."""
        self.last_press_at = time.monotonic()
        if self.code_press_required and not self.code_typing:
            self.code_pressed = True

    def record(self, decision: "Allow") -> None:
        """Count a request the handler let through, so the one-submit-POST rule sees it."""
        if decision.code_post:
            self.code_posts_passed += 1
            self.code_pressed = False    # the press was used by this POST; a later code POST (a second prompt) needs a new one
        elif decision.resume_post:
            self.resume_posts_passed += 1
            if decision.student_file:
                self.student_file_reads_passed += 1
        elif decision.submit_post:
            self.submit_posts_passed += 1

    def note_security_code_prompt(self) -> None:
        """Greenhouse answered 428, or the code inputs appeared: one more POST, for the code, may pass."""
        self.security_code_prompts += 1


@dataclass(frozen=True)
class Allow:
    rule: str = ""
    # Handoff only: the student's first Submit. The handler asks the parent for
    # the hand-over and continues the request only on a committed True reply.
    requires_hand_over: bool = False
    submit_post: bool = False       # this request is the submit POST (count it with RouteState.record)
    code_post: bool = False         # this request is the POST that carries a security code
    resume_post: bool = False       # this request is the page reading an attached file (count it with RouteState.record)
    student_file: bool = False      # ...and the file is one the student chose, in their turn (it uses up one of ``RouteState.student_files_chosen``)
    digest: str = ""                # the hex SHA-256 of the file in a ``resume_post``, for the record (never the file)


@dataclass(frozen=True)
class Abort:
    rule: str
    reason: str
    host: str = ""
    field_key: str = ""

    def record(self, method: str) -> dict[str, str]:
        """The refused_json entry: method, host, rule and the field key. Never a URL query or a body."""
        entry = {"method": method.upper(), "host": self.host, "rule": self.rule}
        if self.field_key:
            entry["field_key"] = self.field_key
        return entry


def _endpoint_matches(endpoints: Iterable[Endpoint], host: str, path: str, kind: str | None = None, method: str | None = None) -> bool:
    """Whether the request is to one of ``endpoints``; with ``method``, one whose ``methods`` (when it names any) include it."""
    return any(
        host == endpoint.host.lower() and path.startswith(endpoint.path_prefix) and (kind is None or endpoint.kind == kind)
        and (method is None or not endpoint.methods or method.upper() in endpoint.methods)
        for endpoint in endpoints
    )


def _guarded_values(values: Mapping[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for key in sorted(values):
        held = values[key]
        for text in (held if isinstance(held, (list, tuple, set, frozenset)) else [held]):
            if isinstance(text, str) and len(text) >= MIN_GUARDED_VALUE:
                found.append((key, text))
                # A browser drops the line breaks of a URL, and a script may split or re-join them,
                # so each line of a multi-line answer is guarded on its own too.
                lines = [line.strip() for line in text.splitlines()]
                if len(lines) > 1:
                    found.extend((key, line) for line in lines if len(line) >= MIN_GUARDED_VALUE)
    return found


CRLF, LF, BACKSLASH = chr(13) + chr(10), chr(10), chr(92)


def _stable_base64(data: bytes, urlsafe: bool) -> set[str]:
    """The characters of ``data``'s base64 that do not depend on the bytes around it, for each of the three offsets it can sit at.

    A script that base64-encodes a JSON document puts the value at any offset modulo three, so the whole form (with its padding) is
    not what appears: a piece in the middle is. Padding and the characters shared with a neighbouring byte are left out.
    """
    forms: set[str] = set()
    for lead, skip in ((0, 0), (1, 2), (2, 3)):
        encoded = base64.urlsafe_b64encode(bytes(lead) + data) if urlsafe else base64.b64encode(bytes(lead) + data)
        text = encoded.decode("ascii").rstrip("=")
        rest = (lead + len(data)) % 3
        stable = text[skip:len(text) - 1] if rest else text[skip:]   # the last character of a partial group is shared with the next byte
        if len(stable) >= MIN_GUARDED_VALUE:
            forms.add(stable)
    return forms


def _legacy_escapes(text: str) -> set[str]:
    """Latin-1 percent-encoding (a legacy form, or ``escape()``) and ``%uXXXX``, which UTF-8 percent-encoding does not give."""
    forms: set[str] = set()
    if text.isascii():
        return forms
    try:
        forms.add(quote(text, safe="", encoding="latin-1", errors="strict"))
        forms.add(quote_plus(text, encoding="latin-1", errors="strict"))
    except UnicodeEncodeError:
        pass   # a character Latin-1 cannot hold is only ever sent as UTF-8 or %uXXXX
    forms.add("".join(
        quote(char, safe="@*_+-./") if char.isascii() else f"%u{ord(char):04X}" if ord(char) < 0x10000 else quote(char, safe="") for char in text
    ))
    return forms


def _encodings(value: str) -> set[str]:
    """The ways a value is likely to appear in a request: raw, URL-encoded (UTF-8, Latin-1 and %uXXXX), JSON-escaped, base64, and with CRLF line breaks."""
    lf = value.replace(CRLF, LF)
    forms = {value}
    plain = set()   # the forms a script has in hand before it encodes them for a URL
    for text in (value, lf, lf.replace(LF, CRLF)):
        forms.add(text)
        plain.add(text)
        forms.add(quote(text, safe=""))
        forms.add(quote_plus(text))
        forms |= _legacy_escapes(text)
        for ascii_only in (True, False):
            escaped = json.dumps(text, ensure_ascii=ascii_only)[1:-1]
            forms.add(escaped)
            forms.add(escaped.replace("/", BACKSLASH + "/"))
            plain.add(escaped)
    # An analytics beacon sends its payload as base64 (standard or URL-safe), and the value is somewhere inside it.
    for text in plain:
        data = text.encode("utf-8", errors="replace")
        forms |= _stable_base64(data, False) | _stable_base64(data, True)
    return forms


def leaked_field(request: RouteRequest, values: Mapping[str, Any], *, exclude: str = "") -> str:
    """The key of a planned value found in the request's URL, headers or body, or "".

    Each value of four or more characters is searched raw, URL-encoded,
    JSON-escaped (a newline or a quote written as an escape sequence), with CRLF
    line breaks, and case-folded, in the text as sent and as URL-decoded both ways
    (``+`` as a space, and ``+`` left alone, which is what a script that never
    called encodeURIComponent sends). ``exclude`` is the field whose own typed
    text a lookup request may carry.
    """
    body = request.body
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8", errors="replace")
    texts = [request.url, *(str(value) for value in request.headers.values()), body or ""]
    haystacks = set(texts) | {unquote_plus(text) for text in texts} | {unquote(text) for text in texts}
    folded = [text.casefold() for text in haystacks]
    for key, value in _guarded_values({k: v for k, v in values.items() if k != exclude}):
        for variant in _encodings(value):
            if any(variant in text for text in haystacks) or any(variant.casefold() in text for text in folded):
                return key
    return ""


HOST_WITHHELD = "[host withheld]"


def _unicode_host(host: str) -> str:
    """The host with each ``xn--`` label turned back into the text it stands for ("" when there is none)."""
    if "xn--" not in host:
        return ""
    labels = []
    for label in host.split("."):
        try:
            labels.append(label.encode("ascii").decode("idna") if label.startswith("xn--") else label)
        except (UnicodeError, ValueError):
            labels.append(label)
    return ".".join(labels)


def safe_host(host: str, values: Mapping[str, Any]) -> str:
    """The host to write in a refused-request record: a host that holds a planned value (a script can put one in a subdomain) is withheld.

    Chromium writes an international host name as punycode before anything sees it, so the host is searched as written and as the text
    its ``xn--`` labels stand for; and when any planned value is not plain ASCII, a host with an ``xn--`` label is withheld whole.
    """
    if not host or not values:
        return host
    unicode_host = _unicode_host(host)
    for form in (host, unicode_host):
        if form and leaked_field(RouteRequest(method="GET", url=form), values):
            return HOST_WITHHELD
    if unicode_host and any(not text.isascii() for _key, text in _guarded_values(values)):
        return HOST_WITHHELD
    return host


def route_decision(mode: str, phase: str, request: RouteRequest, state: RouteState, policy: RoutePolicy) -> Allow | Abort:
    """Whether the agent's browser may make this request. The route handler applies the answer and nothing else.

    Rules for every mode, in order: a main-frame navigation may go only to the
    ATS's own board hosts (``policy.navigation_hosts``); WebSockets are refused; only public addresses; and the
    value guard, which refuses any request carrying a planned value on any host
    (the ATS's included), with three exceptions. Then the table for the mode
    and phase (spec 4.3). Anything unrecognised is refused.
    """
    method = request.method.upper()
    parts = urlsplit(request.url)
    host = _host(request.url)
    path = parts.path

    def abort(rule: str, reason: str, field_key: str = "") -> Abort:
        # A record never carries a value, and the host of a request is the one place a script can put one into a refused URL.
        return Abort(rule, reason, safe_host(host, state.values), field_key)

    if mode not in PHASES or phase not in PHASES[mode]:
        return abort("unknown_phase", "The app did not recognise this stage of the run, so it refused the request")
    if request.is_navigation and host not in policy.navigation_hosts:
        return abort("offsite_navigation", f"This posting sends applicants to {host}")
    if request.is_websocket:
        return abort("websocket", "The page tried to open a WebSocket, which the app refuses")
    if request.public is not True:
        return abort("non_public_address", "The address is not a public one")

    is_submit_post = (
        method == "POST" and policy.is_submit_request(host, path, state.submit_path, state.board_host)
        and policy.accepts_submit_body(_content_type(request))
    )
    is_resume_post = _is_resume_post(mode, phase, method, host, path, state, policy)
    lookup_endpoints = policy.lookup_endpoints if state.lookup_endpoints is None else state.lookup_endpoints
    captcha_endpoints = policy.captcha_endpoints if state.captcha_endpoints is None else state.captcha_endpoints
    after_hand_over = phase == PHASE_AFTER_HAND_OVER
    # A lookup is the one the typed field's own typeahead calls: the plan names its
    # kind, and only an endpoint of that kind counts (never any pinned endpoint).
    is_lookup = (
        method == "GET" and bool(state.typing_key) and bool(state.typing_lookup) and not request.is_navigation
        and _endpoint_matches(lookup_endpoints, host, path, state.typing_lookup)
    )
    # The value guard. Exempt: the submit POST itself, a lookup GET for the field
    # being typed (which may carry that field's own text and nothing else), and
    # GETs once the submit POST has passed. In a handoff that last exemption covers the board's own hosts only: the form can
    # stay on the page for the rest of the run (a security code, a challenge), and what a script there sends to any other host
    # in a GET is checked as before.
    read_after_press = (
        after_hand_over and method == "GET" and bool(state.submit_posts_passed) and (mode != "handoff" or host in policy.navigation_hosts)
    )
    if not is_submit_post and not read_after_press:
        # The file read's body is judged part by part in ``resume_post_decision``; its address and headers are checked here like any request's.
        leaked = leaked_field(replace(request, body=None) if is_resume_post else request, state.values, exclude=state.typing_key if is_lookup else "")
        if leaked:
            return abort("value_guard", "A request carrying a filled-in answer was refused", leaked)

    if method not in SAFE_METHODS and host.endswith(policy.storage_upload_suffixes) and not S3_UPLOAD_ENABLED:
        return abort("s3_upload", "The page tried to upload a file before Submit, which the app does not allow yet")

    if mode in ("lookup", "rehearse"):
        if phase == PHASE_BEFORE_INPUT:
            if method in SAFE_METHODS:
                return Allow()
            # No exception for CAPTCHA endpoints: Greenhouse's runs only on submit.
            return abort("non_get", "A rehearsal sends nothing but GET requests")
        if is_lookup:
            return Allow("lookup")
        if method == "GET" and not request.is_navigation and policy.static_asset_host(host) and request.resource_type in STATIC_RESOURCE_TYPES:
            return Allow("static_asset")
        return abort("after_first_input", "After the first input, only the typed field's lookup and static assets may load")

    # submit and handoff
    if host in policy.telemetry_hosts:
        # Every method: a GET beacon can carry a value as well as a POST, and none of it is the application. Refused and recorded.
        return abort("telemetry", "The page's own usage reporting was refused")
    if is_submit_post and state.code_typing:
        # The app is typing a security code: a widget that sends by itself on the last character must not send the application (D1 B).
        # Never counted, so the student's own press of Submit keeps the prompt's allowance.
        return abort("code_post_while_typing", "A submit request made while the app typed the security code was refused")
    if method in SAFE_METHODS:
        return Allow()
    if is_resume_post:
        return resume_post_decision(phase, request, state, policy)
    if is_submit_post:
        if phase == PHASE_FILL:
            return abort("before_hand_over", "A request that could submit the form was refused before hand-over")
        if phase == PHASE_STUDENT and not state.submit_posts_passed:
            return Allow("hand_over", requires_hand_over=True, submit_post=True)
        if not state.submit_posts_passed:
            return Allow("submit", submit_post=True)
        if policy.security_code_posts and state.security_code_prompts > state.code_posts_passed:
            if state.code_press_required and not state.code_pressed:
                # The app typed the code. The widget (or anything else on the page) may not send it before the student presses Submit.
                return abort("code_post_before_press", "A request that would send the security code was refused because you had not pressed Submit")
            return Allow("security_code", code_post=True)
        return abort("second_submit_post", "A second submit request was refused")
    if phase == PHASE_FILL and is_upload(request) and (_endpoint_matches(captcha_endpoints, host, path) or policy.is_challenge_request(host, path)):
        # The attached file is in the input, and a page's script can read it and send it anywhere a write is let through. A compressed file holds none of
        # the student's words, so the value guard cannot tell. Nothing the fill lets through to these addresses is a file.
        return abort("upload_elsewhere", "The page tried to send a file to an address that is not the file read the app allowed")
    if _endpoint_matches(captcha_endpoints, host, path, method=method):
        return Allow("captcha")
    if policy.is_challenge_request(host, path):
        return Allow("challenge")
    if phase == PHASE_AFTER_HAND_OVER:
        return abort("other_non_get", "A request to an address the app does not recognise was refused")
    return abort("non_get_before_hand_over", "Nothing that could carry the application may leave before hand-over")


def _content_type(request: RouteRequest) -> str:
    for name, value in request.headers.items():
        if str(name).lower() == "content-type":
            return str(value).lower()
    return ""


# ---------------------------------------------------------------------------------------------
# The file read: a page that reads an attached file at once (Lever's /parseResume, spec 3.8 and 7)
# ---------------------------------------------------------------------------------------------

class MultipartPart(NamedTuple):
    """One part of a multipart body: its form name, its file name (None when the part has none), its header lines, its bytes and where it sits in the body."""

    name: str
    filename: str | None
    headers: tuple[tuple[str, str], ...]     # (lower-case name, value), in order
    data: bytes
    start: int                               # the part's headers and bytes are body[start:end]
    end: int


_MULTIPART_BOUNDARY = re.compile(r'multipart/form-data\s*;\s*boundary=(?:"([^"\r\n]{1,70})"|([^\s;"]{1,70}))\s*', re.IGNORECASE)
_DISPOSITION = re.compile(r'form-data; name="([^"]*)"(?:; filename="([^"]*)")?')


def read_multipart(content_type: str, body: bytes) -> tuple[MultipartPart, ...] | None:
    """The parts of a multipart/form-data body, or None when it is not one this reader is sure of. Strict on purpose, because it backs a refusal.

    The body must begin with the first boundary (no preamble), every part must have a header block and a ``Content-Disposition`` of the exact
    shape a browser writes (``form-data; name="..."`` and an optional ``; filename="..."``), and nothing but one line break may follow the
    closing boundary. ``content_type`` is the header as sent (any case); the boundary keeps its case.
    """
    found = _MULTIPART_BOUNDARY.fullmatch(content_type.strip()) if content_type.lower().lstrip().startswith("multipart/form-data") else None
    if found is None or not isinstance(body, (bytes, bytearray)):
        return None
    body = bytes(body)
    delimiter = b"--" + (found.group(1) or found.group(2)).encode("latin-1", errors="replace")
    if not body.startswith(delimiter + b"\r\n"):
        return None
    parts: list[MultipartPart] = []
    cursor = len(delimiter) + 2
    while True:
        end = body.find(b"\r\n" + delimiter, cursor)
        if end < 0:
            return None
        head, divider, data = body[cursor:end].partition(b"\r\n\r\n")
        if not divider:
            return None
        headers: list[tuple[str, str]] = []
        for line in head.decode("utf-8", errors="replace").split("\r\n"):
            name, colon, value = line.partition(":")
            if not colon or not name or name != name.strip():
                return None
            headers.append((name.lower(), value.strip()))
        dispositions = [value for name, value in headers if name == "content-disposition"]
        matched = _DISPOSITION.fullmatch(dispositions[0]) if len(dispositions) == 1 else None
        if matched is None:
            return None
        parts.append(MultipartPart(matched.group(1), matched.group(2), tuple(headers), data, cursor, end))
        after = end + 2 + len(delimiter)
        if body[after:after + 2] == b"--":
            return tuple(parts) if body[after + 2:] in (b"", b"\r\n") else None
        if body[after:after + 2] != b"\r\n":
            return None
        cursor = after + 2


def _is_resume_post(mode: str, phase: str, method: str, host: str, path: str, state: RouteState, policy: RoutePolicy) -> bool:
    """Whether this is a POST to the file-read path of the posting's own host, in a handoff's fill or the student's turn.

    Only the address is read here: whether the request may pass is ``resume_post_decision``, and anything that is not this (another host,
    another path, another mode or phase) is judged by the general rules and refused as every other write is.
    """
    return (
        bool(policy.resume_post_path) and mode == "handoff" and phase in (PHASE_FILL, PHASE_STUDENT) and method == "POST"
        and bool(state.board_host) and host == state.board_host and path == policy.resume_post_path
    )


def _posted_name(attached: str) -> str | None:
    """The file name as the page posts it: each run of characters outside letters, digits, dot, underscore and hyphen is one underscore. None for no name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", attached) if attached else None


def resume_post_decision(phase: str, request: RouteRequest, state: RouteState, policy: RoutePolicy) -> Allow | Abort:
    """The file read of a page (spec 7, "The resume POST"): it passes only if every condition holds, and each one that does not has its own rule.

    The caller has checked the address (``_is_resume_post``) and the value guard on the URL and the headers. Here: during the app's fill the
    student must have allowed it (``resume_upload_allowed``) and no earlier one may have passed (``resume_posts_passed``); the content type is
    multipart/form-data; the body has exactly two parts, ``resume`` (a file; during the fill, the planned file's bytes by SHA-256) and
    ``accountId`` equal to the page's own value (``page_account_id``); and nothing outside the ``resume`` part, which holds the student's
    own name and email by design, carries a planned value. Every failure is a refusal; nothing here ever allows by default.
    """
    host = _host(request.url)

    def abort(rule: str, reason: str, field_key: str = "") -> Abort:
        return Abort(rule, reason, safe_host(host, state.values), field_key)

    filling = phase == PHASE_FILL
    if not filling and state.student_file_reads_passed >= state.student_files_chosen:
        # The student's own act is choosing a file. Without one, a page script could post any bytes here as a "file", and this is the one write
        # to a form host before hand-over that the value guard does not read the body of.
        return abort("resume_post_unasked", "The page tried to send a file you had not chosen, so the app refused it")
    if filling and not state.resume_upload_allowed:
        return abort("resume_post_off", "The page tried to send a file, and you have not allowed the app to attach one")
    if filling and state.resume_posts_passed:
        return abort("resume_post_second", "A second request that sends a file was refused")
    content_type = str(next((value for name, value in request.headers.items() if str(name).lower() == "content-type"), ""))
    if not content_type.lower().startswith("multipart/form-data"):
        return abort("resume_post_content_type", "A request that sends a file in a form the app does not recognise was refused")
    body = request.body
    parts = read_multipart(content_type, body) if isinstance(body, (bytes, bytearray)) else None
    if parts is None or sorted(part.name for part in parts) != ["accountId", "resume"]:
        return abort("resume_post_parts", "A request that sends more or less than the file and the page's account number was refused")
    resume = next(part for part in parts if part.name == "resume")
    account = next(part for part in parts if part.name == "accountId")
    if resume.filename is None or {name for name, _ in resume.headers} - {"content-disposition", "content-type"}:
        return abort("resume_post_parts", "A request that sends more or less than the file and the page's account number was refused")
    if account.filename is not None or len(account.headers) != 1:
        return abort("resume_post_parts", "A request that sends more or less than the file and the page's account number was refused")
    digest = hashlib.sha256(resume.data).hexdigest()
    if filling and (not state.resume_sha256 or digest != state.resume_sha256.lower()):
        return abort("resume_post_file", "A request that sends a file other than the one the app planned was refused")
    try:
        account_id = account.data.decode("utf-8")
    except UnicodeDecodeError:
        account_id = ""
    if not state.page_account_id or account_id != state.page_account_id:
        return abort("resume_post_account", "A request that sends an account number other than the page's own was refused")
    rest = bytes(body)[:resume.start] + bytes(body)[resume.end:]
    # The part's bytes are the file (pinned by digest in the fill), but its type and its file name are text a script chooses. The type is always
    # read. The name is read in the fill unless it is the one the app attached the file under, or that name as the page rewrites it; in the student's turn it is the student's own file's.
    kind = next((value for name, value in resume.headers if name == "content-type"), "")
    if filling and resume.filename not in (state.resume_file_name, _posted_name(state.resume_file_name)):
        rest += (resume.filename or "").encode("utf-8", errors="replace")
    leaked = leaked_field(RouteRequest(method="POST", url="", headers={"content-type": kind}, body=rest), state.values)
    if leaked:
        return abort("value_guard", "A request carrying a filled-in answer was refused", leaked)
    return Allow("resume_upload", resume_post=True, student_file=not filling, digest=digest)


# How soon after the student's press a refused request still counts as the form's attempt to send (seconds).
SEND_AFTER_PRESS_S = 15.0
_FORM_BODY_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded", "application/json")


def looks_like_a_send(request: RouteRequest, state: RouteState, policy: RoutePolicy) -> bool:
    """Handoff, the student's turn: a refused request that is probably the form sending the application to an address the app does not know.

    True for a non-GET with a body of a form's kind (multipart, URL-encoded or JSON) to a host that is neither one of the form's own hosts
    (``student_submit_elsewhere`` says those), Greenhouse's usage reporting nor a CAPTCHA endpoint, made within ``SEND_AFTER_PRESS_S`` of the
    student's press of Submit. The request is refused either way; this only decides whether the student is told. Nothing it reads is kept.
    """
    if request.method.upper() in SAFE_METHODS or not state.last_press_at:
        return False
    if time.monotonic() - state.last_press_at > SEND_AFTER_PRESS_S:
        return False
    host, path = _host(request.url), urlsplit(request.url).path
    captcha_endpoints = policy.captcha_endpoints if state.captcha_endpoints is None else state.captcha_endpoints
    if host in policy.form_post_hosts or host in policy.telemetry_hosts or _endpoint_matches(captcha_endpoints, host, path):
        return False
    if not request.body or not _content_type(request).startswith(_FORM_BODY_TYPES):
        return False
    return True


def is_upload(request: RouteRequest) -> bool:
    """A non-GET that could carry a file: to an S3 host (``*.amazonaws.com``), with an octet-stream body, or a multipart body
    that holds a file part (a part with a file name), or that cannot be read.

    A form's own submission is multipart too, but a form with no file attached carries only empty file parts, so a multipart
    body with no named file is the form posting its fields, which is not an upload. During the fill the agent also treats any
    aborted non-GET to FORM_POST_HOSTS as fatal; during the student's turn such a POST without a file is "elsewhere".
    """
    if request.method.upper() in SAFE_METHODS:
        return False
    if _host(request.url).endswith(".amazonaws.com"):
        return True
    kind = _content_type(request)
    if kind.startswith("application/octet-stream"):
        return True
    if kind.startswith("multipart/form-data"):
        body = request.body
        if body is None:
            return True
        text = bytes(body).decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
        return bool(re.search(r'filename="[^"]', text))
    return False


def student_submit_elsewhere(request: RouteRequest, state: RouteState, policy: RoutePolicy) -> bool:
    """Handoff, the student's turn: an aborted non-GET that could be the form's own submission to another address (6.13 step 3).

    Telemetry (TELEMETRY_HOSTS, and any other host) is not: it is refused and recorded, silently. Nor is a request to the page's own bot
    check (``RoutePolicy.challenge_path_prefixes``). True for a non-GET that is not a CAPTCHA-endpoint request and either goes to
    FORM_POST_HOSTS or is a form navigation (resource type "document").
    """
    if request.method.upper() in SAFE_METHODS:
        return False
    host, path = _host(request.url), urlsplit(request.url).path
    captcha_endpoints = policy.captcha_endpoints if state.captcha_endpoints is None else state.captcha_endpoints
    if host in policy.telemetry_hosts or _endpoint_matches(captcha_endpoints, host, path) or policy.is_challenge_request(host, path):
        return False
    return host in policy.form_post_hosts or request.resource_type == "document"


# ---------------------------------------------------------------------------------------------
# decide_outcome: what happened after hand-over (spec 6.14)
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SeenRequest:
    """A non-GET request seen after hand-over.

    ``passed`` is False only when the route handler itself aborted the request,
    and the gatherer must take it from the handler's own record of its decisions.
    Never take it from the browser's ``requestfailed`` event: that also fires for
    a request the route let through whose connection then dropped or timed out,
    and calling that "refused" would class a submit that reached Greenhouse as
    "Nothing was sent" and let a retry release the claim.
    """

    method: str
    host: str
    path: str
    status: int | None = None
    passed: bool = True


@dataclass(frozen=True)
class Observation:
    """What the watch loop saw after hand-over. ``requests`` follows the ``SeenRequest.passed`` rule: refusals come from the route handler's record."""

    main_path: str = ""
    main_query: str = ""
    form_present: bool = True
    requests: tuple[SeenRequest, ...] = ()
    security_code_visible: bool = False
    challenge_frame: bool = False
    submit_path: str = ""              # the loader paths
    confirmation_path: str = ""
    board_token: str = ""
    job_id: str = ""
    navigated: bool = False            # any main-frame navigation after hand-over
    first_field_error: str = ""        # the QUESTION of the first field the form marks as wrong; never text from the page's error
    board_host: str = ""               # the host the posting's own page was loaded from, for an ATS with more than one (``RoutePolicy.bind_submit_host``)
    main_host: str = ""                # the host the main frame is on now


@dataclass(frozen=True)
class Outcome:
    outcome: str                       # submitted, unconfirmed, needs_you, failed, waiting (the security code), or challenge_wait (Lever: a challenge before any POST)
    after_click: int
    note: str = ""
    resolved_by: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    # Rows that will not change with more waiting. The other two rows (no POST
    # passed, and "anything else") are the ones a late response can still change,
    # so the caller polls until the window ends before it takes them.
    settled: bool = True


def new_code_prompt(obs: Observation, policy: RoutePolicy) -> bool:
    """After a security-code POST went out: whether Greenhouse is asking for a code again, so a new prompt may start.

    True only on evidence the code was refused: a 428, or any other 4xx with the code boxes still showing. False while that POST
    has no answer yet (the boxes stay on the page until it comes), when it was accepted (the confirmation page is on its way),
    and for a 5xx: an edge proxy may answer 502, 503 or 504 after the origin took the code, so that POST may have been received
    and a second one would send the application twice. One answer is never counted as two prompts.
    """
    submits = [seen for seen in obs.requests if seen.passed and _is_submit_post(seen, obs, policy)]
    if len(submits) < 2:
        return False                    # the first POST's own 428 is the prompt that is already being answered
    status = submits[-1].status
    if status is None or not 400 <= status < 500:
        return False
    return status == 428 or obs.security_code_visible


# The student-facing notes of the outcome table. ``{ats}`` is the ATS's display name (``RoutePolicy.display_name``).
UNCONFIRMED_NOTE = "Your application may have been sent, but {ats} did not show its confirmation page. Look for its email"
UNRECOGNIZED_ADDRESS_NOTE = (
    "The form tried to send to an address the app doesn't recognize, so the app stopped it. "
    "Nothing was sent. Apply from the posting instead"
)
SECURITY_CODE_NOTE = ("{ats} asked for the emailed security code, and Submit application was not pressed after it. "
                      "Look for {ats}'s email")
CODE_REFUSED_NOTE = ("{ats} did not accept the security code, and Submit application was not pressed again after that. "
                     "Look for {ats}'s email")
CHALLENGE_NOTE = "{ats} showed a check that wasn't finished. Look for {ats}'s email"
REFUSED_NOTE = "{ats} refused the form (HTTP {status})"
MARKED_WRONG_NOTE = '. {ats} marked "{question}" as wrong'
def _is_submit_post(seen: SeenRequest, obs: Observation, policy: RoutePolicy) -> bool:
    return seen.method.upper() == "POST" and policy.is_submit_request(seen.host.lower(), seen.path, obs.submit_path, obs.board_host.lower())


def _greenhouse_confirmation_reached(obs: Observation) -> bool:
    """The main frame is on Greenhouse's own confirmation path: the loader's, the board's, or the embed's."""
    path = obs.main_path
    if not path:
        return False
    if obs.confirmation_path and path == obs.confirmation_path:
        return True
    token, job_id = obs.board_token, obs.job_id
    if token and job_id and re.fullmatch(rf"/{re.escape(token)}/jobs/{re.escape(job_id)}/confirmation/?", path):
        return True
    if token and job_id and path == "/embed/job_app/confirmation":
        query = parse_qs(obs.main_query.lstrip("?"))
        return query.get("for") == [token] and query.get("token") == [job_id]
    return False


def decide_outcome(obs: Observation, policy: RoutePolicy, *, code_wait_over: bool = False) -> Outcome:
    """The 6.14 table, first matching row wins. Page wording is never used. ``policy`` says which request is the submit POST and which path is the confirmation page.

    ``code_wait_over`` is passed on the second application of the table, after
    the student's ``security_code_s`` wait: a prompt still open then is
    needs_you instead of waiting again.
    """
    if policy.outcome_table is not None:
        return policy.outcome_table(obs, policy, code_wait_over=code_wait_over)
    name = policy.display_name
    submits = [seen for seen in obs.requests if seen.passed and _is_submit_post(seen, obs, policy)]
    last_status = submits[-1].status if submits else None
    answered_ok = [seen for seen in submits if seen.status is not None and 200 <= seen.status < 400]
    prompted = any(seen.status == 428 for seen in submits)
    evidence = {
        "submit_post": bool(submits),
        "submit_status": last_status,
        "confirmation_path": obs.main_path if policy.confirmation_reached(obs) else "",
        "form_absent": not obs.form_present,
    }

    # 1. Greenhouse answered the submit POST, then showed its own confirmation path, and the form is gone.
    if answered_ok and policy.confirmation_reached(obs) and not obs.form_present:
        detail = {"security_code": True} if prompted else {}
        return Outcome("submitted", 1, resolved_by="page", detail=detail, evidence=evidence)

    # 2. The emailed security code: wait for the student, then apply the table again.
    if obs.security_code_visible or last_status == 428:
        code_posted = len(submits) > 1
        if code_posted and last_status is not None and last_status >= 500:
            # An edge may answer 502, 503 or 504 after the origin took the code: the application may have been sent, so this is
            # row 6 whether or not the boxes are still on the page, and no second POST is wanted. It does not wait for the clock.
            return Outcome("unconfirmed", 1, UNCONFIRMED_NOTE.format(ats=name), evidence=evidence, settled=False)
        if code_wait_over:
            if code_posted and (last_status is None or 200 <= last_status < 400):
                # A code POST went out and its answer never came, or was accepted without the confirmation page: the application
                # may have been sent, so the note that says Submit was not pressed after the code would be untrue.
                return Outcome("unconfirmed", 1, UNCONFIRMED_NOTE.format(ats=name), evidence=evidence, settled=False)
            if code_posted:
                # The code POST was answered 428 or 4xx: Greenhouse refused the code. Submit was pressed; it was not pressed again.
                return Outcome("needs_you", 1, CODE_REFUSED_NOTE.format(ats=name), detail={"security_code": True}, evidence=evidence)
            return Outcome("needs_you", 1, SECURITY_CODE_NOTE.format(ats=name), detail={"security_code": True}, evidence=evidence)
        return Outcome("waiting", 1, detail={"waiting": "security_code"}, evidence=evidence)

    # 3. A challenge frame.
    if obs.challenge_frame:
        return Outcome("needs_you", 1, CHALLENGE_NOTE.format(ats=name), evidence=evidence)

    # 4. Greenhouse refused the form (a 4xx other than 428) and it is still there.
    if last_status is not None and 400 <= last_status < 500 and obs.form_present:
        note = REFUSED_NOTE.format(ats=name, status=last_status)
        if obs.first_field_error:
            note += MARKED_WRONG_NOTE.format(ats=name, question=obs.first_field_error)
        return Outcome("failed", 1, note, evidence=evidence)

    # 5. No submit POST passed the route and nothing navigated: nothing that could carry the application left.
    if not submits and not obs.navigated:
        if any(not seen.passed and seen.method.upper() != "GET" for seen in obs.requests):
            note = UNRECOGNIZED_ADDRESS_NOTE
        else:
            note = "The form did not send, so nothing was sent"
            if obs.first_field_error:
                note += MARKED_WRONG_NOTE.format(ats=name, question=obs.first_field_error)
        return Outcome("failed", 0, note, evidence=evidence, settled=False)

    # 6. Anything else: the POST answered 5xx or never answered, a navigation without a POST, a "thank you" with the form still there.
    return Outcome("unconfirmed", 1, UNCONFIRMED_NOTE.format(ats=name), evidence=evidence, settled=False)


LEVER_NOTHING_LEFT_NOTE = "Nothing that could carry the application left the window"


def lever_outcome(obs: Observation, policy: RoutePolicy, *, code_wait_over: bool = False) -> Outcome:
    """The Lever rows of the outcome table (docs/phase5-lever-handoff-spec.md 6.13). First matching row wins. Page wording is never used.

    There is no emailed security code on Lever (Q5), so a 428 is one more refusal and ``security_code_visible`` is not read. The apply POST
    is the one ``policy`` names for the posting's own host (``Observation.board_host``), and the confirmation page counts only on that same
    host (``main_host``). ``code_wait_over`` here means the waiting is over (the shared name is kept so a caller applies one table the same way):
    until then a visible challenge with no POST sent is the student solving it, which is no outcome, and it answers "challenge_wait" (detail
    ``{"waiting": "challenge"}``, ``settled`` False). It is not "waiting": the shared loop reads that as a security-code prompt, and Lever has none.
    """
    name = policy.display_name
    submits = [seen for seen in obs.requests if seen.passed and _is_submit_post(seen, obs, policy)]
    last_status = submits[-1].status if submits else None
    evidence = {
        "submit_post": bool(submits),
        "submit_status": last_status,
        "confirmation_path": obs.main_path if policy.confirmation_reached(obs) else "",
        "form_absent": not obs.form_present,
    }

    # 1. The apply POST was answered 2xx or 3xx, the main frame is on this posting's confirmation path on the host that POST went to, and the form is gone.
    posted_to = submits[-1].host.lower() if submits else ""
    if (
        last_status is not None and 200 <= last_status < 400 and not obs.form_present and policy.confirmation_reached(obs)
        and bool(obs.main_host) and obs.main_host.lower() == posted_to
    ):
        return Outcome("submitted", 1, resolved_by="page", evidence=evidence)

    # 2. A challenge frame. After a POST it is the student's to finish; before one nothing has been sent and it is no outcome (6.13), until the waiting is over.
    if obs.challenge_frame:
        if submits:
            return Outcome("needs_you", 1, CHALLENGE_NOTE.format(ats=name), evidence=evidence)
        if not code_wait_over:
            return Outcome("challenge_wait", 0, detail={"waiting": "challenge"}, evidence=evidence, settled=False)

    # 3. Lever refused the form (a 4xx) and it is still there.
    if last_status is not None and 400 <= last_status < 500 and obs.form_present:
        note = REFUSED_NOTE.format(ats=name, status=last_status)
        if obs.first_field_error:
            note += MARKED_WRONG_NOTE.format(ats=name, question=obs.first_field_error)
        return Outcome("failed", 1, note, evidence=evidence)

    # 4. No apply POST passed the route and nothing navigated: nothing that could carry the application left.
    if not submits and not obs.navigated:
        note = LEVER_NOTHING_LEFT_NOTE
        if obs.first_field_error:
            note += MARKED_WRONG_NOTE.format(ats=name, question=obs.first_field_error)
        return Outcome("failed", 0, note, evidence=evidence, settled=False)

    # 5. Anything else: the POST answered 5xx or never answered, a navigation without a POST, a 2xx that left the form on the page, "thank you" wording with the form there.
    return Outcome("unconfirmed", 1, UNCONFIRMED_NOTE.format(ats=name), evidence=evidence, settled=False)


# Greenhouse's request policy: the constants above, the way ``route_decision`` and ``decide_outcome`` read them. The names a browser may look up
# are the board's, the lookups', the static files' (three shard patterns for the numbered shards) and the two CAPTCHA hosts a form was seen to load.
GREENHOUSE_ROUTE_POLICY = RoutePolicy(
    display_name=DISPLAY_NAME,
    navigation_hosts=BOARD_HOSTS,
    submit_hosts=frozenset({SUBMIT_HOST}),
    form_post_hosts=FORM_POST_HOSTS,
    telemetry_hosts=TELEMETRY_HOSTS,
    static_asset_host=is_static_asset_host,
    lookup_endpoints=GREENHOUSE_LOOKUP_ENDPOINTS,
    captcha_endpoints=CAPTCHA_ENDPOINTS,
    storage_upload_suffixes=(".amazonaws.com",),
    resolvable_hosts=tuple(sorted({
        *BOARD_HOSTS, *(endpoint.host for endpoint in GREENHOUSE_LOOKUP_ENDPOINTS), *STATIC_ASSET_HOSTS,
        "s?-recruiting.cdn.greenhouse.io", "s??-recruiting.cdn.greenhouse.io", "s???-recruiting.cdn.greenhouse.io",
        *CONFIRMED_CAPTCHA_HOSTS,
    })),
    confirmation_reached=_greenhouse_confirmation_reached,
)


# Lever's request policy (docs/phase5-lever-handoff-spec.md section 7). The hosts a posting lives on; the one lookup its form makes; the
# hCaptcha hosts section 7 names; the paths of Cloudflare's bot check; the usage reporting the page works without; the file read at
# /parseResume (``resume_post_decision``); the apply POST (multipart, to the page's own address on the posting's own host); and the
# Lever rows of the outcome table (``lever_outcome``).
LEVER_LOOKUP_ENDPOINTS: tuple[Endpoint, ...] = tuple(Endpoint(host, lever.SEARCH_LOCATIONS_PATH, "location") for host in lever.LEVER_HOSTS)

# The hCaptcha hosts a Lever apply page was seen to use at load (spec 11, Q3; tests/fixtures/apply/lever/endpoints.json): its script (js), the
# three hosts its widget posts to by itself before anyone presses anything (api, api2 and hcaptcha.com), and the host its frames load from, under the
# one path prefix they were seen at. Each is written only by the method it was seen with (``Endpoint.methods``): js and the frame host were only read
# (a write to either is refused like any other), the other three only posted to. Each is an exact host: a name under w.hcaptcha.com (the logo shards) is not here, because a wildcard there would
# let a name carry a planned value. What the widget posts to once Submit is pressed (getcaptcha, checkcaptcha and the like) was not seen, and until the
# first real handoff shows it, a request to any host not listed is refused: the student is told ("the form tried to send to an address the app doesn't
# recognize") and nothing has left. THIS TUPLE IS WHAT A RECORDING EXTENDS, and nothing else.
LEVER_CAPTCHA_ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("js.hcaptcha.com", "/", methods=("GET",)), Endpoint("hcaptcha.com", "/", methods=("POST",)), Endpoint("api.hcaptcha.com", "/", methods=("POST",)),
    Endpoint("api2.hcaptcha.com", "/", methods=("POST",)), Endpoint("newassets.hcaptcha.com", "/captcha/v1/", methods=("GET",)),
)
# The names the browser must be able to look up for those endpoints to be reachable at all (``js.hcaptcha.com`` by GET: without it Submit does nothing,
# spec 3.10). They are in ``LEVER_ROUTE_POLICY.resolvable_hosts``, so the resolver rule (one list for every ATS) names them; a Greenhouse run's own
# request rules still refuse them (``ApplyAgent._resolvable`` reads the run's own policy), which tests/test_apply_agent_static.py pins.
LEVER_CAPTCHA_RESOLVABLE_HOSTS: tuple[str, ...] = tuple(sorted({endpoint.host for endpoint in LEVER_CAPTCHA_ENDPOINTS}))
# Where the page's fonts and its company's logo come from (Q3). Neither is needed for the form to work; a window that shows them is the student's own.
LEVER_STATIC_HOSTS: tuple[str, ...] = ("cdn.lever.co", "lever-client-logos.s3.amazonaws.com")
# Cloudflare's bot check runs on every Lever page (spec 3.14): its script is under this path and its beacons post there. Narrower than the
# spec's "under /cdn-cgi/", until the recording (Q3) shows another path in use.
LEVER_CLOUDFLARE_PATH_PREFIXES: tuple[str, ...] = ("/cdn-cgi/challenge-platform/",)
# Refused for every method, silently, on any subdomain: the page works without them (spec 7). Bugsnag's own host is a lever.co one (the page's
# ``bug-snag.js`` names ``bugs.lever.co``), so it is listed exactly and ``lever.co`` is never let through by suffix. Some boards also load the "Apply with
# LinkedIn" widget, which submits a form into a frame at every load: that refused document POST is neither the form's nor a reason to end the student's
# turn (Q3).
LEVER_TELEMETRY_DOMAINS: tuple[str, ...] = ("googletagmanager.com", "google-analytics.com", "bugsnag.com", "bugs.lever.co", "linkedin.com")


def is_lever_static_asset_host(host: str) -> bool:
    """Lever's documents and static files come from its own two hosts, its fonts from ``cdn.lever.co`` and a company's logo from one picture bucket."""
    return host in lever.LEVER_HOSTS or host in LEVER_STATIC_HOSTS


def lever_loader_paths(page_url: str) -> tuple[str, str, str]:
    """(the posting's host, the apply POST's path, the confirmation page's path) of a Lever posting's address, or three empty strings for any other.

    The form has no ``action``, so it posts to its own page: the path is the canonical ``/{site}/{job_id}/apply`` whatever query the address
    has, on the host the page was loaded from. The Lever adapter answers ``loader_paths`` with this, and binds the host to ``RouteState.board_host``.
    """
    ref = lever.from_url(page_url)
    if ref is None:
        return "", "", ""
    return ref.host, lever.apply_path(ref.site, ref.job_id), lever.thanks_path(ref.site, ref.job_id)


def _lever_confirmation_reached(obs: Observation) -> bool:
    """The main frame is on this posting's own confirmation path, ``/{site}/{job_id}/thanks`` (spec 6.13). Reaching it proves nothing alone (3.12)."""
    token, job_id = obs.board_token, obs.job_id
    return bool(obs.main_path and token and job_id and re.fullmatch(rf"/{re.escape(token)}/{re.escape(job_id)}/thanks/?", obs.main_path))


LEVER_ROUTE_POLICY = RoutePolicy(
    display_name=lever.DISPLAY_NAME,
    navigation_hosts=frozenset(lever.LEVER_HOSTS),
    submit_hosts=frozenset(lever.LEVER_HOSTS),
    form_post_hosts=frozenset(lever.LEVER_HOSTS),
    telemetry_hosts=DomainSet(LEVER_TELEMETRY_DOMAINS),
    static_asset_host=is_lever_static_asset_host,
    lookup_endpoints=LEVER_LOOKUP_ENDPOINTS,
    captcha_endpoints=LEVER_CAPTCHA_ENDPOINTS,
    storage_upload_suffixes=(),
    resolvable_hosts=tuple(sorted({*lever.LEVER_HOSTS, *LEVER_CAPTCHA_RESOLVABLE_HOSTS, *LEVER_STATIC_HOSTS})),
    confirmation_reached=_lever_confirmation_reached,
    challenge_path_prefixes=LEVER_CLOUDFLARE_PATH_PREFIXES,
    resume_post_path=lever.PARSE_RESUME_PATH,
    submit_content_types=("multipart/form-data",),
    bind_submit_host=True,
    security_code_posts=False,
    outcome_table=lever_outcome,
)


# ---------------------------------------------------------------------------------------------
# join: Greenhouse's field listing against the page (spec 6.5)
# ---------------------------------------------------------------------------------------------

# Greenhouse lists a paste-instead alternative next to each upload (resume_text beside
# resume) inside the same required block. The page shows it only after "Enter manually",
# which the agent never presses, so it is neither a required control nor a missing one.
ALTERNATE_TEXT_FIELDS = frozenset({"resume_text", "cover_letter_text"})
_LEGACY_NAME = re.compile(r"^job_application\[(.+?)\](?:\[\])?$")
CHOICE_TYPES = frozenset({"radio", "checkbox"})


def _canonical_key(name: Any) -> str:
    text = str(name or "")
    match = _LEGACY_NAME.match(text)
    return match.group(1) if match else text


def _key_of(item: Any) -> str:
    return _canonical_key(_get(item, "name") or _get(item, "id") or "")


def _lone_choice_agrees(item: Any, scan: Any) -> bool:
    """Whether a radio or checkbox with no question of its own says something the listing says.

    Its words are the scan's ``label`` (the wrapping label's text, then the control's name and id, which are taken off again). They must be
    contained in the listing's label, an option's label or its description: the same rule a statement box is held to before it is ticked
    (a statement may add the heading and description around the option's own words, never replace them). A statement the listing does not
    carry (``label_from_page``) has nothing to be compared with.
    """
    if _get(item, "label_from_page"):
        return True
    words = str(_get(scan, "label") or "")
    for own in (_get(scan, "name"), _get(scan, "id")):
        if own:
            words = re.sub(re.escape(str(own)), " ", words, flags=re.IGNORECASE)
    words = question_key(words)
    if not words:
        return False
    description = re.sub(r"<[^>]*>", " ", str(_get(item, "description") or ""))
    listed = question_key(" ".join((str(_get(item, "label") or ""), *(str(option) for option in _get(item, "options") or ()), description)))
    return f" {words} " in f" {listed} "


def join(schema_fields: Iterable[Any], scan_fields: Iterable[Any], fill_keys: Iterable[Any] | None = None, *, ats_name: str) -> list[Problem]:
    """Every way the page and the ATS's own listing disagree (``ats_name`` is how the problems' sentences name the ATS).

    Each schema field is matched to page controls by ``name`` or ``id``. A
    required field needs exactly one control (a radio or checkbox group counts
    as one); a control with a required marker the listing does not mention, a
    hidden control the plan would fill, and a question whose wording normalizes
    to a different key are all problems. Greenhouse's own hidden inputs
    (``input_hidden``) are matched but never need a control.

    ``fill_keys`` are the keys the plan would fill. Join runs before there is a
    plan, so with none given every hidden listed control is reported (the
    cautious reading). Once the plan exists, pass its keys and a hidden control
    that the plan leaves blank, such as an optional sub-question the page shows
    only after its parent is answered, is not a problem. A key the plan would
    fill that has no control at all is reported under its own kind (``optional_not_drawn``, not required, and not
    a join problem), so the plan blanks it instead of the run stopping on a field the page does not draw.
    """
    fills = None if fill_keys is None else {_canonical_key(key) for key in fill_keys}
    schema = [item for item in schema_fields]
    scans = [item for item in scan_fields]
    names = {str(_get(item, "name") or "") for item in schema}
    problems: list[Problem] = []
    claimed: set[int] = set()
    for item in schema:
        name = str(_get(item, "name") or "")
        label = str(_get(item, "label") or name)
        required = bool(_get(item, "required"))
        hidden_input = _get(item, "type") == "input_hidden" or name in ALTERNATE_TEXT_FIELDS
        found = [
            index for index, scan in enumerate(scans)
            if name and name in (_canonical_key(_get(scan, "name")), _canonical_key(_get(scan, "id")))
        ]
        claimed.update(found)
        if hidden_input:
            continue
        controls = {("group", name) if _get(scans[i], "type") in CHOICE_TYPES else ("control", i): i for i in found}
        if len(controls) != 1:
            # A listed field the page does not draw is a problem when it is required, when the page draws it twice, and (once there is
            # a plan) when the plan would fill it: the plan then leaves it blank, and the rescan after a choice brings it back if the
            # page only draws it once a parent question is answered. An optional field nothing fills needs no control.
            planned_to_fill = not controls and fills is not None and _canonical_key(name) in fills
            if required or len(controls) > 1:
                problems.append(Problem(
                    "listing_mismatch", name,
                    f"The form does not match what {ats_name}'s own listing describes ({label})", label, required,
                ))
            elif planned_to_fill:
                # Not a disagreement between the form and its listing (6.5, 9.2): an optional question the page draws only after a
                # parent answer is no sign the form is wrong. Its own kind, so the plan blanks the field and the run still counts as clean.
                problems.append(Problem(OPTIONAL_NOT_DRAWN, name, OPTIONAL_NOT_DRAWN_MESSAGE.format(question=label), label, False))
            continue
        scan = scans[next(iter(controls.values()))]
        heard = _get(scan, "question")
        # A radio or checkbox with no fieldset legend (a consent box wrapped in its own label) reports no question at all. Its own words are
        # then the only wording there is, so those are compared with the listing's (never skipped: Finish in browser ticks a statement box
        # from the listing's words alone).
        unheard_choice = _get(scan, "type") in CHOICE_TYPES and not str(heard or "").strip()
        if unheard_choice:
            mismatch = not _lone_choice_agrees(item, scan)
            heard = str(_get(scan, "label") or "")
        else:
            mismatch = heard is not None and question_key(heard) != question_key(label)
        if mismatch:
            problems.append(Problem(
                "wording_mismatch", name, f"The form's wording differs from {ats_name}'s listing ({heard})", str(heard), required,
            ))
        # A control the page hides is never filled: it may be a spam trap. A file
        # input inside a visible upload group is the exception (Greenhouse's
        # input#resume is visually hidden). The scan says "group_visible" when it can.
        if _get(scan, "visible_css") is False:
            group = _get(scan, "widget") == "file_group" and _get(scan, "group_visible", True) is not False
            if not group and (fills is None or required or _canonical_key(name) in fills):
                problems.append(Problem(
                    "hidden_control", name, f"The form hides the field \"{label}\", so the app will not fill it", label, required,
                ))
    for index, scan in enumerate(scans):
        if index in claimed or not _get(scan, "required_any"):
            continue
        heard = str(_get(scan, "question") or _get(scan, "label") or _get(scan, "name") or _get(scan, "id") or "a field")
        if names and _key_of(scan) in names:
            continue
        problems.append(Problem(
            "unlisted_required", _key_of(scan), f"The form has a required field the listing does not mention ({heard})", heard, True,
        ))
    return problems


# ---------------------------------------------------------------------------------------------
# REQUIRED_CHECK_SCRIPT: the independent re-scan of the filled form (spec 6.10)
# ---------------------------------------------------------------------------------------------

# Whether the form is one page of several. It fails closed: any visible button, link or role=button on the page whose words (text, value,
# aria-label or title) include next, continue, proceed, "go to step" or "step 2", and any step counter ("Step 1 of 3",
# "Page 1/3", "2 of 4"), makes it so. The wording of a real multi-page form is not known, so the words are matched, not the whole text.
# It returns fixed words only, never anything the page said. The app reads one page, so a form that shows either is not a form it read
# whole (the rehearsal says so and does not call itself clean).
MORE_PAGES_SCRIPT = r"""() => {
  const form = document.querySelector("form#application-form") || document.querySelector("#application_form");
  if (!form) return [];
  const scope = form.parentElement || form;
  const squash = (text) => String(text || "").replace(/\s+/g, " ").trim().toLowerCase();
  const shown = (el) => {
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const box = el.getBoundingClientRect();
    return box.width >= 2 && box.height >= 2;
  };
  const found = [];
  const goes_on = /\b(next|continue|proceed)\b|\bgo to step\b|\bstep \d+\b/;
  for (const el of document.querySelectorAll("button, a, [role=button], input[type=button], input[type=submit], input[type=image]")) {
    const words = squash([el.innerText, el.value, el.getAttribute("aria-label"), el.getAttribute("title")].join(" "));
    if (goes_on.test(words) && shown(el)) { found.push("next"); break; }
  }
  // A counter written with a word ("Step 1 of 3", "Page 1/3") counts anywhere on the page; a bare "2 of 4" only beside the form.
  const named = /\b(?:step|page)\s*(\d+)\s*(?:of|\/)\s*(\d+)\b/.exec(squash(document.body.innerText));
  const bare = /\b(\d+)\s+of\s+(\d+)\b/.exec(squash(scope.innerText));
  const total = (said) => (said ? Number(said[2]) : 0);
  if (total(named) > 1 || (total(bare) > 1 && Number(bare[1]) <= total(bare)) || document.querySelector("[aria-current=step]")) found.push("steps");
  return found;
}"""

# Read-only JavaScript, run with frame.evaluate. It deliberately shares no code
# or selectors with apps/extension/apply-engine.js, so a bug in that scanner
# cannot hide itself here. It changes nothing on the page, and a static test
# (12.7) asserts that it holds no way to click, type, or submit.
#
# It returns {form, legacy, items, controls, invalid}:
#   items     one per field that any required marker points at: {key, question, markers, kind, value_text, empty}
#   controls  the current value of every control in the form: {key, name, id, kind, value_text, checked, mirror, required}
#   invalid   what the form itself flags: {key, question, reason}
# value_text is a string, or a list of strings for a multi select or a group of
# checked boxes (the labels of the checked ones).
REQUIRED_CHECK_SCRIPT = r"""() => {
  const form = document.querySelector("form#application-form") || document.querySelector("#application_form");
  if (!form) return {form: false, legacy: false, items: [], controls: [], invalid: []};
  const squash = (text) => String(text || "").replace(/\s+/g, " ").trim();
  const shown = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const box = el.getBoundingClientRect();
    return box.width >= 2 && box.height >= 2;
  };
  const escape = (text) => (window.CSS && CSS.escape) ? CSS.escape(text) : String(text).replace(/["\\]/g, "\\$&");
  const everyControl = Array.from(form.querySelectorAll("input, select, textarea")).filter(
    (el) => !["submit", "button", "image", "reset"].includes((el.type || "").toLowerCase()));
  const isMirror = (el) => (el.type || "").toLowerCase() === "hidden" || el.getAttribute("aria-hidden") === "true";
  const real = everyControl.filter((el) => !isMirror(el));
  const keyOf = (el) => el.name || el.id || "";
  const isChoice = (el) => ["radio", "checkbox"].includes((el.type || "").toLowerCase());
  const distinct = (node) => new Set(real.filter((el) => node.contains(el)).map(keyOf)).size;
  const containerOf = (el) => {
    if (isChoice(el)) {
      const group = el.closest("fieldset, [role=group], [role=radiogroup]");
      if (group && group !== form && distinct(group) <= 1) return group;
      const wrap = el.closest("label");
      return (wrap && wrap.parentElement && wrap.parentElement !== form) ? wrap.parentElement : (el.parentElement || form);
    }
    let node = el.parentElement, last = el;
    while (node && node !== form) {
      if (distinct(node) > 1) break;
      last = node;
      if (node.querySelector("label, legend")) return node;
      node = node.parentElement;
    }
    return last;
  };
  const labelTexts = (el, box) => {
    const out = [];
    if (el.id) { const l = document.querySelector('label[for="' + escape(el.id) + '"]'); if (l) out.push(l.textContent); }
    const legend = box.querySelector("legend");
    if (legend && isChoice(el)) out.unshift(legend.textContent);
    const wrap = el.closest("label");
    if (wrap && isChoice(el)) out.push(wrap.textContent);
    const by = el.getAttribute("aria-labelledby");
    if (by) out.push(by.split(/\s+/).map((id) => (document.getElementById(id) || {}).textContent || "").join(" "));
    const first = box.querySelector("label, legend");
    if (first) out.push(first.textContent);
    if (el.getAttribute("aria-label")) out.push(el.getAttribute("aria-label"));
    return out.map(squash).filter(Boolean);
  };
  const stripMarks = (text) => squash(text.replace(/\*/g, " ").replace(/\(required\)|\brequired\b\s*$/gi, ""));
  const labelOfChoice = (el) => {
    const wrap = el.closest("label");
    if (wrap) return squash(wrap.textContent);
    if (el.id) { const l = document.querySelector('label[for="' + escape(el.id) + '"]'); if (l) return squash(l.textContent); }
    return squash(el.value);
  };
  const kindOf = (el) => {
    const type = (el.type || "").toLowerCase();
    if (el.getAttribute("role") === "combobox") return "combobox";
    if (el.tagName === "SELECT") return el.multiple ? "select_multiple" : "select";
    if (el.tagName === "TEXTAREA") return "textarea";
    if (["radio", "checkbox", "file"].includes(type)) return type;
    return type || "text";
  };
  const valueOf = (el, box) => {
    const kind = kindOf(el);
    if (kind === "combobox") {
      const many = Array.from(box.querySelectorAll('[class*="multi-value__label"]')).map((n) => squash(n.textContent));
      if (many.length) return many;
      const one = box.querySelector('[class*="single-value"]');
      return one ? squash(one.textContent) : "";
    }
    if (kind === "select") return (el.value !== "" && el.selectedOptions[0]) ? squash(el.selectedOptions[0].textContent) : "";
    if (kind === "select_multiple") return Array.from(el.selectedOptions).map((o) => squash(o.textContent));
    if (kind === "file") return (el.files && el.files.length) ? el.files[0].name : "";
    return el.value || "";
  };
  const empty = (value) => Array.isArray(value) ? value.length === 0 : value === "";
  // A fixed reason per validity state. The browser's own message is never used: it
  // quotes what was typed (an email that is missing its "@"), and a reason ends up
  // in the run's stored notes, which never hold a field's value.
  const reasonOf = (el) => {
    const v = el.validity || {};
    if (v.valueMissing) return "required and empty";
    if (v.typeMismatch) return "not the kind of value the field expects";
    if (v.patternMismatch) return "does not match the format the field expects";
    if (v.tooShort) return "too short";
    if (v.tooLong) return "too long";
    if (v.rangeUnderflow) return "below the smallest value allowed";
    if (v.rangeOverflow) return "above the largest value allowed";
    if (v.stepMismatch) return "not a value the field allows";
    if (v.badInput) return "not readable as a value for the field";
    return "not valid";
  };
  const invalid = [];
  const items = new Map();
  const questionByKey = new Map();
  real.forEach((el, index) => {
    const box = containerOf(el);
    const key = keyOf(el);
    const texts = labelTexts(el, box);
    const question = stripMarks(texts[0] || "");
    const group = isChoice(el) ? Array.from(box.querySelectorAll("input")).filter((n) => keyOf(n) === key && isChoice(n)) : [el];
    const markers = [];
    if (group.some((n) => n.hasAttribute("required"))) markers.push("attr");
    const holder = el.closest("[role=group], [role=radiogroup]");
    if (el.getAttribute("aria-required") === "true" || (holder && holder.getAttribute("aria-required") === "true")) markers.push("aria");
    if (texts.some((text) => text.includes("*")) || (box.querySelector("label, legend") || {textContent: ""}).textContent.includes("*")) markers.push("asterisk");
    if (box.querySelector('input[required][aria-hidden="true"], input[type="hidden"][required]')) markers.push("hidden_required_sibling");
    if (box.querySelector("span.required")) markers.push("span_required");
    questionByKey.set(keyOf(el), question);
    if (shown(el) && (el.getAttribute("aria-invalid") === "true" || el.matches(":invalid"))) {
      invalid.push({key: key, question: question, reason: el.getAttribute("aria-invalid") === "true" ? "marked invalid by the form" : reasonOf(el)});
    }
    if (!markers.length) return;
    const id = key || ("#" + index);
    const value = isChoice(el) ? group.filter((n) => n.checked).map(labelOfChoice) : valueOf(el, box);
    const seen = items.get(id);
    if (seen) {
      markers.forEach((m) => { if (!seen.markers.includes(m)) seen.markers.push(m); });
      return;
    }
    items.set(id, {key: key, question: question, markers: markers, kind: kindOf(el), value_text: value, empty: empty(value)});
  });
  const controls = everyControl.map((el) => {
    const box = containerOf(el);
    const kind = kindOf(el);
    const checked = isChoice(el) ? el.checked : false;
    const value = isChoice(el) ? (checked ? labelOfChoice(el) : "") : (isMirror(el) ? (el.value || "") : valueOf(el, box));
    return {key: keyOf(el), name: el.name || "", id: el.id || "", kind: kind, value_text: value, checked: checked,
      mirror: isMirror(el), required: el.hasAttribute("required") || el.getAttribute("aria-required") === "true"};
  });
  form.querySelectorAll('[role="alert"], [class*="error"]').forEach((node) => {
    if (node.matches("input, select, textarea") || !shown(node)) return;
    const text = squash(node.textContent);
    if (text) invalid.push({key: "", question: "", reason: text.slice(0, 200)});
  });
  return {form: true, legacy: form.id === "application_form", items: Array.from(items.values()), controls: controls, invalid: invalid};
}"""


# ---------------------------------------------------------------------------------------------
# check_required: the pre-submit check of the filled form (spec 6.10)
# ---------------------------------------------------------------------------------------------

SKIPPED_DISPOSITIONS = frozenset({"deferred", "left_for_you", "blank"})
CAPTCHA_TOKEN_FIELDS = frozenset({"g-recaptcha-response", "h-captcha-response", "cf-turnstile-response"})


def _normal(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text if text is not None else "")).strip().casefold()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value]
    return [] if value in (None, "") else [str(value)]


def _is_empty(value: Any) -> bool:
    return not _as_list(value) or all(not text.strip() for text in _as_list(value))


def _planned(entry: Any) -> Any:
    """What the plan put in the field: the file's original name for a file, else the value."""
    name = _get(entry, "file_name") or _get(entry, "original_name")
    if name:
        return name
    value = _get(entry, "value")
    if isinstance(value, Mapping):
        return value.get("original_name") or value.get("name") or ""
    return value


def _same_value(kind: str, planned: Any, held: Any) -> bool:
    """Whether what the page holds is what the plan put there (page text compared as read back, spec 6.8)."""
    if isinstance(planned, bool):
        return planned == (not _is_empty(held))
    if kind in ("text", "textarea", "email", "tel", "url", "number", "file") and not isinstance(planned, (list, tuple)) and not isinstance(held, list):
        # Only the CRLF difference a browser makes in a textarea is allowed.
        return str(planned).replace("\r\n", "\n") == str(held).replace("\r\n", "\n")
    # A radio or a checkbox group is compared by the labels of what is checked: a
    # planned "Yes" is not satisfied by a checked "No". Only a planned bool means "ticked".
    want, have = _as_list(planned), _as_list(held)
    return sorted(_normal(text) for text in want) == sorted(_normal(text) for text in have)


def _scrub(text: str, secrets: Iterable[tuple[str, str]]) -> str:
    """``text`` with every guarded value it quotes (any case) replaced by a placeholder."""
    for _key, value in secrets:
        text = re.sub(re.escape(value), "[your answer]", text, flags=re.IGNORECASE)
    return text


def _plan_fields(plan: Any) -> list[Any]:
    fields = _get(plan, "fields", plan)
    return list(fields) if fields is not None else []


def _has_source(entry: Any) -> bool:
    source = _get(entry, "source")
    kind = _get(source, "kind") if source is not None else None
    return bool(kind) and kind != "none"


def _group_controls(controls: Iterable[Any]) -> list[tuple[str, dict[str, Any]]]:
    """One entry per control, except that the boxes or radios of a group become one holding the labels of the checked ones."""
    grouped: list[tuple[str, dict[str, Any]]] = []
    choices: dict[str, dict[str, Any]] = {}
    for control in controls:
        key = _canonical_key(_get(control, "key"))
        kind = str(_get(control, "kind") or "")
        if kind not in CHOICE_TYPES:
            grouped.append((key, {"kind": kind, "value_text": _get(control, "value_text"), "mirror": _get(control, "mirror"),
                                  "required": _get(control, "required"), "checked": False}))
            continue
        if key not in choices:
            choices[key] = {"kind": kind, "value_text": [], "mirror": False, "required": False, "checked": False}
            grouped.append((key, choices[key]))
        slot = choices[key]
        slot["required"] = bool(slot["required"] or _get(control, "required"))
        if _get(control, "checked"):
            slot["checked"] = True
            slot["value_text"].extend(_as_list(_get(control, "value_text")))
    return grouped


def check_required(
    items: Iterable[Any], plan: Any, schema: Iterable[Any], initial_values: Mapping[str, Any] | None = None,
    *, controls: Iterable[Any] = (), invalid: Iterable[Any] = (), confirmed_plan_hash: str | None = None, ats_name: str,
) -> list[Problem]:
    """The six checks of 6.10. Any problem is needs_you in a submit run and makes a rehearsal not clean. ``ats_name`` is how a problem's sentence names the ATS.

    ``items``, ``controls`` and ``invalid`` come from REQUIRED_CHECK_SCRIPT.
    Fields the plan marks deferred, left_for_you or blank are skipped for
    checks 1 to 3 (and their empty or invalid state is not held against the
    form). ``confirmed_plan_hash`` is passed for a submit run only.
    """
    initial = {_canonical_key(key): value for key, value in (initial_values or {}).items()}
    plan_by_key = {_canonical_key(_get(entry, "key")): entry for entry in _plan_fields(plan)}
    schema_list = list(schema)
    schema_by_key = {_canonical_key(_get(item, "name")): item for item in schema_list}
    hidden_names = {key for key, item in schema_by_key.items() if _get(item, "type") == "input_hidden"}
    skipped = {key for key, entry in plan_by_key.items() if _get(entry, "disposition") in SKIPPED_DISPOSITIONS}
    problems: list[Problem] = []
    complained: set[str] = set()

    def add(kind: str, key: str, message: str, question: str, required: bool = True) -> None:
        problems.append(Problem(kind, key, message, question, required))
        complained.add(key)

    item_keys: set[str] = set()
    items, controls = list(items), list(controls)
    for item in items:
        key = _canonical_key(_get(item, "key"))
        question = str(_get(item, "question") or key or "a field")
        item_keys.add(key)
        if key in skipped:
            continue
        # 1. Every required item holds something.
        if _get(item, "empty") or _is_empty(_get(item, "value_text")):
            add("empty", key, f"The required field \"{question}\" is empty", question)
            continue
        # 2. It is in the plan, with a source, and holds what the plan put there.
        entry = plan_by_key.get(key)
        if entry is None:
            add("not_planned", key, f"The form has a required field the app did not plan for ({question})", question)
        elif not _has_source(entry):
            add("no_source", key, f"The required field \"{question}\" has no source for its answer", question)
        elif not _same_value(str(_get(item, "kind") or ""), _planned(entry), _get(item, "value_text")):
            add("value_mismatch", key, f"The field \"{question}\" does not hold what the plan put there", question)

    # 3. Every schema-required field appears among the items: a source independent of the scanner.
    for key, item in schema_by_key.items():
        if not _get(item, "required") or _get(item, "type") == "input_hidden" or key in ALTERNATE_TEXT_FIELDS or key in skipped or key in item_keys:
            continue
        label = str(_get(item, "label") or key)
        add("required_not_seen", key, f"{ats_name} lists \"{label}\" as required but the check did not find it on the form", label)

    # 4. No control holds a value the plan did not put there.
    grouped = _group_controls(controls)
    visible_keys = {key for key, control in grouped if not _get(control, "mirror")}
    for key, control in grouped:
        kind = str(_get(control, "kind") or "")
        held = _get(control, "value_text")
        has_value = bool(_get(control, "checked")) if kind in CHOICE_TYPES else not _is_empty(held)
        if not has_value or key in complained:
            continue
        # Greenhouse's own hidden inputs, the CAPTCHA token, and a react-select's mirror of a
        # control that is checked on its own (the mirror holds an option's value, not its label).
        if key in hidden_names or key in CAPTCHA_TOKEN_FIELDS or (_get(control, "mirror") and key in visible_keys):
            continue
        entry = plan_by_key.get(key)
        schema_item = schema_by_key.get(key)
        label = str(_get(entry, "question") or _get(schema_item, "label") or key or "a field") if (entry or schema_item) else (key or "a field")
        required = bool(_get(control, "required") or _get(entry, "required") or _get(schema_item, "required"))
        if entry is not None and key not in skipped and _has_source(entry):
            if not _same_value(kind, _planned(entry), held):
                add("unplanned_value", key, f"The field \"{label}\" holds a value the app did not put there", label, required)
            continue
        if not required and key in initial and _same_value(kind, initial[key], held):
            continue   # an optional field still holding the page's own default: listed as "left as the page set it"
        add("unplanned_value", key, f"The field \"{label}\" holds a value the app did not put there", label, required)

    # 5. The form flags nothing (aria-invalid, native :invalid, visible error text).
    # A page's error text may quote what was typed, and a Problem never holds a value.
    secrets = _guarded_values({
        f"{origin}{index}": value
        for origin, group in (("planned", [_planned(entry) for entry in plan_by_key.values()]),
                              ("item", [_get(item, "value_text") for item in items]),
                              ("control", [_get(control, "value_text") for control in controls]))
        for index, value in enumerate(group)
    })
    for flagged in invalid:
        key = _canonical_key(_get(flagged, "key"))
        if key and key in skipped:
            continue
        label = str(_get(flagged, "question") or key or "the form")
        reason = _scrub(str(_get(flagged, "reason") or "not valid"), secrets)[:160]
        problems.append(Problem("invalid", key, f"The form flagged \"{label}\": {reason}", label, True))

    # 6. Submit runs only: the plan is the one the student confirmed.
    if confirmed_plan_hash is not None and _get(plan, "plan_hash") != confirmed_plan_hash:
        problems.append(Problem("plan_changed", "", "The form or your answers changed since you confirmed. Look at the new plan", "", True))
    return problems


# ---------------------------------------------------------------------------------------------
# clean_rehearsal: does this rehearsal count toward the gate (spec 9.2)
# ---------------------------------------------------------------------------------------------

def _is_file_entry(entry: Any) -> bool:
    source = _get(entry, "source")
    return bool(
        _get(entry, "control") == "file" or _get(entry, "file_sha256")
        or (source is not None and _get(source, "kind") in ("resume", "cover_letter"))
    )


def clean_rehearsal(run: Any) -> bool:
    """A rehearsal is clean when it finished, and nothing that matters was left unresolved.

    ``run`` carries ``plan`` (the value-free entries, as stored in apply_runs.plan_json),
    ``join_problems`` (from ``join``), ``check_problems`` (from ``check_required``)
    and ``outcome``, all required: a run missing any of them (a stored row that
    keeps its plan and problems under other names, say) is never clean. Not clean:
    a problem on a required field, any join problem,
    any failed check, or a planned file that was deferred (a board that uploads as
    you attach). Optional fields left blank, and deferred sensitive fields, do not
    make it unclean, because every rehearsal defers those.
    """
    if _get(run, "outcome", "") != "rehearsed":
        return False
    # A run that does not carry these was not checked, so it is not clean. An empty
    # list means "checked, nothing found".
    plan, join_problems, check_problems = (_get(run, name) for name in ("plan", "join_problems", "check_problems"))
    if plan is None or join_problems is None or check_problems is None:
        return False
    if list(join_problems) or list(check_problems):
        return False
    for entry in plan:
        disposition = _get(entry, "disposition")
        if disposition == "deferred" and _is_file_entry(entry):
            return False
        if _get(entry, "required") and (_get(entry, "problem") or disposition in ("blank", "left_for_you")):
            return False
    return True
