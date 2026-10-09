"""A frozen copy of what the request-policy seam (LV1b) moved: the Greenhouse request rules and outcome table as they were before.

Copied from opportunity_app/apply/checks.py at 80ac76c (the head of the branch below LV1b): the module constants
``FORM_POST_HOSTS``, ``TELEMETRY_HOSTS``, ``S3_UPLOAD_ENABLED``, ``BOARD_HOSTS`` and ``SUBMIT_HOST`` read directly, and the
notes with Greenhouse's name written in. It exists only so tests/test_apply_ats_seam.py can run old against new
(AGENTS.md section 8 rule 14). Do not edit it, do not import anything from it but that test, and do not make it import the
code it is the old copy of. Its first-party imports are the value types and the helpers the seam did not touch
(``RouteRequest``, ``Allow``, ``Abort``, ``Observation``, ``SeenRequest``, ``Outcome``, ``leaked_field``, ``safe_host``,
``is_static_asset_host``, ``PHASES``, ``SAFE_METHODS`` and the like); the constants the old code read as module globals are
copied here by value.
"""

from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlsplit

from opportunity_app.apply.checks import (
    PHASE_AFTER_HAND_OVER,
    PHASE_BEFORE_INPUT,
    PHASE_FILL,
    PHASE_STUDENT,
    PHASES,
    SAFE_METHODS,
    STATIC_RESOURCE_TYPES,
    Abort,
    Allow,
    Observation,
    Outcome,
    RouteRequest,
    RouteState,
    SeenRequest,
    _content_type,
    _endpoint_matches,
    _host,
    is_static_asset_host,
    leaked_field,
    safe_host,
)

BOARD_HOSTS = frozenset({"job-boards.greenhouse.io", "boards.greenhouse.io"})
SUBMIT_HOST = "boards.greenhouse.io"
FORM_POST_HOSTS = frozenset({"job-boards.greenhouse.io", "boards.greenhouse.io", "boards-api.greenhouse.io"})
TELEMETRY_HOSTS = frozenset({"c.spl.greenhouse.io"})
S3_UPLOAD_ENABLED = False
SEND_AFTER_PRESS_S = 15.0
_FORM_BODY_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded", "application/json")


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
        # A record never carries a value, and the host of a request is the one place a script can put one into a refused URL.
        return Abort(rule, reason, safe_host(host, state.values), field_key)

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
    # GETs once the submit POST has passed. In a handoff that last exemption covers the board's own hosts only: the form can
    # stay on the page for the rest of the run (a security code, a challenge), and what a script there sends to any other host
    # in a GET is checked as before.
    read_after_press = (
        after_hand_over and method == "GET" and bool(state.submit_posts_passed) and (mode != "handoff" or host in BOARD_HOSTS)
    )
    if not is_submit_post and not read_after_press:
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
        if method == "GET" and not request.is_navigation and is_static_asset_host(host) and request.resource_type in STATIC_RESOURCE_TYPES:
            return Allow("static_asset")
        return abort("after_first_input", "After the first input, only the typed field's lookup and static assets may load")

    # submit and handoff
    if host in TELEMETRY_HOSTS:
        # Every method: a GET beacon can carry a value as well as a POST, and none of it is the application. Refused and recorded.
        return abort("telemetry", "The page's own usage reporting was refused")
    if is_submit_post and state.code_typing:
        # The app is typing a security code: a widget that sends by itself on the last character must not send the application (D1 B).
        # Never counted, so the student's own press of Submit keeps the prompt's allowance.
        return abort("code_post_while_typing", "A submit request made while the app typed the security code was refused")
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
            if state.code_press_required and not state.code_pressed:
                # The app typed the code. The widget (or anything else on the page) may not send it before the student presses Submit.
                return abort("code_post_before_press", "A request that would send the security code was refused because you had not pressed Submit")
            return Allow("security_code", code_post=True)
        return abort("second_submit_post", "A second submit request was refused")
    if _endpoint_matches(state.captcha_endpoints, host, path):
        return Allow("captcha")
    if phase == PHASE_AFTER_HAND_OVER:
        return abort("other_non_get", "A request to an address the app does not recognise was refused")
    return abort("non_get_before_hand_over", "Nothing that could carry the application may leave before hand-over")


def looks_like_a_send(request: RouteRequest, state: RouteState) -> bool:
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
    if host in FORM_POST_HOSTS or host in TELEMETRY_HOSTS or _endpoint_matches(state.captcha_endpoints, host, path):
        return False
    if not request.body or not _content_type(request).startswith(_FORM_BODY_TYPES):
        return False
    return True


def student_submit_elsewhere(request: RouteRequest, state: RouteState) -> bool:
    """Handoff, the student's turn: an aborted non-GET that could be the form's own submission to another address (6.13 step 3).

    Telemetry (TELEMETRY_HOSTS, and any other host) is not: it is refused and recorded, silently. True for a non-GET that is not a
    CAPTCHA-endpoint request and either goes to FORM_POST_HOSTS or is a form navigation (resource type "document").
    """
    if request.method.upper() in SAFE_METHODS:
        return False
    host, path = _host(request.url), urlsplit(request.url).path
    if host in TELEMETRY_HOSTS or _endpoint_matches(state.captcha_endpoints, host, path):
        return False
    return host in FORM_POST_HOSTS or request.resource_type == "document"


# ---------------------------------------------------------------------------------------------
# decide_outcome: what happened after hand-over (spec 6.14)


def new_code_prompt(obs: Observation) -> bool:
    """After a security-code POST went out: whether Greenhouse is asking for a code again, so a new prompt may start.

    True only on evidence the code was refused: a 428, or any other 4xx with the code boxes still showing. False while that POST
    has no answer yet (the boxes stay on the page until it comes), when it was accepted (the confirmation page is on its way),
    and for a 5xx: an edge proxy may answer 502, 503 or 504 after the origin took the code, so that POST may have been received
    and a second one would send the application twice. One answer is never counted as two prompts.
    """
    submits = [seen for seen in obs.requests if seen.passed and _is_submit_post(seen, obs)]
    if len(submits) < 2:
        return False                    # the first POST's own 428 is the prompt that is already being answered
    status = submits[-1].status
    if status is None or not 400 <= status < 500:
        return False
    return status == 428 or obs.security_code_visible


UNCONFIRMED_NOTE = "Your application may have been sent, but Greenhouse did not show its confirmation page. Look for its email"
UNRECOGNIZED_ADDRESS_NOTE = (
    "The form tried to send to an address the app doesn't recognize, so the app stopped it. "
    "Nothing was sent. Apply from the posting instead"
)
SECURITY_CODE_NOTE = ("Greenhouse asked for the emailed security code, and Submit application was not pressed after it. "
                      "Look for Greenhouse's email")
CODE_REFUSED_NOTE = ("Greenhouse did not accept the security code, and Submit application was not pressed again after that. "
                     "Look for Greenhouse's email")
CHALLENGE_NOTE = "Greenhouse showed a check that wasn't finished. Look for Greenhouse's email"


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
        code_posted = len(submits) > 1
        if code_posted and last_status is not None and last_status >= 500:
            # An edge may answer 502, 503 or 504 after the origin took the code: the application may have been sent, so this is
            # row 6 whether or not the boxes are still on the page, and no second POST is wanted. It does not wait for the clock.
            return Outcome("unconfirmed", 1, UNCONFIRMED_NOTE, evidence=evidence, settled=False)
        if code_wait_over:
            if code_posted and (last_status is None or 200 <= last_status < 400):
                # A code POST went out and its answer never came, or was accepted without the confirmation page: the application
                # may have been sent, so the note that says Submit was not pressed after the code would be untrue.
                return Outcome("unconfirmed", 1, UNCONFIRMED_NOTE, evidence=evidence, settled=False)
            if code_posted:
                # The code POST was answered 428 or 4xx: Greenhouse refused the code. Submit was pressed; it was not pressed again.
                return Outcome("needs_you", 1, CODE_REFUSED_NOTE, detail={"security_code": True}, evidence=evidence)
            return Outcome("needs_you", 1, SECURITY_CODE_NOTE, detail={"security_code": True}, evidence=evidence)
        return Outcome("waiting", 1, detail={"waiting": "security_code"}, evidence=evidence)

    # 3. A challenge frame.
    if obs.challenge_frame:
        return Outcome("needs_you", 1, CHALLENGE_NOTE, evidence=evidence)

    # 4. Greenhouse refused the form (a 4xx other than 428) and it is still there.
    if last_status is not None and 400 <= last_status < 500 and obs.form_present:
        note = f"Greenhouse refused the form (HTTP {last_status})"
        if obs.first_field_error:
            note += f'. Greenhouse marked "{obs.first_field_error}" as wrong'
        return Outcome("failed", 1, note, evidence=evidence)

    # 5. No submit POST passed the route and nothing navigated: nothing that could carry the application left.
    if not submits and not obs.navigated:
        if any(not seen.passed and seen.method.upper() != "GET" for seen in obs.requests):
            note = UNRECOGNIZED_ADDRESS_NOTE
        else:
            note = "The form did not send, so nothing was sent"
            if obs.first_field_error:
                note += f'. Greenhouse marked "{obs.first_field_error}" as wrong'
        return Outcome("failed", 0, note, evidence=evidence, settled=False)

    # 6. Anything else: the POST answered 5xx or never answered, a navigation without a POST, a "thank you" with the form still there.
    return Outcome("unconfirmed", 1, UNCONFIRMED_NOTE, evidence=evidence, settled=False)


