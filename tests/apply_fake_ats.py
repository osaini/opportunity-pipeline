"""An in-process fake Greenhouse, and a fake schema client, for the apply agent's tests.

Nothing here reaches a real employer. ``FakeGreenhouse.route`` is a Playwright
route handler (the same shape as ``tests.test_outreach_forms.Site.route``): the
apply agent's tests pass it as ``route_hook``, so the agent's own request policy
runs first and only what it lets through reaches the fake. It answers the REAL
hostnames, so the adapter's host checks run as in production with no network:

    job-boards.greenhouse.io/examplerobotics/jobs/4000000001        the application form
    job-boards.greenhouse.io/examplerobotics/jobs/4000000001/confirmation
    boards-api.greenhouse.io/v1/boards/examplerobotics/jobs/4000000001?questions=true
    POST boards.greenhouse.io/examplerobotics/jobs/4000000001         the submit path

The company, board token, job ids and every word of the pages are fictional
(tests/fixtures/apply/greenhouse/). The lookup endpoint the form's location
typeahead calls, ``/fake-lookup/location``, is invented for the fake: the real
lookup endpoints are pinned against a live board in M5a. The listing fixtures
keep to the keys the live Job Board API returns. That API gives a demographic
question only an id, and a ``data_compliance`` entry only its type and consent
flags, so the control names and the consent statement the form shows live in
``DEMOGRAPHIC_CONTROL`` and ``DATA_COMPLIANCE_CONTROLS`` below: unconfirmed until
checked against a live board, and M4 must derive them from the page's DOM.

It records every request that reached it, the clicks the page counted on
buttons the agent must never press (``forbidden_clicks``), and whether the
submit path was ever hit. Its Playwright import is lazy, so collecting it never
fails without Playwright.

``scenario`` switches what the page and the submit answer do; see ``SCENARIOS``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from opportunity_app.apply.agent_types import STOPPED

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apply" / "greenhouse"

BOARD_TOKEN = "examplerobotics"
JOB_ID = "4000000001"
LEGACY_JOB_ID = "4000000002"
JOB_HOST = "job-boards.greenhouse.io"
SUBMIT_HOST = "boards.greenhouse.io"
API_HOST = "boards-api.greenhouse.io"
OFFSITE_HOST = "careers.example-robotics.test"
OTHER_JOB_ID = "4000000099"
JOB_PATH = f"/{BOARD_TOKEN}/jobs/{JOB_ID}"
OTHER_JOB_PATH = f"/{BOARD_TOKEN}/jobs/{OTHER_JOB_ID}"
CONFIRMATION_PATH = f"{JOB_PATH}/confirmation"
LEGACY_JOB_PATH = f"/{BOARD_TOKEN}/jobs/{LEGACY_JOB_ID}"
LEGACY_CONFIRMATION_PATH = f"{LEGACY_JOB_PATH}/confirmation"
JOB_URL = f"https://{JOB_HOST}{JOB_PATH}"
LEGACY_JOB_URL = f"https://{JOB_HOST}{LEGACY_JOB_PATH}"
CONFIRMATION_URL = f"https://{JOB_HOST}{CONFIRMATION_PATH}"
SCHEMA_PATH = f"/v1/boards/{BOARD_TOKEN}/jobs/{JOB_ID}"
LOOKUP_PATH = "/fake-lookup/location"
# What the fixture PAGE names, not something the listing says (see the docstring).
DEMOGRAPHIC_CONTROL = "question_{id}"
DATA_COMPLIANCE_CONTROLS = {
    "gdpr": ("gdpr_consent_given", "I consent to Example Robotics storing my application data for 365 days"),
}
LOOKUP_OPTIONS = ("Springfield, Example State, United States", "Springdale, Example State, United States")

SCENARIOS = (
    "confirm",                      # the POST is accepted (a 200 naming the confirmation path: see FakeGreenhouse._submit)
    "security_code",                 # 428, then #security-input-0..7; a second POST with a code gives the confirmation
    "validation_422",
    "server_500",
    "hang",                         # the POST never answers
    "hang_evaluate",                # a page script loops forever after load (the watchdog test)
    "text_only_thanks",             # the POST is answered 200 and the page reloads to "Thank you for applying", form still present
    "confirmation_without_post",    # a page script navigates to the confirmation path without a POST
    "other_path_post",              # the form posts to a path other than submitPath
    "request_submit_during_fill",   # a page script calls requestSubmit() while the agent fills
    "redirect_offsite",             # the posting sends applicants to the employer's own site
    "redirect_other_posting",       # the board sends the posting's address on to another posting of the same board
    "popup_offsite",                # a page script opens the employer's own site in a popup as the page loads
    "navigate_offsite_after_input", # a page script sends the main frame to the employer's own site once the first field is typed into
    "no_portfolio",                 # the optional "Portfolio or project link" field is not drawn
    "closed",                       # the posting is closed: the schema gives 404
    "loader_missing",               # no submitPath in the HTML
    "eager_script",                 # a page script POSTs on every keystroke, like lead-capture scripts
    "eager_get",                    # a page script sends a GET beacon to another host carrying a field value
    "eager_get_greenhouse",         # the same beacon, to a Greenhouse host, also unencoded and wrapped in JSON
    "stray_get",                    # a page script sends a GET to a non-lookup path and to two lookup endpoints on every input, carrying no value
    "lookup_leak",                  # a typeahead's lookup GET also carries another field's value
    "double_submit",                # a page script (or a double click) sends a second POST right after the first
    "captcha_body_leak",            # a page script POSTs a field value to a CAPTCHA endpoint
    "websocket",                    # a page script opens a WebSocket
    "s3_upload",                    # data-allow-s3="true", and attaching makes a PUT to an S3 host
)

_FORM = 'document.getElementById("application-form")'
_SCRIPTS = {
    "eager_script": _FORM + """.addEventListener("input", function (e) {
      fetch("https://analytics.example-robotics.test/collect", {method: "POST", body: JSON.stringify({field: e.target.name, value: e.target.value})}).catch(function () {});
    });""",
    "eager_get": _FORM + """.addEventListener("input", function (e) {
      if (e.target.value) new Image().src = "https://pixel.example-robotics.test/p.gif?v=" + encodeURIComponent(e.target.value);
    });""",
    # Three beacons per input: properly encoded, concatenated with no encoding (a "+" then stays a "+"), and wrapped in JSON (a newline becomes a backslash-n).
    "eager_get_greenhouse": _FORM + """.addEventListener("input", function (e) {
      if (!e.target.value) return;
      new Image().src = "https://job-boards.greenhouse.io/pixel.gif?v=" + encodeURIComponent(e.target.value);
      new Image().src = "https://job-boards.greenhouse.io/pixel.gif?raw=" + e.target.value;
      new Image().src = "https://job-boards.greenhouse.io/pixel.gif?json=" + JSON.stringify({value: e.target.value});
    });""",
    "stray_get": _FORM + """.addEventListener("input", function () {
      fetch("https://job-boards.greenhouse.io/track?e=input").catch(function () {});
      fetch("https://boards-api.greenhouse.io/fake-lookup/location?q=abc").catch(function () {});
      fetch("https://boards-api.greenhouse.io/fake-lookup/school?q=abc").catch(function () {});
    });""",
    "lookup_leak": """window.grLookupUrl = function (kind, text) {
      return "https://boards-api.greenhouse.io/fake-lookup/" + kind + "?q=" + encodeURIComponent(text)
        + "&e=" + encodeURIComponent(document.getElementById("email").value);
    };""",
    "double_submit": """var realFetch = window.fetch;
    window.fetch = function (url, options) {
      var first = realFetch(url, options);
      if (options && options.method === "POST") realFetch(url, options).catch(function () {});
      return first;
    };""",
    "captcha_body_leak": _FORM + """.addEventListener("input", function (e) {
      if (e.target.value) fetch("https://www.google.com/recaptcha/api2/reload?k=fixture", {method: "POST", body: e.target.value}).catch(function () {});
    });""",
    "websocket": """try { new WebSocket("wss://socket.example-robotics.test/live"); } catch (e) {}""",
    "s3_upload": """document.getElementById("resume").addEventListener("change", function (e) {
      fetch("https://example-robotics-uploads.s3.amazonaws.com/resume", {method: "PUT", body: e.target.files[0]}).catch(function () {});
    });""",
    "request_submit_during_fill": """(function () {
      var form = """ + _FORM + """, fired = false;
      form.addEventListener("input", function () {
        if (fired) return;
        fired = true;
        window.grValidate = function () { return ""; };
        setTimeout(function () { form.requestSubmit(); }, 50);
      });
    })();""",
    "confirmation_without_post": """document.addEventListener("submit", function (e) {
      e.preventDefault();
      e.stopPropagation();
      window.location.assign(window.__loader.confirmationPath);
    }, true);""",
    "other_path_post": """window.__loader.submitPath = "/examplerobotics/jobs/4000000001/apply-v2";""",
    "popup_offsite": """window.open("https://careers.example-robotics.test/apply");""",
    "navigate_offsite_after_input": _FORM + """.addEventListener("input", function () {
      setTimeout(function () { window.location.assign("https://careers.example-robotics.test/apply"); }, 30);
    }, {once: true});""",
    "hang_evaluate": """setTimeout(function () { for (;;) {} }, 1500);""",
    "text_only_thanks": """window.grAfterSubmit = function () { window.location.reload(); };""",
}


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> dict[str, Any]:
    return json.loads(fixture_text(name))


@dataclass
class Seen:
    """A request that reached the fake (the agent's own policy had already let it through)."""

    method: str
    host: str
    path: str
    query: str
    resource_type: str = ""
    post_data: str = ""


@dataclass
class Reply:
    status: int
    body: str = ""
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


def form_values(body: str) -> dict[str, list[str]]:
    """The fields of a multipart form body, as the fake reads them: {name: [values]}."""
    found: dict[str, list[str]] = {}
    for name, value in re.findall(r'name="([^"]+)"\r?\n\r?\n(.*?)\r?\n--', body or "", flags=re.S):
        found.setdefault(name, []).append(value)
    return found


class FakeGreenhouse:
    def __init__(self, scenario: str = "confirm") -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}")
        self.scenario = scenario
        self.requests: list[Seen] = []
        self.websockets: list[str] = []
        self.thanks_shown = False       # the text_only_thanks scenario answered a POST: the next page load says thanks

    # --- what the tests ask --------------------------------------------------------------

    def submit_posts(self) -> list[Seen]:
        return [seen for seen in self.requests if seen.method == "POST" and seen.host == SUBMIT_HOST and seen.path in (JOB_PATH, LEGACY_JOB_PATH)]

    @property
    def submit_path_hit(self) -> bool:
        return bool(self.submit_posts())

    def non_get_requests(self) -> list[Seen]:
        return [seen for seen in self.requests if seen.method not in ("GET", "HEAD", "OPTIONS")]

    def requests_to(self, host: str) -> list[Seen]:
        return [seen for seen in self.requests if seen.host == host]

    @staticmethod
    def forbidden_clicks(page: Any) -> int:
        """How many times the page counted a click on a control the agent must never press."""
        return int(page.evaluate("() => window.__forbiddenClicks || 0"))

    # --- Playwright wiring ---------------------------------------------------------------

    def install(self, context: Any) -> None:
        """Serve a whole browser context from the fake (for tests that drive Chromium directly)."""
        context.route("**/*", self.route)
        if hasattr(context, "route_web_socket"):
            def refuse(ws: Any) -> None:
                # Refused by never calling connect_to_server(). ws.close() from inside
                # the handler deadlocks Playwright's sync API (seen with 1.5x).
                self.websockets.append(ws.url)
            context.route_web_socket("**/*", refuse)

    def route(self, route: Any) -> None:
        request = route.request
        try:
            # post_data decodes strictly as UTF-8 and raises on a real PDF's bytes; the fake only reads text fields.
            buffer = request.post_data_buffer
            body = buffer.decode("utf-8", errors="replace") if buffer else ""
        except Exception:  # noqa: BLE001 - no body
            body = ""
        reply = self.answer(request.method, request.url, body, resource_type=getattr(request, "resource_type", ""))
        if reply is None:
            return   # the POST that never answers
        route.fulfill(status=reply.status, headers=reply.headers, content_type=reply.content_type, body=reply.body)

    # --- the fake itself (no Playwright) ---------------------------------------------------

    def answer(self, method: str, url: str, body: str = "", *, resource_type: str = "") -> Reply | None:
        parts = urlsplit(url)
        host, path = (parts.hostname or "").lower(), parts.path
        self.requests.append(Seen(method, host, path, parts.query, resource_type, body))
        cors = {"access-control-allow-origin": f"https://{JOB_HOST}", "access-control-allow-headers": "*"}
        if method == "OPTIONS":
            return Reply(204, headers=cors)
        if host == JOB_HOST and method == "GET":
            return self._page(path)
        if host == OFFSITE_HOST and method == "GET":
            return Reply(200, fixture_text("offsite.html"))
        if host == SUBMIT_HOST and method == "POST" and path == JOB_PATH:
            return self._submit(body, cors)
        if host == API_HOST and method == "GET":
            return self._api(path, parts.query, cors)
        return Reply(200, "{}" if method != "GET" else "", "application/json" if method != "GET" else "text/plain", cors)

    def _page(self, path: str) -> Reply:
        if path == JOB_PATH:
            if self.scenario == "closed":
                return Reply(200, fixture_text("closed.html"))
            if self.scenario == "redirect_offsite":
                # A script, not a 302: the hop after a fulfilled redirect is not routed (see _submit).
                return Reply(200, f'<html><body><script>window.location.replace("https://{OFFSITE_HOST}/apply");</script></body></html>')
            if self.scenario == "redirect_other_posting":
                return Reply(200, f'<html><body><script>window.location.replace("{OTHER_JOB_PATH}");</script></body></html>')
            if self.scenario == "text_only_thanks" and self.thanks_shown:
                return Reply(200, fixture_text("text_only_thanks.html"))
            return Reply(200, self._form_html())
        if path == OTHER_JOB_PATH and self.scenario == "redirect_other_posting":
            return Reply(200, self._form_html())   # the same questions under another posting
        if path == CONFIRMATION_PATH:
            return Reply(200, fixture_text("new_confirmation.html"))
        if path == LEGACY_JOB_PATH:
            return Reply(200, fixture_text("legacy_form.html"))
        if path == LEGACY_CONFIRMATION_PATH:
            return Reply(200, fixture_text("legacy_confirmation.html"))
        return Reply(404, "<h1>Not found</h1>")

    def _form_html(self) -> str:
        html = fixture_text("new_form.html")
        if self.scenario == "loader_missing":
            html = html.replace('"submitPath":"/examplerobotics/jobs/4000000001",', "")
        if self.scenario == "s3_upload":
            html = html.replace('data-allow-s3="false"', 'data-allow-s3="true"')
        if self.scenario == "no_portfolio":
            field_block = re.search(r'[ \t]*<div class="field">\s*<label for="question_4000000102">.*?</div>\s*?\n', html, flags=re.S)
            assert field_block, "the fixture's Portfolio field moved"
            html = html.replace(field_block.group(0), "")
        script = _SCRIPTS.get(self.scenario, "")
        return html.replace("<!--FAKE_SCENARIO-->", f"<script>{script}</script>" if script else "")

    def _api(self, path: str, query: str, cors: dict[str, str]) -> Reply:
        if path == SCHEMA_PATH and parse_qs(query).get("questions") == ["true"] and self.scenario != "closed":
            return Reply(200, fixture_text("schema_new.json"), "application/json", cors)
        if path == LOOKUP_PATH:
            typed = (parse_qs(query).get("q") or [""])[0].lower()
            found = [option for option in LOOKUP_OPTIONS if typed and typed in option.lower()]
            return Reply(200, json.dumps(found), "application/json", cors)
        return Reply(404, json.dumps({"status": 404, "error": "Job not found"}), "application/json", cors)

    def _submit(self, body: str, cors: dict[str, str]) -> Reply | None:
        # Accepted answers are a 200 that names the confirmation path, never a 303: Playwright does not
        # route the hop that follows a fulfilled redirect, so a redirect from here would leave the fake
        # and reach the real network. decide_outcome treats 2xx and 3xx alike (test_apply_checks covers 303).
        confirm = Reply(200, json.dumps({"confirmationPath": CONFIRMATION_PATH}), "application/json", cors)
        if self.scenario == "hang":
            return None
        if self.scenario == "server_500":
            return Reply(500, "{}", "application/json", cors)
        if self.scenario == "validation_422":
            return Reply(422, json.dumps({"errors": {"email": ["is invalid"]}}), "application/json", cors)
        if self.scenario == "security_code":
            typed = "".join(form_values(body).get("security_code", []))
            if typed.strip():
                return confirm
            return Reply(428, fixture_text("security_code_428.json"), "application/json", cors)
        if self.scenario == "text_only_thanks":
            self.thanks_shown = True
            return Reply(200, "{}", "application/json", cors)
        return confirm


class FakeSchemaClient:
    """Serves the fixture listing of the fictional job, or a 404, with no network.

    Tests, the sandbox and the UI suite pass it as ``apply_schema_client_factory``.
    ``fetch(board_token, job_id)`` is the real client's call (apply_schema_client): it returns a
    copy of the parsed listing, or None for a 404. ``any_job`` answers every board and job with
    the fictional listing, which the sandbox uses so each role it seeds has one.
    """

    def __init__(self, *, closed: bool = False, legacy: bool = False, any_job: bool = False) -> None:
        self.closed = closed
        self.legacy = legacy
        self.any_job = any_job
        self.calls: list[tuple[str, str]] = []

    def fetch(self, board_token: str, job_id: str) -> dict[str, Any] | None:
        self.calls.append((board_token, job_id))
        if self.closed:
            return None
        if self.any_job:
            return copy.deepcopy(fixture_json("schema_legacy.json" if self.legacy else "schema_new.json"))
        if board_token != BOARD_TOKEN:
            return None
        if job_id == JOB_ID and not self.legacy:
            return copy.deepcopy(fixture_json("schema_new.json"))
        if job_id == LEGACY_JOB_ID or (self.legacy and job_id == JOB_ID):
            return copy.deepcopy(fixture_json("schema_legacy.json"))
        return None

    __call__ = fetch

    def fetch_url(self, url: str) -> dict[str, Any] | None:
        """The same, from a boards-api URL: /v1/boards/{token}/jobs/{id}."""
        match = re.fullmatch(r"/v1/boards/([^/]+)/jobs/(\d+)", urlsplit(url).path)
        return self.fetch(match.group(1), match.group(2)) if match else None


# Knobs the in-process UI suite may change between tests (a thread-isolated fake reads them when a run starts).
CANNED: dict[str, Any] = {"hang": False, "outcome": "rehearsed", "step_delay": 0.3}
STOPPED_TEXT = STOPPED
NO_OPTIONS_TEXT = "No options came back for what you typed"


def canned_png(width: int = 480, height: int = 320) -> bytes:
    """A small PNG (grey, with a darker band) for the canned rehearsal's picture, built with zlib and struct only."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + bytes([70 if 120 <= y < 170 else 215]) * (width * 3) for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


class FakeApplyAgentFactory:
    """The agent factory of a test or the sandbox: it says a window could open, and its runs are canned.

    ``available()`` is the probe the apply_agent switch's requirement asks (apply_runs.setup_requirement): "" when
    Playwright and Chromium are present, else a sentence. The fake always says they are, so the same tests pass
    with or without Playwright installed. A run opens no browser and no socket: ``CannedAgent`` reports its steps and
    answers from the draft plan. It runs in a thread (``isolation="thread"``) so API and UI tests stay fast; pass
    ``isolation="process"`` to test the spawned child. A knob left as None is read from ``CANNED`` when the run starts.
    """

    def __init__(self, missing: str = "", *, isolation: str = "thread", step_delay: float | None = None,
                 outcome: str | None = None, hang: bool | None = None) -> None:
        self.missing = missing
        self.isolation = isolation
        self.step_delay = step_delay
        self.outcome = outcome
        self.hang = hang

    def available(self) -> str:
        return self.missing

    def __call__(self, **kwargs: Any) -> "CannedAgent":
        return CannedAgent(
            step_delay=CANNED["step_delay"] if self.step_delay is None else self.step_delay,
            outcome=CANNED["outcome"] if self.outcome is None else self.outcome,
            hang=CANNED["hang"] if self.hang is None else self.hang,
            **kwargs,
        )


class CannedAgent:
    """A fictional rehearsal or lookup: no browser, no socket, every sentence value-free."""

    def __init__(self, *, mode: str, run_id: str, screenshot_dir: Path | None, timeouts: Any, on_progress: Any, heartbeat: Any,
                 step_delay: float, outcome: str, hang: bool) -> None:
        self.mode, self.run_id, self.screenshot_dir = mode, run_id, screenshot_dir
        self.on_progress, self.heartbeat = on_progress, heartbeat
        self.step_delay, self.outcome, self.hang = step_delay, outcome, hang

    def __enter__(self) -> "CannedAgent":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def _pause(self, cancelled: Any) -> bool:
        """Wait one step's delay. True when the run was stopped meanwhile."""
        waited = 0.0
        while waited < self.step_delay:
            if cancelled():
                return True
            time.sleep(0.05)
            waited += 0.05
        self.heartbeat()
        return bool(cancelled())

    def _step(self, step: str, text: str, cancelled: Any) -> bool:
        self.on_progress(step, text)
        return self._pause(cancelled)

    def run(self, plan: Any, *, page_url: str, schema: list[Any], files: dict[str, Any], lookup: Any = None, replan: Any = None,
            hand_over: Any = None, cancelled: Any = None) -> Any:
        from opportunity_app.apply import policy as apply_policy
        from opportunity_app.apply.agent_types import PROGRESS_STEPS, RunResult

        cancelled = cancelled or (lambda: False)
        stopped = RunResult("failed", [STOPPED_TEXT])
        self.on_progress("open", PROGRESS_STEPS["open"])
        while self.hang:
            if cancelled():
                return stopped
            time.sleep(0.1)
        if self._pause(cancelled):
            return stopped
        if lookup is not None:
            if self._step("lookup", PROGRESS_STEPS["lookup"].format(question=lookup.question), cancelled):
                return stopped
            found = [option for option in LOOKUP_OPTIONS if lookup.text.casefold() in option.casefold()][:20]
            return RunResult(
                "looked_up", [] if found else [NO_OPTIONS_TEXT], options={lookup.field: found},
                evidence={"page": "application_form_new", "lookups": [{"key": lookup.key, "question": lookup.question}], "refused_total": 0},
            )
        entries = apply_policy.plan_entries(plan)
        filling = sum(1 for entry in entries if entry["disposition"] == "fill")
        for step, text in (("read", PROGRESS_STEPS["read"]), ("fill", PROGRESS_STEPS["fill"].format(n=filling)),
                           ("check", PROGRESS_STEPS["check"]), ("picture", PROGRESS_STEPS["picture"])):
            if self._step(step, text, cancelled):
                return stopped
        masked = [entry["key"] for entry in entries if entry.get("sensitive")]
        shots: list[dict[str, Any]] = []
        if self.screenshot_dir is not None:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshot_dir / f"{self.run_id}-filled.png"
            data = canned_png()
            path.write_bytes(data)
            shots.append({"step": "filled", "path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "masked": masked})
        return RunResult(
            self.outcome, [], plan=entries, plan_hash=getattr(plan, "plan_hash", ""), join_problems=[], check_problems=[],
            screenshots=shots, refused=[{"method": "POST", "host": "analytics.example-robotics.test", "rule": "non_get"}],
            evidence={"page": "application_form_new", "loader": {"submit_path": True, "confirmation_path": True}, "uploads_on_attach": False,
                      "captcha_widget": False, "lookups": [], "submit_path_hit": False, "refused_total": 1,
                      "filled_keys": [entry["key"] for entry in entries if entry["disposition"] == "fill" and entry.get("control") != "file"],
                      "checked_keys": [entry["key"] for entry in entries if entry["disposition"] == "deferred" and entry.get("control") != "file"]},
        )


class HangingAgentFactory:
    """A process-isolated agent that starts a grandchild process and then never yields: only the watchdog can end it."""

    isolation = "process"

    def __init__(self, pid_file: str) -> None:
        self.pid_file = pid_file

    def available(self) -> str:
        return ""

    def __call__(self, **kwargs: Any) -> "HangingAgent":
        return HangingAgent(self.pid_file)


class HangingAgent:
    def __init__(self, pid_file: str) -> None:
        self.pid_file = pid_file

    def __enter__(self) -> "HangingAgent":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def run(self, plan: Any, **kwargs: Any) -> Any:
        grandchild = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        Path(self.pid_file).write_text(f"{os.getpid()} {grandchild.pid}", encoding="utf-8")
        while True:   # no heartbeat, no cancel check, never sleeps
            pass


class CrashingAgentFactory:
    """An agent factory that raises, to see that the runner reports the type name and never the message."""

    def __init__(self, isolation: str = "thread") -> None:
        self.isolation = isolation

    def available(self) -> str:
        return ""

    def __call__(self, **kwargs: Any) -> Any:
        raise RuntimeError("boom")


# --- playing the student, for tests that drive a page (and, in M5a, for ``student_hook``) -----------

def choose(page: Any, name: str, label: str) -> None:
    """Pick one option of the react-select imitation: type to open the menu, then click the option with exactly this text."""
    page.fill(f"#{name}", label)
    page.locator(f"xpath=//div[@class='rs' and .//input[@id='{name}']]//div[@role='option' and normalize-space()='{label}']").click()


def complete_form(page: Any, *, resume: Any = None) -> None:
    """Fill every required field of the fictional form the way the student would. ``resume`` is a Playwright file payload."""
    page.fill("#first_name", "Sam")
    page.fill("#last_name", "Rivera")
    page.fill("#email", "sam.rivera@example.test")
    page.fill("#question_4000000101", "I build small robot arms and would like to learn from the team.")
    choose(page, "question_4000000103", "Controls")
    choose(page, "question_4000000105", "Yes")
    choose(page, "question_4000000106", "No")
    page.check("#question_4000000109")
    page.check("#question_4000000110")
    page.check("input[name='question_4000000111'][value='0']")
    page.check("#gdpr_consent_given")
    if resume is not None:
        page.set_input_files("#resume", resume)


def press_submit(page: Any) -> None:
    page.click("form#application-form button[type=submit]")


def type_security_code(page: Any, code: str = "12345678") -> None:
    for index, character in enumerate(code):
        page.fill(f"#security-input-{index}", character)
