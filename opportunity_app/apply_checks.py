"""The integrity-critical decisions of the Greenhouse apply agent, as pure functions.

Standard library only, with no browser and no database, so the default
unittest suite (which has no Playwright) covers every decision the agent
makes about whether something may leave the page, what happened after Submit,
and whether a filled form is what the student was shown. Browser tests then
only have to prove that the observations these functions read are gathered
correctly.

    route_decision   whether one browser request may go out, in each mode and phase
    decide_outcome   what happened after hand-over, from what the browser saw
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

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, NamedTuple, Sequence
from urllib.parse import parse_qs, quote, quote_plus, unquote, unquote_plus, urlsplit

# ---------------------------------------------------------------------------------------------
# Hosts and endpoints
# ---------------------------------------------------------------------------------------------

# Where a main-frame navigation may go. Never my.greenhouse.io: that is the
# student's own MyGreenhouse account, which the agent never signs in to.
BOARD_HOSTS = frozenset({"job-boards.greenhouse.io", "boards.greenhouse.io"})
# The submit path in the served HTML belongs to this host, not the job-boards one.
SUBMIT_HOST = "boards.greenhouse.io"
GREENHOUSE_DOMAIN = "greenhouse.io"
STATIC_RESOURCE_TYPES = frozenset({"image", "font", "stylesheet", "script", "media"})
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
    """An exact host and a path prefix. ``kind`` names the field a lookup serves."""

    host: str
    path_prefix: str
    kind: str = ""


# The typeahead lookups a form calls (location, school, degree, discipline),
# each tied to the field it serves. Empty until they are confirmed against a
# live board before M5a; until then those fields are left unfilled.
GREENHOUSE_LOOKUP_ENDPOINTS: tuple[Endpoint, ...] = ()

# Checkbox CAPTCHA services. Provisional: M5a confirms them on a live board and
# pins them in a fixture. Nothing that carries a planned value may go to them.
CAPTCHA_ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("www.google.com", "/recaptcha/"),
    Endpoint("www.recaptcha.net", "/recaptcha/"),
    Endpoint("www.gstatic.com", "/recaptcha/"),
    Endpoint("hcaptcha.com", "/"),
    Endpoint("api.hcaptcha.com", "/"),
    Endpoint("challenges.cloudflare.com", "/"),
)

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
    lookup_endpoints: Sequence[Endpoint] = field(default_factory=lambda: GREENHOUSE_LOOKUP_ENDPOINTS)
    captcha_endpoints: Sequence[Endpoint] = field(default_factory=lambda: CAPTCHA_ENDPOINTS)
    submit_posts_passed: int = 0
    security_code_prompts: int = 0
    code_posts_passed: int = 0

    def record(self, decision: "Allow") -> None:
        """Count a request the handler let through, so the one-submit-POST rule sees it."""
        if decision.code_post:
            self.code_posts_passed += 1
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


def _endpoint_matches(endpoints: Iterable[Endpoint], host: str, path: str, kind: str | None = None) -> bool:
    return any(
        host == endpoint.host.lower() and path.startswith(endpoint.path_prefix) and (kind is None or endpoint.kind == kind)
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


def _encodings(value: str) -> set[str]:
    """The ways a value is likely to appear in a request: raw, URL-encoded, JSON-escaped, and with CRLF line breaks."""
    lf = value.replace(CRLF, LF)
    forms = {value}
    for text in (value, lf, lf.replace(LF, CRLF)):
        forms.add(text)
        forms.add(quote(text, safe=""))
        forms.add(quote_plus(text))
        for ascii_only in (True, False):
            escaped = json.dumps(text, ensure_ascii=ascii_only)[1:-1]
            forms.add(escaped)
            forms.add(escaped.replace("/", BACKSLASH + "/"))
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


def route_decision(mode: str, phase: str, request: RouteRequest, state: RouteState) -> Allow | Abort:
    """Whether the agent's browser may make this request. The route handler applies the answer and nothing else.

    Rules for every mode, in order: a main-frame navigation may go only to the
    two board hosts; WebSockets are refused; only public addresses; and the
    value guard, which refuses any request carrying a planned value on any host
    (Greenhouse included), with three exceptions. Then the table for the mode
    and phase (spec 4.3). Anything unrecognised is refused.
    """
    method = request.method.upper()
    parts = urlsplit(request.url)
    host = _host(request.url)
    path = parts.path

    def abort(rule: str, reason: str, field_key: str = "") -> Abort:
        return Abort(rule, reason, host, field_key)

    if mode not in PHASES or phase not in PHASES[mode]:
        return abort("unknown_phase", "The app did not recognise this stage of the run, so it refused the request")
    if request.is_navigation and host not in BOARD_HOSTS:
        return abort("offsite_navigation", f"This posting sends applicants to {host}")
    if request.is_websocket:
        return abort("websocket", "The page tried to open a WebSocket, which the app refuses")
    if request.public is not True:
        return abort("non_public_address", "The address is not a public one")

    is_submit_post = method == "POST" and host == SUBMIT_HOST and bool(state.submit_path) and path == state.submit_path
    after_hand_over = phase == PHASE_AFTER_HAND_OVER
    # A lookup is the one the typed field's own typeahead calls: the plan names its
    # kind, and only an endpoint of that kind counts (never any pinned endpoint).
    is_lookup = (
        method == "GET" and bool(state.typing_key) and bool(state.typing_lookup) and not request.is_navigation
        and _endpoint_matches(state.lookup_endpoints, host, path, state.typing_lookup)
    )
    # The value guard. Exempt: the submit POST itself, a lookup GET for the field
    # being typed (which may carry that field's own text and nothing else), and
    # GETs once the submit POST has passed.
    if not is_submit_post and not (after_hand_over and method == "GET" and state.submit_posts_passed):
        leaked = leaked_field(request, state.values, exclude=state.typing_key if is_lookup else "")
        if leaked:
            return abort("value_guard", "A request carrying a filled-in answer was refused", leaked)

    if method not in SAFE_METHODS and host.endswith(".amazonaws.com") and not S3_UPLOAD_ENABLED:
        return abort("s3_upload", "The page tried to upload a file before Submit, which the app does not allow yet")

    if mode in ("lookup", "rehearse"):
        if phase == PHASE_BEFORE_INPUT:
            if method in SAFE_METHODS:
                return Allow()
            # No exception for CAPTCHA endpoints: Greenhouse's runs only on submit.
            return abort("non_get", "A rehearsal sends nothing but GET requests")
        if is_lookup:
            return Allow("lookup")
        if method == "GET" and not request.is_navigation and is_greenhouse_host(host) and request.resource_type in STATIC_RESOURCE_TYPES:
            return Allow("static_asset")
        return abort("after_first_input", "After the first input, only the typed field's lookup and static assets may load")

    # submit and handoff
    if method in SAFE_METHODS:
        return Allow()
    if is_submit_post:
        if phase == PHASE_FILL:
            return abort("before_hand_over", "A request that could submit the form was refused before hand-over")
        if phase == PHASE_STUDENT and not state.submit_posts_passed:
            return Allow("hand_over", requires_hand_over=True, submit_post=True)
        if not state.submit_posts_passed:
            return Allow("submit", submit_post=True)
        if state.security_code_prompts > state.code_posts_passed:
            return Allow("security_code", code_post=True)
        return abort("second_submit_post", "A second submit request was refused")
    if _endpoint_matches(state.captcha_endpoints, host, path):
        return Allow("captcha")
    if phase == PHASE_AFTER_HAND_OVER:
        return abort("other_non_get", "A request to an address the app does not recognise was refused")
    return abort("non_get_before_hand_over", "Nothing that could carry the application may leave before hand-over")


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
    first_field_error: str = ""


@dataclass(frozen=True)
class Outcome:
    outcome: str                       # submitted, unconfirmed, needs_you, failed, or waiting (the security code)
    after_click: int
    note: str = ""
    resolved_by: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    # Rows that will not change with more waiting. The other two rows (no POST
    # passed, and "anything else") are the ones a late response can still change,
    # so the caller polls until the window ends before it takes them.
    settled: bool = True


UNCONFIRMED_NOTE = "Your application may have been sent, but Greenhouse did not show its confirmation page. Look for its email"
UNRECOGNIZED_ADDRESS_NOTE = (
    "The form tried to send to an address the app doesn't recognize, so the app stopped it. "
    "Nothing was sent. Apply from the posting instead"
)
SECURITY_CODE_NOTE = "Greenhouse asked for the emailed security code, and it was not entered. Look for Greenhouse's email"
CHALLENGE_NOTE = "Greenhouse showed a challenge the app does not touch. Finish it in the window, or look for Greenhouse's email"


def _is_submit_post(seen: SeenRequest, obs: Observation) -> bool:
    return seen.method.upper() == "POST" and seen.host.lower() == SUBMIT_HOST and bool(obs.submit_path) and seen.path == obs.submit_path


def confirmation_reached(obs: Observation) -> bool:
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


def decide_outcome(obs: Observation, *, code_wait_over: bool = False) -> Outcome:
    """The 6.14 table, first matching row wins. Page wording is never used.

    ``code_wait_over`` is passed on the second application of the table, after
    the student's ``security_code_s`` wait: a prompt still open then is
    needs_you instead of waiting again.
    """
    submits = [seen for seen in obs.requests if seen.passed and _is_submit_post(seen, obs)]
    last_status = submits[-1].status if submits else None
    answered_ok = [seen for seen in submits if seen.status is not None and 200 <= seen.status < 400]
    prompted = any(seen.status == 428 for seen in submits)
    evidence = {
        "submit_post": bool(submits),
        "submit_status": last_status,
        "confirmation_path": obs.main_path if confirmation_reached(obs) else "",
        "form_absent": not obs.form_present,
    }

    # 1. Greenhouse answered the submit POST, then showed its own confirmation path, and the form is gone.
    if answered_ok and confirmation_reached(obs) and not obs.form_present:
        detail = {"security_code": True} if prompted else {}
        return Outcome("submitted", 1, resolved_by="page", detail=detail, evidence=evidence)

    # 2. The emailed security code: wait for the student, then apply the table again.
    if obs.security_code_visible or last_status == 428:
        if code_wait_over:
            return Outcome("needs_you", 1, SECURITY_CODE_NOTE, detail={"security_code": True}, evidence=evidence)
        return Outcome("waiting", 1, detail={"waiting": "security_code"}, evidence=evidence)

    # 3. A challenge frame.
    if obs.challenge_frame:
        return Outcome("needs_you", 1, CHALLENGE_NOTE, evidence=evidence)

    # 4. Greenhouse refused the form (a 4xx other than 428) and it is still there.
    if last_status is not None and 400 <= last_status < 500 and obs.form_present:
        note = f"Greenhouse refused the form (HTTP {last_status})"
        if obs.first_field_error:
            note += f": {obs.first_field_error}"
        return Outcome("failed", 1, note, evidence=evidence)

    # 5. No submit POST passed the route and nothing navigated: nothing that could carry the application left.
    if not submits and not obs.navigated:
        if any(not seen.passed and seen.method.upper() != "GET" for seen in obs.requests):
            note = UNRECOGNIZED_ADDRESS_NOTE
        else:
            note = "The form did not send, so nothing was sent"
            if obs.first_field_error:
                note += f". The form says: {obs.first_field_error}"
        return Outcome("failed", 0, note, evidence=evidence, settled=False)

    # 6. Anything else: the POST answered 5xx or never answered, a navigation without a POST, a "thank you" with the form still there.
    return Outcome("unconfirmed", 1, UNCONFIRMED_NOTE, evidence=evidence, settled=False)


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


def join(schema_fields: Iterable[Any], scan_fields: Iterable[Any], fill_keys: Iterable[Any] | None = None) -> list[Problem]:
    """Every way the page and Greenhouse's own listing disagree.

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
    only after its parent is answered, is not a problem.
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
            if required or len(controls) > 1:
                problems.append(Problem(
                    "listing_mismatch", name,
                    f"The form does not match what Greenhouse's own listing describes ({label})", label, required,
                ))
            continue
        scan = scans[next(iter(controls.values()))]
        heard = _get(scan, "question")
        if heard is not None and question_key(heard) != question_key(label):
            problems.append(Problem(
                "wording_mismatch", name, f"The form's wording differs from Greenhouse's listing ({heard})", str(heard), required,
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
    *, controls: Iterable[Any] = (), invalid: Iterable[Any] = (), confirmed_plan_hash: str | None = None,
) -> list[Problem]:
    """The six checks of 6.10. Any problem is needs_you in a submit run and makes a rehearsal not clean.

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
        add("required_not_seen", key, f"Greenhouse lists \"{label}\" as required but the check did not find it on the form", label)

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
