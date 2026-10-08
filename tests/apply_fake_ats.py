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
    "next_button",                  # a multi-page form: a Next button sits under the questions
    "continue_link",                # a multi-page form: a "Save and continue" link outside the form, in the page around it
    "step_indicator",               # a multi-page form: "Step 1 of 3" above the questions
    "continue_to_step",             # a multi-page form: a "Continue to step 2" button under the questions
    "next_section",                 # a multi-page form: a "Next section" button under the questions
    "next_review",                  # a multi-page form: a "Next: Review" button under the questions
    "page_slash_counter",           # a multi-page form: "Page 1/3" above the questions
    "next_button_aria",             # a multi-page form: a button with only an icon and the label "Go to the next page"
    "counter_outside",              # a multi-page form: "Step 2 of 4" in a header outside the form's own element
    "next_review_submit",           # a multi-page form: a "Next: Review and submit" button under the questions
    "continue_to_submit",           # a multi-page form: a "Continue to submit" button under the questions
    "go_to_step",                   # a multi-page form: a "Go to step 2" button under the questions
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
    # The agent's init script makes window.fetch read-only (it strips keepalive), so the second POST comes from a submit listener of its
    # own that runs right after the form's: the same address, the same body, while the first is still in flight.
    "double_submit": _FORM + """.addEventListener("submit", function () {
      if (window.grValidate()) return;
      fetch("https://boards.greenhouse.io" + window.__loader.submitPath, {method: "POST", body: new FormData(""" + _FORM + """)}).catch(function () {});
    });""",
    "captcha_body_leak": _FORM + """.addEventListener("input", function (e) {
      if (e.target.value) fetch("https://www.google.com/recaptcha/api2/reload?k=fixture", {method: "POST", body: e.target.value}).catch(function () {});
    });""",
    "websocket": """try { new WebSocket("wss://socket.example-robotics.test/live"); } catch (e) {}""",
    "s3_upload": """document.getElementById("resume").addEventListener("change", function (e) {
      fetch("https://example-robotics-uploads.s3.amazonaws.com/resume", {method: "PUT", body: e.target.files[0]}).catch(function () {});
    });""",
    "next_button": """(function () {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = "Next";
      """ + _FORM + """.appendChild(button);
    })();""",
    "continue_to_step": """(function () {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = "Continue to step 2";
      """ + _FORM + """.appendChild(button);
    })();""",
    "next_section": """(function () {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = "Next section";
      """ + _FORM + """.appendChild(button);
    })();""",
    "next_review": """(function () {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = "Next: Review";
      """ + _FORM + """.appendChild(button);
    })();""",
    "page_slash_counter": """(function () {
      var note = document.createElement("p");
      note.textContent = "Page 1/3";
      """ + _FORM + """.insertBefore(note, """ + _FORM + """.firstChild);
    })();""",
    "next_button_aria": """(function () {
      var button = document.createElement("button");
      button.type = "button";
      button.setAttribute("aria-label", "Go to the next page");
      button.textContent = "→";
      document.body.appendChild(button);
    })();""",
    "counter_outside": """(function () {
      var note = document.createElement("div");
      note.textContent = "Step 2 of 4";
      document.body.insertBefore(note, document.body.firstChild);
    })();""",
    "next_review_submit": """(function () {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = "Next: Review and submit";
      """ + _FORM + """.appendChild(button);
    })();""",
    "continue_to_submit": """(function () {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = "Continue to submit";
      """ + _FORM + """.appendChild(button);
    })();""",
    "go_to_step": """(function () {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = "Go to step 2";
      """ + _FORM + """.appendChild(button);
    })();""",
    "continue_link": """(function () {
      var link = document.createElement("a");
      link.href = "#page-2";
      link.setAttribute("role", "button");
      link.textContent = "Save and continue";
      """ + _FORM + """.parentElement.appendChild(link);
    })();""",
    "step_indicator": """(function () {
      var note = document.createElement("p");
      note.textContent = "Step 1 of 3";
      """ + _FORM + """.insertBefore(note, """ + _FORM + """.firstChild);
    })();""",
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
    # The listing can say the cover letter is required; the page then carries the required marker on its upload group, as the live one does.
    letter_required = False

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
        if self.letter_required:
            html = html.replace('<label for="cover_letter">Cover Letter</label>', '<label for="cover_letter">Cover Letter <span class="required"></span></label>')
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
            return self._new_listing() if not self.legacy else copy.deepcopy(fixture_json("schema_legacy.json"))
        if board_token != BOARD_TOKEN:
            return None
        if job_id == JOB_ID and not self.legacy:
            return self._new_listing()
        if job_id == LEGACY_JOB_ID or (self.legacy and job_id == JOB_ID):
            return copy.deepcopy(fixture_json("schema_legacy.json"))
        return None

    __call__ = fetch

    @staticmethod
    def _new_listing() -> dict[str, Any]:
        """The fictional listing; ``CANNED["letter_required"]`` makes its cover letter a required question (read at each fetch)."""
        listing = copy.deepcopy(fixture_json("schema_new.json"))
        if CANNED.get("letter_required"):
            next(block for block in listing["questions"] if block["label"] == "Cover Letter")["required"] = True
        return listing

    def fetch_url(self, url: str) -> dict[str, Any] | None:
        """The same, from a boards-api URL: /v1/boards/{token}/jobs/{id}."""
        match = re.fullmatch(r"/v1/boards/([^/]+)/jobs/(\d+)", urlsplit(url).path)
        return self.fetch(match.group(1), match.group(2)) if match else None


# --- Lever: a fictional posting whose application page is a fixture, and the page client that serves it -------------------------------

LEVER_FIXTURES = FIXTURES.parent / "lever"
LEVER_SITE = "harbordemo"
LEVER_JOB_ID = "6f1d2c3b-4a59-4687-8c7d-9e0f1a2b3c4d"
LEVER_URL = f"https://jobs.lever.co/{LEVER_SITE}/{LEVER_JOB_ID}"
# The page's own title is "Harbor Demo Labs - Customer Success Lead" (tests/fixtures/apply/lever/demo_eeo_survey.html).
LEVER_COMPANY = "Harbor Demo Labs"
LEVER_TITLE = "Customer Success Lead"
LEVER_ROLE_ID = "lever-harbor-demo"


def lever_fixture_text(name: str) -> str:
    return (LEVER_FIXTURES / name).read_text(encoding="utf-8")


class FakeLeverPageClient:
    """Serves a Lever application page fixture, or a 404, or "did not answer", with no network.

    Tests, the sandbox and the UI suite pass it as ``apply_page_client_factory``. ``fetch(site, job_id, host)`` is the real client's
    call (apply.schema_client.LeverPageClient): it returns the page's HTML, or None for a 404. By default it answers the one fictional
    posting; ``any_posting`` answers every site and posting with the page, which a sandbox that seeds its own roles uses.
    ``pages`` maps a posting's ``site/job_id`` to a fixture name so a test can serve different pages. With no ``page``, the fixture named by
    ``CANNED["lever_page"]`` is served (the in-process UI suite sets it), else the demo page.
    """

    def __init__(self, *, page: str | None = None, closed: bool = False, unavailable: bool = False, any_posting: bool = False,
                 pages: dict[str, str] | None = None) -> None:
        self.page = page
        self.closed = closed
        self.unavailable = unavailable
        self.any_posting = any_posting
        self.pages = dict(pages or {})
        self.calls: list[tuple[str, str, str]] = []

    def fetch(self, site: str, job_id: str, host: str) -> str | None:
        from opportunity_app.apply.schema_client import SchemaUnavailable

        self.calls.append((site, job_id, host))
        if self.unavailable:
            raise SchemaUnavailable("Lever did not answer (a fake that does not)")
        if self.closed:
            return None
        key = f"{site}/{job_id}"
        if key in self.pages:
            return lever_fixture_text(self.pages[key])
        if self.any_posting or (site, job_id) == (LEVER_SITE, LEVER_JOB_ID):
            return lever_fixture_text(self.page or CANNED.get("lever_page") or "demo_eeo_survey.html")
        return None

    __call__ = fetch


def seed_lever_role(conn: Any, user_id: str, *, saved: bool = True, role_id: str = LEVER_ROLE_ID) -> str:
    """Insert the fictional Lever posting as a role (saved by default) and return its id. For the sandbox and the UI suite; the caller commits."""
    from opportunity_app.core.timestamps import utc_now

    stamp = utc_now()
    conn.execute(
        "INSERT INTO opportunities(id, company, title, url, first_seen_at, last_seen_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (role_id, LEVER_COMPANY, LEVER_TITLE, LEVER_URL, stamp, stamp, stamp, stamp),
    )
    # A role the pipeline synced has a source row, and the lists read it through one: this one is Lever's, by site and posting uuid.
    conn.execute(
        "INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, source_url, first_seen_at, last_seen_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (role_id, f"lever:{LEVER_SITE}", "Harbor Demo Lever", LEVER_JOB_ID, LEVER_URL, stamp, stamp),
    )
    if saved:
        conn.execute("INSERT INTO opportunity_interactions(opportunity_id, user_id, action, created_at, source) VALUES(?, ?, 'saved', ?, 'user')", (role_id, user_id, stamp))
    return role_id


# Knobs the in-process UI suite may change between tests (a thread-isolated fake reads them when a run starts).
# "handoff" is Finish in browser's canned run: "wait" is how long the fictional student takes before pressing Submit
# application (seconds), and "outcome" is what the form then does: submitted, unconfirmed, security_code, refused (the
# app says no to the hand-over), failed_4xx, or hang_after_hand_over. "after_front" (optional) ends the wait that many seconds
# after the first request to bring the window forward, so a test of that request waits for it rather than racing a fixed wait.
# "letter_required" makes the listing's cover letter a required question, so a test can see what the page does with and without an approved letter.
# "lever_page" is the Lever fixture a ``FakeLeverPageClient`` made without a ``page`` serves (empty: the demo page).
CANNED: dict[str, Any] = {"lever_page": "", "hang": False, "outcome": "rehearsed", "step_delay": 0.3, "handoff": {"wait": 1.5, "outcome": "submitted"}, "letter_required": False}
HANDOFF_OUTCOMES = ("submitted", "unconfirmed", "security_code", "refused", "failed_4xx", "hang_after_hand_over")
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
                 outcome: str | None = None, hang: bool | None = None, handoff: dict[str, Any] | None = None) -> None:
        self.missing = missing
        self.isolation = isolation
        self.step_delay = step_delay
        self.outcome = outcome
        self.hang = hang
        self.handoff = handoff

    def available(self) -> str:
        return self.missing

    def __call__(self, **kwargs: Any) -> "CannedAgent":
        return CannedAgent(
            step_delay=CANNED["step_delay"] if self.step_delay is None else self.step_delay,
            outcome=CANNED["outcome"] if self.outcome is None else self.outcome,
            hang=CANNED["hang"] if self.hang is None else self.hang,
            handoff=dict(CANNED["handoff"] if self.handoff is None else self.handoff),
            **kwargs,
        )


class CannedAgent:
    """A fictional rehearsal or lookup: no browser, no socket, every sentence value-free."""

    def __init__(self, *, mode: str, run_id: str, screenshot_dir: Path | None, timeouts: Any, on_progress: Any, heartbeat: Any,
                 step_delay: float, outcome: str, hang: bool, handoff: dict[str, Any] | None = None, ats: str = "greenhouse") -> None:
        self.mode, self.run_id, self.screenshot_dir, self.ats = mode, run_id, screenshot_dir, ats
        self.on_progress, self.heartbeat = on_progress, heartbeat
        self.step_delay, self.outcome, self.hang = step_delay, outcome, hang
        self.handoff = handoff or {"wait": 1.5, "outcome": "submitted"}
        self.timeouts = timeouts
        self.extra_cleanup: Any = None

    def __enter__(self) -> "CannedAgent":
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.extra_cleanup is not None:
            self.extra_cleanup()
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
            hand_over: Any = None, cancelled: Any = None, link: Any = None, check_file: Any = None) -> Any:
        from opportunity_app.apply import policy as apply_policy
        from opportunity_app.apply.agent_types import PROGRESS_STEPS, RunResult, progress_text

        cancelled = cancelled or (lambda: False)
        stopped = RunResult("failed", [STOPPED_TEXT])
        if self.mode == "handoff":
            return self._handoff(plan, hand_over, cancelled, link)
        self.on_progress("open", progress_text("open", "Greenhouse"))
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
                      # A file counts as attached when the runner read one for it (the résumé, an approved cover letter); there is no page to read it back from.
                      "filled_keys": [entry["key"] for entry in entries if entry["disposition"] == "fill" and (entry.get("control") != "file" or entry["key"] in files)],
                      "checked_keys": [entry["key"] for entry in entries if entry["disposition"] == "deferred" and entry.get("control") != "file"]},
        )


    # --- Finish in browser: a fictional student who takes a moment and presses Submit application ---------------------------------

    def _handoff(self, plan: Any, hand_over: Any, cancelled: Any, link: Any) -> Any:
        """The handoff script (no browser, no socket): fill, say ready, wait for the student, hand over, then one of the outcomes."""
        from opportunity_app.apply import policy as apply_policy
        from opportunity_app.apply.agent_types import (
            HANDOFF_NOT_SUBMITTED, HANDOFF_UNRECORDED, LEFT_FIELD, PROGRESS_STEPS, RunResult, progress_text,
        )
        from opportunity_app.apply.ats import name_of
        from opportunity_app.apply.checks import UNCONFIRMED_NOTE as UNCONFIRMED_TEMPLATE

        ats_name = name_of(self.ats)
        UNCONFIRMED_NOTE = UNCONFIRMED_TEMPLATE.format(ats=ats_name)

        entries = apply_policy.plan_entries(plan)
        left = [
            {"key": entry["key"], "question": entry["question"], "reason": entry["problem"] or LEFT_FIELD.format(question=entry["question"])}
            for entry in entries if entry["disposition"] == "left_for_you"
        ]
        plan_hash = getattr(plan, "plan_hash", "")
        # On Lever the app's attach of the résumé sends it to Lever (the plan fills the file only when the student let the app attach it); the real
        # driver reports it in the ready message and in every result after it, and so does this one.
        resume_sent = self.ats == "lever" and any(
            entry["disposition"] == "fill" and (entry.get("source") or {}).get("kind") == "resume" for entry in entries
        )
        evidence: dict[str, Any] = {
            "resume_sent_to_lever": resume_sent,
            "page": "application_form_new", "loader": {"submit_path": True, "confirmation_path": True}, "uploads_on_attach": False,
            "captcha_widget": False, "lookups": [], "submit_path_hit": False, "refused_total": 0, "left_for_you": left, "page_defaults": [],
            "handoff_end": "", "browser_closed": True, "parent_gone": False, "submit_post": False, "submit_continued": False,
        }
        stopped = RunResult("needs_you", [HANDOFF_NOT_SUBMITTED], plan=entries, plan_hash=plan_hash, handed_over=False, after_click=False,
                            evidence={**evidence, "handoff_end": "stopped"})
        self.on_progress("open", progress_text("open", ats_name))
        if self.handoff.get("outcome") == "no_loader":
            # A property of the board: the form sends applications somewhere the app does not know. Stops before any input, as the real agent does.
            from opportunity_app.apply.agent_types import HANDOFF_NO_LOADER

            return RunResult("needs_you", [HANDOFF_NO_LOADER], plan=entries, plan_hash=plan_hash, handed_over=False, after_click=False,
                             evidence={**evidence, "handoff_end": "board"})
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
        if link is not None:
            message = {"plan": entries, "plan_hash": plan_hash, "left": left, "screenshot": shots[0] if shots else None,
                       "captcha_widget": False, "page_defaults": [], "resume_sent_to_lever": resume_sent}
            if "in_s" in self.handoff:
                message["handoff_in_s"] = self.handoff["in_s"]   # how long the window really stays the student's (the real agent says it)
            link.ready(message)
        self.on_progress("your_turn", PROGRESS_STEPS["your_turn"])
        if self.handoff.get("elsewhere"):
            # The student pressed Submit and the form tried to send somewhere the app does not recognize: refused, and the turn goes on.
            self.on_progress("form_elsewhere", PROGRESS_STEPS["form_elsewhere"].format(host=str(self.handoff["elsewhere"])))
        self._after_ready()
        waited, beat = 0.0, 0.0
        wait = float(self.handoff.get("wait", 1.5))
        fronts = 0
        while waited < wait:
            if cancelled():
                return stopped
            time.sleep(0.1)
            waited += 0.1
            if link is not None and link.front_requested():
                fronts += 1
                if "after_front" in self.handoff:
                    wait = min(wait, waited + float(self.handoff["after_front"]))
            if waited - beat >= 1.0:
                self.heartbeat()
                beat = waited
        evidence["fronts"] = fronts
        granted = bool(hand_over()) if hand_over is not None else False
        kind = str(self.handoff.get("outcome", "submitted"))
        if kind == "refused" or not granted:
            # The app said no (or we pretend it did): the POST would be aborted, and nothing the agent says may call it sent.
            return RunResult("needs_you", [HANDOFF_UNRECORDED], plan=entries, plan_hash=plan_hash, screenshots=shots, handed_over=False,
                             after_click=False, evidence={**evidence, "handoff_end": "refused"})
        self.on_progress("submitting", progress_text("submitting", ats_name))
        evidence.update(handoff_end="posted", submit_post=True, submit_continued=True, submit_status=200,
                        confirmation_path=f"/{LEVER_SITE}/{LEVER_JOB_ID}/thanks" if self.ats == "lever" else "/examplerobotics/jobs/4000000001/confirmation")
        if kind == "hang_after_hand_over":
            while not cancelled():   # a process is killed; a thread is told to stop when the run ends
                time.sleep(0.2)
            return RunResult("unconfirmed", [UNCONFIRMED_NOTE], plan=entries, plan_hash=plan_hash, screenshots=shots, handed_over=True,
                             after_click=True, evidence=evidence)
        if kind == "unconfirmed":
            evidence["submit_status"] = None
            return RunResult("unconfirmed", [UNCONFIRMED_NOTE], plan=entries, plan_hash=plan_hash, screenshots=shots, handed_over=True,
                             after_click=True, evidence=evidence)
        if kind == "failed_4xx":
            evidence.update(submit_status=422)
            refused = "Lever refused the form (HTTP 422): \"Current company\" is marked invalid" if self.ats == "lever" else 'Greenhouse marked "Why do you want to work here?" as wrong'
            return RunResult("failed", [refused], plan=entries, plan_hash=plan_hash,
                             screenshots=shots, handed_over=True, after_click=True, evidence=evidence)
        code = {"prompted": False, "typed": False, "fallback": False, "posted": False, "rounds": 0, "auto_submit_blocked": False, "reason": ""}
        if kind == "security_code":
            code.update(prompted=True, rounds=1)
            self.on_progress("security_code", progress_text("security_code", "Greenhouse"))
            typed = self._ask_for_the_code(link, cancelled, typed_ok=bool(self.handoff.get("code_typed", True)),
                                           reason=str(self.handoff.get("code_reason", "")))
            code.update(typed=typed, fallback=not typed, posted=True, reason="" if typed else str(self.handoff.get("code_reason", "")))
            self.on_progress("code_typed" if typed else "code_yours", progress_text("code_typed" if typed else "code_yours", "Greenhouse"))
            time.sleep(0.2)
        evidence["security_code"] = code
        return RunResult("submitted", [], plan=entries, plan_hash=plan_hash, screenshots=shots, handed_over=True, after_click=True,
                         confirmation_seen=True, evidence=evidence)

    def _after_ready(self) -> None:
        """A hook for the process variant (it starts its stand-in for Chromium before the student's turn)."""

    @staticmethod
    def _ask_for_the_code(link: Any, cancelled: Any, *, typed_ok: bool = True, reason: str = "") -> bool:
        """Ask the parent for the code without blocking and never again while an ask is outstanding. True when it was found and 'typed'.

        ``typed_ok`` False plays an agent that was handed the code and could not put it in (``reason`` says why)."""
        if link is None:
            return False
        ident = link.ask_code()
        waited = 0.0
        while waited < 30.0 and not cancelled():
            reply = link.code_reply(ident)
            if reply is None:
                time.sleep(0.05)
                waited += 0.05
                continue
            status = reply.get("status")
            if status == "found":
                link.code_result(ident, typed_ok, "" if typed_ok else reason)   # the code itself is dropped here: nothing is typed
                return typed_ok
            if status == "fallback":
                return False
            time.sleep(0.3)
            waited += 0.3
            ident = link.ask_code()
        return False


class ProcessCannedFactory:
    """Finish in browser's canned run in a REAL child process, with a sleeping grandchild standing in for Chromium.

    Module-level and picklable (``isolation = "process"``), opens no browser and no socket, and is importable as
    ``apply_fake_ats`` from the spawned child. The grandchild is started before the student's turn (the runner snapshots the
    child's descendants when it sees the agent is ready), its pid is written to ``pid_file``, and the child's own exit
    stops it unless ``leak`` is True (a browser that outlives its driver), so the kill-ordering tests see real pids.
    ``outcome`` and ``wait`` are the same knobs as ``CANNED["handoff"]``.
    """

    isolation = "process"

    def __init__(self, *, outcome: str = "submitted", wait: float = 0.5, spawn_grandchild: bool = True, pid_file: str = "",
                 leak: bool = False, step_delay: float = 0.0, crash_at: str = "") -> None:
        self.outcome = outcome
        self.wait = wait
        self.spawn_grandchild = spawn_grandchild
        self.pid_file = pid_file
        self.leak = leak
        self.step_delay = step_delay
        self.crash_at = crash_at

    def available(self) -> str:
        return ""

    def __call__(self, **kwargs: Any) -> "ProcessCannedAgent":
        return ProcessCannedAgent(
            step_delay=self.step_delay, outcome="rehearsed", hang=False, handoff={"wait": self.wait, "outcome": self.outcome}, factory=self, **kwargs,
        )


class ProcessCannedAgent(CannedAgent):
    def __init__(self, *, factory: ProcessCannedFactory, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.factory = factory
        self.grandchild: subprocess.Popen[bytes] | None = None

    def _after_ready(self) -> None:
        if self.factory.crash_at == "turn":
            os._exit(3)   # the driver dies during the student's turn, whatever the browser does
        if self.factory.crash_at == "after_hand_over":
            self.crash_after_hand_over = True

    def _handoff(self, plan: Any, hand_over: Any, cancelled: Any, link: Any) -> Any:
        if self.factory.spawn_grandchild:
            self.grandchild = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(600)"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if self.factory.pid_file:
                Path(self.factory.pid_file).write_text(f"{os.getpid()} {self.grandchild.pid}", encoding="utf-8")
            if not self.factory.leak:
                self.extra_cleanup = self._stop_grandchild
        if self.factory.crash_at == "after_hand_over":
            original = hand_over

            def hand_over_then_die() -> bool:
                granted = bool(original())
                if granted:
                    os._exit(4)   # the driver dies right after the parent committed the hand-over
                return granted

            hand_over = hand_over_then_die
        return super()._handoff(plan, hand_over, cancelled, link)

    def _stop_grandchild(self) -> None:
        if self.grandchild is not None and self.grandchild.poll() is None:
            self.grandchild.kill()
            try:
                self.grandchild.wait(timeout=10)
            except subprocess.SubprocessError:
                pass


class HangingAgentFactory:
    """A process-isolated agent that starts a grandchild process and then never yields: only the watchdog can end it."""

    isolation = "process"

    def __init__(self, pid_file: str, *, driver: bool = False) -> None:
        self.pid_file = pid_file
        self.driver = driver   # the grandchild behaves like Playwright's driver: it ends when its stdin closes, that is, when the child dies

    def available(self) -> str:
        return ""

    def __call__(self, **kwargs: Any) -> "HangingAgent":
        return HangingAgent(self.pid_file, self.driver)


class HangingAgent:
    def __init__(self, pid_file: str, driver: bool = False) -> None:
        self.pid_file = pid_file
        self.driver = driver

    def __enter__(self) -> "HangingAgent":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def run(self, plan: Any, **kwargs: Any) -> Any:
        code = "import sys; sys.stdin.read()" if self.driver else "import time; time.sleep(600)"
        grandchild = subprocess.Popen(
            [sys.executable, "-c", code], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdin=subprocess.PIPE if self.driver else None,
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


def kill_if_same_process(pid: int, started: str | None) -> bool:
    """Kill ``pid`` only if it is still the process first seen there (``started`` is what ``process_start`` read then). True if killed.

    A cleanup that kills by a bare pid after the process is gone can end a stranger's program, because the pid is handed out again
    (quickly, on Windows). With no recorded start nothing is killed.
    """
    from opportunity_app.apply import runner as apply_runner

    if not started or not apply_runner.process_alive(pid) or apply_runner.process_start(pid) != started:
        return False
    apply_runner._kill_pid(pid)
    return True
