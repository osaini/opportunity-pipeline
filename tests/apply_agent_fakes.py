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

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers_apply
from apply_fake_ats import API_HOST, JOB_URL, LOOKUP_OPTIONS, FakeGreenhouse, fixture_json

from opportunity_app.apply import policy as apply_policy
from opportunity_app.apply.agent import ApplyAgent, GreenhouseAdapter
from opportunity_app.apply.agent_types import ApplyTimeouts, FilePayload
from opportunity_app.apply.checks import Endpoint
from opportunity_app.apply.greenhouse import ADAPTER_VERSION
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
CONSENT = "I consent to Example Robotics storing my application data for 365 days"


def resume_payload(data: bytes = RESUME_BYTES, name: str = RESUME_NAME) -> FilePayload:
    return FilePayload(name=name, mime_type="application/pdf", buffer=data, sha256=hashlib.sha256(RESUME_BYTES).hexdigest())


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


def fixture_replan(sources: apply_policy.Sources, *, schema: list[apply_policy.SchemaField] | None = None) -> Callable[[list[dict[str, Any]], bool], Any]:
    """The parent's answer to the child's scan: the plan from the page's own fields, as ``ApplyRunner`` builds it."""
    listing = schema if schema is not None else fixture_schema()

    def replan(scan: list[dict[str, Any]], uploads_on_attach: bool) -> Any:
        return apply_policy.build_plan(
            apply_policy.with_page_labels(listing, scan), scan, sources, COMPANY, "rehearse",
            canonical_url=JOB_URL, adapter_version=ADAPTER_VERSION, uploads_on_attach=uploads_on_attach,
        )

    return replan


def draft_plan(sources: apply_policy.Sources, *, schema: list[apply_policy.SchemaField] | None = None) -> Any:
    """The plan from the listing alone: what the child is handed before it has read the page."""
    return apply_policy.build_plan(
        schema if schema is not None else fixture_schema(), None, sources, COMPANY, "rehearse",
        canonical_url=JOB_URL, adapter_version=ADAPTER_VERSION,
    )


class RecordingAgent(ApplyAgent):
    """An ``ApplyAgent`` that serves the fictional Greenhouse and writes what the fake saw when it closes."""

    def __init__(self, *, fake: FakeGreenhouse, record_path: str = "", **kwargs: Any) -> None:
        super().__init__(route_hook=fake.route, headless=True, **kwargs)
        self.fake = fake
        self.record_path = record_path

    def record(self) -> dict[str, Any]:
        try:
            forbidden = FakeGreenhouse.forbidden_clicks(self._page)
        except Exception:  # noqa: BLE001 - the page is gone
            forbidden = -1
        return {
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
    """The agent factory of the browser tests. Picklable, so ``isolation="process"`` spawns a real child."""

    def __init__(
        self, scenario: str = "confirm", record_path: str = "", lookup_endpoints: tuple[Endpoint, ...] = FIXTURE_LOOKUP,
        isolation: str = "process",
    ) -> None:
        self.scenario = scenario
        self.record_path = record_path
        self.lookup_endpoints = tuple(lookup_endpoints)
        self.isolation = isolation

    def available(self) -> str:
        return ""

    def __call__(self, *, mode: str, run_id: str, screenshot_dir: Path | None, timeouts: ApplyTimeouts,
                 on_progress: Callable[[str, str], None], heartbeat: Callable[[], None]) -> RecordingAgent:
        return RecordingAgent(
            fake=FakeGreenhouse(self.scenario), record_path=self.record_path, mode=mode, adapter=GreenhouseAdapter(),
            run_id=run_id, screenshot_dir=screenshot_dir, timeouts=TEST_TIMEOUTS if timeouts == ApplyTimeouts() else timeouts,
            lookup_endpoints=self.lookup_endpoints, on_progress=on_progress, heartbeat=heartbeat,
        )
