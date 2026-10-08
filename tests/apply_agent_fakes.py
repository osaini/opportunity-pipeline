"""What the apply agent's browser tests share: a factory that serves the fictional Greenhouse, and a plan to fill it with.

Not a test module (nothing here is named ``test*``, so pytest never collects it). ``BrowserAgentFactory`` is a
module-level object with plain attributes, so the runner can pickle it into a spawned child exactly as it does the real
factory; its agent serves every page from ``FakeGreenhouse`` through the agent's own ``route_hook``, so the agent's request
policy runs first and only what it lets through reaches the fake. It writes what the fake saw to ``record_path`` when the
agent closes, because a child process cannot hand a Python object back.

``full_sources`` and ``fixture_replan`` build the plan the way the runner's parent does: from the fixture's own listing,
a profile, saved answers, a confirmed location label, the sensitive store's entries and a confirmed résumé.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers_apply
from apply_fake_ats import (
    API_HOST, JOB_URL, LOOKUP_OPTIONS, FakeGreenhouse, FakeLever, Reply, choose, fixture_json, fixture_text, press_submit, type_security_code,
)

from opportunity_app.apply import policy as apply_policy
from opportunity_app.apply.agent import ApplyAgent, GreenhouseAdapter
from opportunity_app.apply.agent_types import ApplyTimeouts, FilePayload
from opportunity_app.apply.checks import Endpoint
from opportunity_app.apply.greenhouse import ADAPTER_VERSION
from opportunity_app.apply.lever_adapter import LeverAdapter
from opportunity_app.apply.sensitive import STORABLE

COMPANY = helpers_apply.COMPANY
COMPANY_KEY = "example robotics"
RESUME_NAME = "Sam Rivera Resume.pdf"
RESUME_BYTES = b"%PDF-1.4\n% a fictional resume for the apply agent's tests\n"
PORTFOLIO = "https://portfolio.example.test/sam-rivera"
WHY = "I build small robot arms and would like to learn from the team."
FIXTURE_LOOKUP = (Endpoint(API_HOST, "/fake-lookup/location", "location"),)
# No settling waits, and a short wait for a lookup's options, so a test that expects no option does not sit out three seconds.
TEST_TIMEOUTS = ApplyTimeouts(settle_s=0, between_fields_s=0, choice_settle_s=1.0, navigation_s=15)
# A handoff's waits are short enough to sit out in a test: the student's turn, the reader's window, the code wait, the poll.
HANDOFF_TIMEOUTS = ApplyTimeouts(
    settle_s=0, between_fields_s=0, choice_settle_s=1.0, navigation_s=15, fill_s=60, handoff_s=20, security_code_s=6, code_read_s=4,
    code_poll_s=0.5, code_reply_s=1, heartbeat_s=0.5, outcome_s=4,
)
CONSENT = "I consent to Example Robotics storing my application data for 365 days"


def resume_payload(data: bytes = RESUME_BYTES, name: str = RESUME_NAME) -> FilePayload:
    return FilePayload(name=name, mime_type="application/pdf", buffer=data, sha256=hashlib.sha256(RESUME_BYTES).hexdigest())


LETTER_TEXT = "# Cover letter\n\nDear Hiring Team,\n\nI would like to work on your robot arms.\n\nSincerely,\nSam Rivera\n"
LETTER_BYTES = b"%PDF-1.4\n% a fictional cover letter for the apply agent's tests\n"
LETTER_NAME = "Example-Robotics-Controls-Intern-cover_letter-v2.pdf"


def letter_source(text: str = LETTER_TEXT, *, version: int = 2, document_id: str = "doc-1") -> dict[str, Any]:
    """What ``policy.cover_letter_for`` answers for a role whose latest cover letter is approved."""
    return {"document_id": document_id, "version": version, "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "content": text,
            "file_name": LETTER_NAME, "problem_kind": "", "problem": ""}


def letter_payload(data: bytes = LETTER_BYTES, name: str = LETTER_NAME, text: str = LETTER_TEXT) -> FilePayload:
    """The approved letter's PDF as the runner reads it: its own hash, and the hash of the text it was rendered from."""
    return FilePayload(name=name, mime_type="application/pdf", buffer=data, sha256=hashlib.sha256(data).hexdigest(),
                       content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


def with_letter(sources: apply_policy.Sources, **kwargs: Any) -> apply_policy.Sources:
    return dataclasses.replace(sources, cover_letter=letter_source(**kwargs))


def schema_with_required_letter() -> list[apply_policy.SchemaField]:
    listing = fixture_json("schema_new.json")
    next(block for block in listing["questions"] if block["label"] == "Cover Letter")["required"] = True
    return apply_policy.parse_schema(listing)


def fixture_schema() -> list[apply_policy.SchemaField]:
    return apply_policy.parse_schema(fixture_json("schema_new.json"))


def _sensitive(schema: list[apply_policy.SchemaField], category: str, name: str, text: str, kind: str = "option", company: str = "") -> dict[str, Any]:
    label = next(item.label for item in schema if item.name == name)
    return helpers_apply.entry(category, label, text, kind, company, entry_id=f"store-{name}")


def full_sources(*, location: str | None = LOOKUP_OPTIONS[0], resume_data: bytes = RESUME_BYTES, extra_answers: tuple[dict[str, Any], ...] = ()) -> apply_policy.Sources:
    """Everything the fictional form's required fields (and a few optional ones) can be answered from."""
    schema = fixture_schema()
    resume = dict(helpers_apply.RESUME_OK, sha256=hashlib.sha256(resume_data).hexdigest(), original_name=RESUME_NAME)
    store = helpers_apply.Store(
        _sensitive(schema, "work_authorization", "question_4000000105", "Yes", company=COMPANY_KEY),
        _sensitive(schema, "sponsorship", "question_4000000106", "No", company=COMPANY_KEY),
        _sensitive(schema, "acknowledgment", "question_4000000109", "checked", "checkbox", COMPANY_KEY),
        _sensitive(schema, "acknowledgment", "question_4000000110", "checked", "checkbox", COMPANY_KEY),
        helpers_apply.entry("consent", CONSENT, "checked", "checkbox", COMPANY_KEY, "store-gdpr"),
        _sensitive(schema, "eeo_gender", "gender", "Decline To Self Identify"),
        _sensitive(schema, "eeo_hispanic", "hispanic_ethnicity", "Decline To Self Identify"),
        _sensitive(schema, "eeo_veteran", "veteran_status", "I don't wish to answer"),
        _sensitive(schema, "eeo_disability", "disability_status", "I do not want to answer"),
    )
    answers = [
        helpers_apply.answer("Why do you want to work at Example Robotics?", WHY),
        helpers_apply.answer("Which team are you most interested in?", "Controls"),
        helpers_apply.answer("Have you previously worked at Example Robotics?", "No"),
        helpers_apply.answer("Portfolio or project link", PORTFOLIO),
        *extra_answers,
    ]
    return helpers_apply.sources(
        answers=answers, labels={"location": location} if location else {}, resume=resume,
        allowed=frozenset(STORABLE), store=store,
    )


def fixture_replan(
    sources: apply_policy.Sources, *, schema: list[apply_policy.SchemaField] | None = None, mode: str = "rehearse",
    label_checkboxes: bool = False,
) -> Callable[[list[dict[str, Any]], bool], Any]:
    """The parent's answer to the child's scan: the plan from the page's own fields, as ``ApplyRunner`` builds it.

    The extension engine reads no question for a checkbox whose label is inline (the fixture's privacy and accuracy boxes), so the
    join reports a "wording mismatch" and a handoff leaves each such box for the student. ``label_checkboxes`` gives those scan
    entries the label the plan already uses, as an engine that read it would, so a test can follow a ticked consent box.
    """
    listing = schema if schema is not None else fixture_schema()

    def replan(scan: list[dict[str, Any]], uploads_on_attach: bool) -> Any:
        fields = apply_policy.with_page_labels(listing, scan)
        if label_checkboxes:
            labels = {item.name: item.label for item in fields}
            scan = [dict(item, question=labels.get(item.get("name") or item.get("id"), "")) if item.get("type") == "checkbox" and not item.get("question") else item
                    for item in scan]
        return apply_policy.build_plan(
            fields, scan, sources, COMPANY, mode,
            ats_name="Greenhouse", canonical_url=JOB_URL, adapter_version=ADAPTER_VERSION, uploads_on_attach=uploads_on_attach,
        )

    return replan


def draft_plan(sources: apply_policy.Sources, *, schema: list[apply_policy.SchemaField] | None = None, mode: str = "rehearse") -> Any:
    """The plan from the listing alone: what the child is handed before it has read the page."""
    return apply_policy.build_plan(
        schema if schema is not None else fixture_schema(), None, sources, COMPANY, mode,
        ats_name="Greenhouse", canonical_url=JOB_URL, adapter_version=ADAPTER_VERSION,
    )


class RecordingAgent(ApplyAgent):
    """An ``ApplyAgent`` that serves the fictional Greenhouse and writes what the fake saw when it closes."""

    def __init__(self, *, fake: FakeGreenhouse, record_path: str = "", **kwargs: Any) -> None:
        super().__init__(route_hook=fake.route, headless=True, **kwargs)
        self.fake = fake
        self.record_path = record_path
        self._forbidden: int | None = None

    def _close_browser(self) -> bool:
        # A handoff closes its own window before it returns, so the page is counted first.
        self._count_forbidden()
        return super()._close_browser()

    def _student(self, step: str) -> None:
        release = getattr(self.fake, "release_due", None)
        if release is not None:
            try:
                release()
            except Exception:  # noqa: BLE001 - a page that went away
                pass
        super()._student(step)

    def _count_forbidden(self) -> None:
        try:
            self._forbidden = FakeGreenhouse.forbidden_clicks(self._page)
        except Exception:  # noqa: BLE001 - the page is gone
            pass

    def record(self) -> dict[str, Any]:
        self._count_forbidden()
        forbidden = -1 if self._forbidden is None else self._forbidden
        return {
            "post_times": list(getattr(self.fake, "post_times", [])),
            "requests": [vars(seen) for seen in self.fake.requests],
            "websockets": list(self.fake.websockets),
            "submit_path_hit": self.fake.submit_path_hit,
            "non_get": [vars(seen) for seen in self.fake.non_get_requests()],
            "forbidden_clicks": forbidden,
        }

    def __exit__(self, *exc: Any) -> None:
        if self.record_path:
            try:
                Path(self.record_path).write_text(json.dumps(self.record()), encoding="utf-8")
            except Exception:  # noqa: BLE001 - a record that cannot be written changes nothing
                pass
        super().__exit__(*exc)


class BrowserAgentFactory:
    """The agent factory of the browser tests. Picklable, so ``isolation="process"`` spawns a real child.

    ``student`` names one of ``STUDENTS`` (a module-level function, so a spawned child finds it by name): the test's stand-in for
    the person in the window during a handoff. ``mode`` overrides the job's mode when set.
    """

    def __init__(
        self, scenario: str = "confirm", record_path: str = "", lookup_endpoints: tuple[Endpoint, ...] = FIXTURE_LOOKUP,
        isolation: str = "process", mode: str = "", student: str = "", letter_required: bool = False,
    ) -> None:
        self.scenario = scenario
        self.record_path = record_path
        self.lookup_endpoints = tuple(lookup_endpoints)
        self.isolation = isolation
        self.mode = mode
        self.student = student
        self.letter_required = letter_required   # the page marks its cover letter required, as the listing does

    def available(self) -> str:
        return ""

    def __call__(self, *, mode: str, run_id: str, screenshot_dir: Path | None, timeouts: ApplyTimeouts,
                 on_progress: Callable[[str, str], None], heartbeat: Callable[[], None], ats: str = "greenhouse") -> RecordingAgent:
        mode = self.mode or mode
        fake = HandoffGreenhouse(self.scenario)
        fake.letter_required = self.letter_required
        return RecordingAgent(
            fake=fake, record_path=self.record_path, mode=mode, adapter=GreenhouseAdapter(),
            run_id=run_id, screenshot_dir=screenshot_dir,
            timeouts=(HANDOFF_TIMEOUTS if mode == "handoff" else TEST_TIMEOUTS) if timeouts == ApplyTimeouts() else timeouts,
            lookup_endpoints=self.lookup_endpoints, on_progress=on_progress, heartbeat=heartbeat,
            student_hook=STUDENTS.get(self.student),
        )


# --- Finish in browser: the fictional Greenhouse's extra scenarios, the student, and the parent's end of the pipe ----------------

HANDOFF_SCENARIOS = (
    "security_code_autosubmit",   # the code widget submits the form by itself when the 8th box is filled
    "upload_on_attach_unmarked",  # attaching the resume POSTs it to an S3-like host, with no data-allow-s3 marker
    "upload_cover_letter",        # attaching a cover letter (the student's own act, in the window) uploads it
    "loader_other_host",          # submitPath is an address on job-boards.greenhouse.io, not on the submit host
    "error_echoes_input",         # the 422 marks the email field and its error text repeats what was typed
    "telemetry",                  # Snowplow-style usage reporting to c.spl.greenhouse.io on every input, POST and GET
    "security_code_twice",        # the code is asked for again after the first code POST
    "security_code_slow",         # the code POST is answered two seconds after it arrives (a real submit takes one to three)
    "security_code_retry",        # the code widget submits on the 8th box and again every 2.5 s until the page moves on
    "security_code_retry_twice",  # the retrying widget above, and the code is asked for again after the first code POST
    "security_code_forger",       # the code widget fakes the student's press (a script click, a made-up event, every function on window), then submits
    "consent_box_reworded",       # the accuracy box on the form says something other than the listing does (marketing, not accuracy)
    "tracker_on_submit",          # a tracker reports the Submit click to another address while the form's own submission goes on as usual
    "form_posts_elsewhere",       # the form sends its application to an address the app does not recognize (the page's own submit listener)
    "challenge",                  # the answer to the POST is a visible reCAPTCHA challenge frame
    "bframe_hidden",              # a reCAPTCHA frame is loaded but invisible, and the POST is refused with a 422
)
CODE_ANSWER_DELAY_S = 2.0
_ALIASES = {
    "security_code_autosubmit": "security_code", "error_echoes_input": "validation_422", "security_code_twice": "security_code",
    "security_code_slow": "security_code", "security_code_retry": "security_code", "security_code_retry_twice": "security_code", "security_code_forger": "security_code",
    "bframe_hidden": "validation_422",
}
_FORM = 'document.getElementById("application-form")'
_UPLOAD = """fetch("https://example-robotics-uploads.s3.amazonaws.com/%s", {method: "PUT", body: e.target.files[0]}).catch(function () {});"""
_HANDOFF_SCRIPTS = {
    "security_code_autosubmit": """document.getElementById("security-code").addEventListener("input", function () {
      var boxes = document.querySelectorAll("#security-code input");
      if (Array.prototype.every.call(boxes, function (box) { return box.value; })) %s.requestSubmit();
    });""" % _FORM,
    "security_code_retry": """(function () {
      var form = %s, retry = null;
      document.getElementById("security-code").addEventListener("input", function () {
        var boxes = document.querySelectorAll("#security-code input");
        if (retry || !Array.prototype.every.call(boxes, function (box) { return box.value; })) return;
        form.requestSubmit();
        retry = setInterval(function () { form.requestSubmit(); }, 2500);
      });
    })();""" % _FORM,
    "security_code_retry_twice": """(function () {
      var form = %s, retry = null;
      document.getElementById("security-code").addEventListener("input", function () {
        var boxes = document.querySelectorAll("#security-code input");
        if (retry || !Array.prototype.every.call(boxes, function (box) { return box.value; })) return;
        form.requestSubmit();
        retry = setInterval(function () { form.requestSubmit(); }, 2500);
      });
    })();""" % _FORM,
    "security_code_forger": """(function () {
      var form = %s, done = false;
      document.getElementById("security-code").addEventListener("input", function () {
        var boxes = document.querySelectorAll("#security-code input");
        if (done || !Array.prototype.every.call(boxes, function (box) { return box.value; })) return;
        done = true;
        // After the app has finished typing: a forgery during the typing is refused for that reason alone.
        setTimeout(forge, 1500);
      });
      function forge() {
        var button = form.querySelector("button[type=submit]"), tried = [];
        try { button.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true})); tried.push("dispatch"); } catch (error) { /* refused */ }
        try { button.click(); tried.push("click"); } catch (error) { /* refused */ }
        try { button.dispatchEvent(new PointerEvent("click", {bubbles: true, cancelable: true, isTrusted: true})); tried.push("pointer"); } catch (error) { /* refused */ }
        // Every function the page can see that a fresh frame does not have: a binding the app left in this world would show here.
        var fresh = document.createElement("iframe");
        document.body.appendChild(fresh);
        var known = Object.getOwnPropertyNames(fresh.contentWindow);
        document.body.removeChild(fresh);
        var extra = Object.getOwnPropertyNames(window).filter(function (name) { return known.indexOf(name) < 0 && typeof window[name] === "function"; });
        extra.forEach(function (name) {
          [JSON.stringify({trusted: true, hit: true}), "1", true, {trusted: true}].forEach(function (argument) { try { window[name](argument); } catch (error) { /* refused */ } });
        });
        window.__forged = {tried: tried, functions: extra};
        form.requestSubmit();
      }
    })();""" % _FORM,
    "tracker_on_submit": """document.addEventListener("submit", function () {
      fetch("https://events.example-analytics.test/collect", {method: "POST", body: new URLSearchParams({event: "apply_submit"})}).catch(function () {});
    }, true);""",
    "form_posts_elsewhere": """document.addEventListener("submit", function (e) {
      e.preventDefault();
      e.stopImmediatePropagation();
      fetch("https://apply.example-robotics.test/submit", {method: "POST", body: new URLSearchParams(new FormData(%s))}).catch(function () {});
    }, true);""" % _FORM,
    "upload_on_attach_unmarked": """document.getElementById("resume").addEventListener("change", function (e) { %s });""" % (_UPLOAD % "resume"),
    "upload_cover_letter": """document.getElementById("cover_letter").addEventListener("change", function (e) { %s });""" % (_UPLOAD % "cover"),
    "error_echoes_input": """window.grAfterSubmit = function (response) {
      if (response.ok) { window.location.assign(window.__loader.confirmationPath); return; }
      var email = document.getElementById("email");
      email.setAttribute("aria-invalid", "true");
      var box = document.getElementById("form-error");
      box.textContent = "Email " + email.value + " is invalid";
      box.hidden = false;
    };""",
    "challenge": """window.grAfterSubmit = function () {
      var frame = document.createElement("iframe");
      frame.src = "https://www.recaptcha.net/recaptcha/enterprise/bframe?hl=en";
      frame.style.width = "300px"; frame.style.height = "300px";
      document.body.appendChild(frame);
    };""",
    "bframe_hidden": """(function () {
      var frame = document.createElement("iframe");
      frame.src = "https://www.recaptcha.net/recaptcha/enterprise/bframe?hl=en";
      frame.style.cssText = "visibility:hidden;width:300px;height:300px";
      document.body.appendChild(frame);
    })();""",
    "telemetry": """%s.addEventListener("input", function (e) {
      var payload = btoa(unescape(encodeURIComponent(JSON.stringify({field: e.target.name, value: e.target.value}))));
      fetch("https://c.spl.greenhouse.io/com.snowplowanalytics.snowplow/tp2", {method: "POST", body: JSON.stringify({ue_px: payload})}).catch(function () {});
      new Image().src = "https://c.spl.greenhouse.io/i?e=pv&u=" + encodeURIComponent(window.location.href);
    });""" % _FORM,
}


class HandoffGreenhouse(FakeGreenhouse):
    """``FakeGreenhouse`` plus the scenarios of a handoff (the base file is the fake's own; these only add pages and scripts)."""

    def __init__(self, scenario: str = "confirm") -> None:
        self.posts = 0
        self.post_times: list[float] = []   # time.monotonic() when each submit POST reached the fake (the clock is system-wide)
        self.held: list[tuple[float, Any, Any]] = []   # (when, route, reply): answers the slow scenario has not given yet
        if scenario in HANDOFF_SCENARIOS:
            super().__init__("confirm")
            self.scenario = scenario
        else:
            super().__init__(scenario)

    def answer(self, method: str, url: str, body: str = "", *, resource_type: str = "") -> Any:
        if method == "POST" and url.split("?")[0].endswith("/examplerobotics/jobs/4000000001") and "boards.greenhouse.io" in url:
            self.post_times.append(time.monotonic())
        return super().answer(method, url, body, resource_type=resource_type)

    def route(self, route: Any) -> None:
        """The slow scenario holds the answer to the code POST (every submit POST after the first) and gives it later: ``release_due``."""
        request = route.request
        if (self.scenario == "security_code_slow" and request.method == "POST" and self.posts >= 1
                and request.url.split("?")[0].endswith("/examplerobotics/jobs/4000000001")):
            try:
                buffer = request.post_data_buffer
                body = buffer.decode("utf-8", errors="replace") if buffer else ""
            except Exception:  # noqa: BLE001 - no body
                body = ""
            reply = self.answer(request.method, request.url, body, resource_type=getattr(request, "resource_type", ""))
            if reply is not None:
                self.held.append((time.monotonic() + CODE_ANSWER_DELAY_S, route, reply))
            return
        super().route(route)

    def release_due(self) -> None:
        """Answer every held POST whose time has come. Called from the agent's own wait loop (one thread, so no lock)."""
        for item in list(self.held):
            if time.monotonic() >= item[0]:
                self.held.remove(item)
                _due, route, reply = item
                route.fulfill(status=reply.status, headers=reply.headers, content_type=reply.content_type, body=reply.body)

    def _form_html(self) -> str:
        if self.scenario not in HANDOFF_SCENARIOS:
            return super()._form_html()
        html = fixture_text("new_form.html")
        if self.scenario == "loader_other_host":
            html = html.replace(
                '"submitPath":"/examplerobotics/jobs/4000000001",', '"submitPath":"https://job-boards.greenhouse.io/examplerobotics/jobs/4000000001",',
            )
        if self.scenario == "consent_box_reworded":
            html = html.replace(
                "I certify that the information I have provided is accurate</label>",
                "I agree that Example Robotics may share my application with marketing partners</label>",
            )
        script = _HANDOFF_SCRIPTS.get(self.scenario, "")
        return html.replace("<!--FAKE_SCENARIO-->", f"<script>{script}</script>" if script else "")

    def _submit(self, body: str, cors: dict[str, str]) -> Any:
        real = self.scenario
        self.posts += 1
        if real in ("security_code_twice", "security_code_retry_twice") and self.posts <= 2:
            return Reply(428, fixture_text("security_code_428.json"), "application/json", {"access-control-allow-origin": "https://job-boards.greenhouse.io"})
        self.scenario = _ALIASES.get(real, real)
        try:
            return super()._submit(body, cors)
        finally:
            self.scenario = real


def _once(page: Any, name: str) -> bool:
    """True the first time it is asked for this name on this page (the student's steps happen once, not on every tick)."""
    return not page.evaluate(
        "(name) => { window.__done = window.__done || {}; const had = !!window.__done[name]; window.__done[name] = true; return had; }", name,
    )


def _shown(page: Any, name: str) -> bool:
    return page.locator(f"xpath=//div[@class='rs' and .//input[@id='{name}']]//*[contains(@class,'single-value')]").count() > 0


def student_completes(page: Any) -> None:
    """Fill whatever the agent left empty, the way the person would (a field the app already filled is left alone)."""
    for name, text in (("first_name", "Sam"), ("last_name", "Rivera"), ("email", "sam.rivera@example.test"),
                       ("question_4000000101", "I build small robot arms and would like to learn from the team.")):
        if not page.input_value(f"#{name}"):
            page.fill(f"#{name}", text)
    for name, label in (("question_4000000103", "Controls"), ("question_4000000105", "Yes"), ("question_4000000106", "No")):
        if not _shown(page, name):
            choose(page, name, label)
    for selector in ("#question_4000000109", "#question_4000000110", "#gdpr_consent_given"):
        if not page.is_checked(selector):
            page.check(selector)
    if not page.locator("input[name='question_4000000111']:checked").count():
        page.check("input[name='question_4000000111'][value='0']")


def complete_and_submit(page: Any, step: str) -> None:
    if step == "handoff" and _once(page, "submit"):
        student_completes(page)
        press_submit(page)


def do_nothing(page: Any, step: str) -> None:
    return None


def close_window(page: Any, step: str) -> None:
    if step == "handoff" and _once(page, "close"):
        page.close()


def press_and_close(page: Any, step: str) -> None:
    if step == "handoff" and _once(page, "submit"):
        student_completes(page)
        press_submit(page)
        page.close()


def type_code_and_submit(page: Any, step: str) -> None:
    """The first turn is complete_and_submit. At the code prompt: type the code if the boxes are empty, then press Submit."""
    if step == "handoff":
        complete_and_submit(page, step)
    elif step == "security_code" and _once(page, "code"):
        if not page.input_value("#security-input-0"):
            type_security_code(page)
        page.wait_for_timeout(2300)   # a person takes longer than the app's guard against a widget that sends by itself
        press_submit(page)


def press_when_typed(page: Any, step: str) -> None:
    """The first turn is complete_and_submit. At a code prompt: when the boxes hold a code (the app typed it), wait out the app's
    guard against a widget that sends by itself, then press Submit. Never types anything."""
    if step == "handoff":
        complete_and_submit(page, step)
    elif step == "security_code" and page.locator("#security-input-0").count() and page.input_value("#security-input-0"):
        last = page.evaluate("() => window.__lastPress || 0")
        if page.evaluate("() => Date.now()") - last > 2600:
            page.wait_for_timeout(2300)
            page.evaluate("() => { window.__lastPress = Date.now(); }")
            press_submit(page)


def type_code_late(page: Any, step: str) -> None:
    """The first turn is complete_and_submit. At a code prompt: do nothing for 1.5 s (the app's own reader gets its answer in), then type
    the code if the boxes are empty and press Submit, the way a person who got the email later would."""
    if step == "handoff":
        complete_and_submit(page, step)
    elif step == "security_code":
        first = page.evaluate("() => { window.__codeSeen = window.__codeSeen || Date.now(); return window.__codeSeen; }")
        if page.evaluate("() => Date.now()") - first > 1500 and _once(page, "late_code"):
            if not page.input_value("#security-input-0"):
                type_security_code(page)
            page.wait_for_timeout(2300)
            press_submit(page)


PRESSES: list[float] = []   # time.monotonic() just before each press the students below make at a code prompt (a test clears it)


def press_after_the_widget_gave_up(page: Any, step: str) -> None:
    """The first turn is complete_and_submit. At a code prompt: when the boxes hold a code, wait 3.4 s (the retrying widget has tried twice by
    then), then press Submit once, with a real mouse click. Never types anything."""
    if step == "handoff":
        complete_and_submit(page, step)
    elif step == "security_code" and page.locator("#security-input-0").count() and page.input_value("#security-input-0"):
        first = page.evaluate("() => { window.__armedAt = window.__armedAt || Date.now(); return window.__armedAt; }")
        if page.evaluate("() => Date.now()") - first > 3400 and _once(page, "late_press"):
            PRESSES.append(time.monotonic())
            press_submit(page)


def press_at_each_prompt_after_the_widget_gave_up(page: Any, step: str) -> None:
    """Like ``press_after_the_widget_gave_up``, but for a form that asks for the code twice: the same press, 3.4 s after the last one (or after
    the first prompt), up to twice, a real mouse click each time. The boxes keep what the app typed; it types nothing."""
    if step == "handoff":
        complete_and_submit(page, step)
    elif step == "security_code" and page.locator("#security-input-0").count() and page.input_value("#security-input-0"):
        since = page.evaluate("() => { window.__armedAt = window.__armedAt || Date.now(); return window.__lastPressAt || window.__armedAt; }")
        count = page.evaluate("() => window.__pressCount || 0")
        if count < 2 and page.evaluate("() => Date.now()") - since > 3400:
            page.evaluate("() => { window.__lastPressAt = Date.now(); window.__pressCount = (window.__pressCount || 0) + 1; }")
            PRESSES.append(time.monotonic())
            press_submit(page)


def die_in_the_turn(page: Any, step: str) -> None:
    """The driver process ends without closing anything (a crash): the browser it started is left for the parent to find."""
    if step == "handoff" and _once(page, "die"):
        os._exit(3)


def _freeze(pid: int) -> None:
    """Stop a process where it is (it cannot notice that its pipe closed), so it outlives its driver."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x800, False, pid)          # PROCESS_SUSPEND_RESUME
        if handle:
            ctypes.WinDLL("ntdll").NtSuspendProcess(ctypes.c_void_p(handle))
            kernel32.CloseHandle(handle)
        return
    import signal

    os.kill(pid, signal.SIGSTOP)


def kill_the_driver_then_die(page: Any, step: str) -> None:
    """Chromium and its helpers are frozen, the Playwright driver (the one process below this child) is killed by pid, and then the
    child ends: a browser that outlives its driver, for the parent's by-pid kill to find.

    ``die_in_the_turn`` ends only the child, and the driver then closes Chromium itself; and a Chromium whose driver was killed exits
    on its own when its pipe closes, so it is frozen first.
    """
    if step == "handoff" and _once(page, "die"):
        from opportunity_app.apply import runner as apply_runner

        table = apply_runner._process_table() or {}
        # The same identity check the runner makes: on Windows the table keeps a dead parent's pid, so a process whose old parent held
        # the pid this child now has would be taken for a driver (killed) or a Chromium helper (frozen for good). Only what started
        # at or after its parent counts.
        check = apply_runner._start_check()
        below = apply_runner._descendants_of(table, os.getpid(), started=check)
        drivers = [pid for pid in below if table.get(pid) == os.getpid()]
        for driver in drivers:
            for pid in apply_runner._descendants_of(table, driver, started=check):
                _freeze(pid)
        for driver in drivers:
            apply_runner._kill_pid(driver)
        os._exit(3)


def attach_and_upload(page: Any, step: str) -> None:
    if step == "handoff" and _once(page, "attach"):
        page.set_input_files("#cover_letter", {"name": "letter.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4 a fictional letter"})


STUDENTS = {
    "complete_and_submit": complete_and_submit, "do_nothing": do_nothing, "close_window": close_window, "press_and_close": press_and_close,
    "type_code_and_submit": type_code_and_submit, "press_after_the_widget_gave_up": press_after_the_widget_gave_up, "press_at_each_prompt": press_at_each_prompt_after_the_widget_gave_up, "type_code_late": type_code_late, "attach_and_upload": attach_and_upload, "press_when_typed": press_when_typed,
    "die_in_the_turn": die_in_the_turn, "kill_the_driver_then_die": kill_the_driver_then_die,
}


class FakeLink:
    """The parent's end of a handoff for a test that runs the agent in a thread: scripted code replies, and a record of everything asked.

    ``replies`` is what the reader answers to each ask, in order (a ``{"status": ...}`` dict, or ``(seconds, dict)`` for a reply that
    arrives that long after the ask); once it runs out every ask is answered ``fallback``. The link counts it when the agent breaks
    a rule the real channel keeps: an ask while the last one has no reply yet (``reasked``).
    """

    def __init__(self, replies: tuple[Any, ...] = ()) -> None:
        self.replies = list(replies)
        self.ready_messages: list[dict[str, Any]] = []
        self.asks: list[int] = []
        self.results: list[tuple[int, bool, str]] = []
        self.front_requests = 0
        self.fronts_taken = 0
        self.gone = False
        self.reasked = 0
        self.dropped = 0
        self.abandoned: list[int] = []   # the asks the agent stopped waiting for
        self._ids = 0
        self._pending: int | None = None
        self._arriving: dict[int, tuple[float, dict[str, Any]]] = {}

    def ready(self, message: dict[str, Any]) -> None:
        self.ready_messages.append(message)

    def ask_code(self) -> int:
        if self._pending is not None:
            self.reasked += 1
        self._ids += 1
        ident, self._pending = self._ids, self._ids
        self.asks.append(ident)
        scripted = self.replies.pop(0) if self.replies else {"status": "fallback", "reason": "none"}
        delay, reply = scripted if isinstance(scripted, tuple) else (0.0, scripted)
        self._arriving[ident] = (time.monotonic() + delay, dict(reply))
        return ident

    def code_reply(self, ident: int) -> dict[str, Any] | None:
        if ident != self._pending:
            self.dropped += 1
            return {"status": "fallback", "reason": "not_current"}
        due = self._arriving.get(ident)
        if due is None or time.monotonic() < due[0]:
            return None
        del self._arriving[ident]
        self._pending = None
        return due[1]

    def abandon_code(self) -> None:
        """What the real channel does: the outstanding ask is forgotten, and a reply that comes for it later is dropped."""
        if self._pending is not None:
            self.abandoned.append(self._pending)
            self._arriving.pop(self._pending, None)
            self._pending = None

    def code_result(self, ident: int, typed: bool, reason: str = "") -> None:
        self.results.append((ident, typed, reason))

    def front_requested(self) -> bool:
        if self.fronts_taken < self.front_requests:
            self.fronts_taken += 1
            return True
        return False

    def parent_gone(self) -> bool:
        return self.gone


# --- Finish in browser on Lever: the fictional board, the student, and the agent that records what the board saw ---------------------------------------

LEVER_STUDENT_FILE = b"%PDF-1.4 a fictional resume the student picks in the window"
LEVER_STUDENT_SHA = hashlib.sha256(LEVER_STUDENT_FILE).hexdigest()
LEVER_REQUIRED_VALUES = {"name": "Sam Rivera", "email": "sam.rivera@example.test", "phone": "555-0100", "org": "Fictional Employer"}


class TimedLever(FakeLever):
    """FakeLever that notes when (``time.monotonic()``, which the parent and a child on one machine share) each application POST reached it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.post_times: list[float] = []

    def answer(self, method: str, url: str, body: bytes = b"", **kwargs: Any) -> Any:
        reply = super().answer(method, url, body, **kwargs)
        if len(self.apply_posts()) > len(self.post_times):
            self.post_times.append(time.monotonic())
        return reply


def lever_student_completes(page: Any) -> None:
    """Fill what the app left empty and the form requires, the way the person would (a field the app filled is left alone)."""
    for name, value in LEVER_REQUIRED_VALUES.items():
        box = page.locator(f'form#application-form input[name="{name}"]')
        if box.count() and not box.first.input_value():
            box.first.fill(value)


def lever_press_submit(page: Any) -> None:
    page.click("#btn-submit")


def lever_attach_a_file(page: Any) -> None:
    """The student chooses a file in the form's file box: a path, so the browser's own (trusted) events say so, as a picked file's do."""
    path = Path(tempfile.gettempdir()) / f"lever-e2e-student-resume-{os.getpid()}.pdf"
    path.write_bytes(LEVER_STUDENT_FILE)
    page.set_input_files('input[name="resume"]', str(path))


def lever_complete_and_submit(page: Any, step: str) -> None:
    if step == "handoff" and _once(page, "submit"):
        lever_student_completes(page)
        lever_press_submit(page)


def lever_attach_complete_and_submit(page: Any, step: str) -> None:
    """Attach a file; once the page has finished reading it (the 'working' sign is gone), complete the form and press Submit."""
    if step != "handoff":
        return
    if _once(page, "attach"):
        lever_attach_a_file(page)
        return
    working = page.locator(".resume-upload-working")
    if working.count() and working.first.evaluate("(e) => getComputedStyle(e).display") == "block":
        return
    if _once(page, "submit"):
        lever_student_completes(page)
        lever_press_submit(page)


def lever_attach_and_wait(page: Any, step: str) -> None:
    if step == "handoff" and _once(page, "attach"):
        lever_attach_a_file(page)


LEVER_STUDENTS = {
    "complete_and_submit": lever_complete_and_submit, "attach_complete_and_submit": lever_attach_complete_and_submit, "attach_and_wait": lever_attach_and_wait,
    "do_nothing": do_nothing, "close_window": close_window,
}


class LeverRecordingAgent(ApplyAgent):
    """An ``ApplyAgent`` for Lever that serves the fictional board and writes what the board saw when it closes (a child process cannot hand back an object)."""

    def __init__(self, *, fake: TimedLever, record_path: str = "", **kwargs: Any) -> None:
        super().__init__(route_hook=fake.route, headless=True, **kwargs)
        self.fake = fake
        self.record_path = record_path
        self.pressed: dict[str, int] = {}
        self.clicked: list[str] = []

    def _close_browser(self) -> bool:
        try:
            self.pressed = FakeLever.clicks(self._page)
        except Exception:  # noqa: BLE001 - the page is gone
            pass
        return super()._close_browser()

    def _click(self, locator: Any, purpose: str, key: str = "") -> None:
        self.clicked.append(purpose)
        return super()._click(locator, purpose, key)

    def record(self) -> dict[str, Any]:
        return {
            "post_times": list(self.fake.post_times),
            "requests": [{"method": seen.method, "host": seen.host, "path": seen.path, "status": seen.status} for seen in self.fake.requests],
            "websockets": list(self.fake.websockets),
            "non_get": [{"method": seen.method, "host": seen.host, "path": seen.path} for seen in self.fake.non_get_requests(noise=False)],
            "parse_posts": [{"status": seen.status, "sha256": seen.part("resume").sha256 if seen.part("resume") else ""} for seen in self.fake.parse_posts()],
            "apply_posts": len(self.fake.apply_posts()),
            "pressed": dict(self.pressed),
            # (the page's own count is lost when it moves on to the confirmation page; the agent's own record of what it clicked is not)
            "agent_clicks": list(self.clicked),
        }

    def __exit__(self, *exc: Any) -> None:
        if self.record_path:
            try:
                Path(self.record_path).write_text(json.dumps(self.record()), encoding="utf-8")
            except Exception:  # noqa: BLE001 - a record that cannot be written changes nothing
                pass
        super().__exit__(*exc)


class LeverBrowserAgentFactory:
    """The agent factory of the Lever end-to-end tests. Picklable, so ``isolation="process"`` spawns a real child that opens a real Chromium.

    ``scenario`` is what the board's apply POST answers (``apply_fake_ats.LEVER_SCENARIOS``), ``parse_mode`` what its résumé reader does, and ``student`` names one
    of ``LEVER_STUDENTS``.
    """

    def __init__(self, scenario: str = "to_thanks", record_path: str = "", student: str = "", parse_mode: str = "success", isolation: str = "process",
                 page: str = "demo_eeo_survey.html", parse_delay_s: float = 0.15) -> None:
        self.scenario = scenario
        self.record_path = record_path
        self.student = student
        self.parse_mode = parse_mode
        self.isolation = isolation
        self.page = page
        self.parse_delay_s = parse_delay_s

    def available(self) -> str:
        return ""

    def __call__(self, *, mode: str, run_id: str, screenshot_dir: Path | None, timeouts: ApplyTimeouts,
                 on_progress: Callable[[str, str], None], heartbeat: Callable[[], None], ats: str = "lever") -> LeverRecordingAgent:
        fake = TimedLever(self.scenario, parse_mode=self.parse_mode, page=self.page)
        fake.parse_delay_s = self.parse_delay_s
        return LeverRecordingAgent(
            fake=fake, record_path=self.record_path, mode=mode, adapter=LeverAdapter(), run_id=run_id, screenshot_dir=screenshot_dir,
            timeouts=HANDOFF_TIMEOUTS if timeouts == ApplyTimeouts() else timeouts, on_progress=on_progress, heartbeat=heartbeat,
            student_hook=LEVER_STUDENTS.get(self.student),
        )
