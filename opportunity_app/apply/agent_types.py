"""What the apply agent and its runner agree on: the timeouts, the files, the lookup, the job, the result, the factory.

Standard library only, and no import of any other first-party module (tests/test_leaf_modules.py holds it to that), so the
runner's child process can read the pipe's messages and the agent and the runner (apply/agent.py, apply/runner.py) share one
definition. A job still names the agent's factory and carries a ``policy.Plan``, so unpickling one imports those modules in
the child; what stays out of it is the web app and the database connection, which a plan only closes over in the parent.
Only FilePayload's bytes and LookupRequest's typed text are student data; both live in memory and are never written to
a run row. Everything a RunResult carries is value-free: sentences, public page text, hashes and request facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

AGENT_MODES = ("lookup", "rehearse", "submit", "handoff")
# Which agent mode each apply_runs.kind runs in.
MODE_FOR_KIND = {"lookup": "lookup", "rehearsal": "rehearse", "submit": "submit", "handoff": "handoff"}
# The modes built so far. The others return a failed RunResult and open no browser.
BUILT_MODES = ("lookup", "rehearse")
OUTCOMES = ("looked_up", "rehearsed", "submitted", "unconfirmed", "needs_you", "failed")
ISOLATIONS = ("process", "thread")

# The steps a run reports, in order, with their words (apply_runs.progress_json). The agent fills {n} and {question}.
PROGRESS_STEPS = {
    "start": "Starting the browser",
    "open": "Opening the Greenhouse form",
    "read": "Reading the form",
    "lookup": "Looking up options for {question}",
    "fill": "Filling {n} fields",
    "check": "Checking every required field",
    "picture": "Taking a picture of the filled form",
}
MAX_LOOKUP_OPTIONS = 20
# The one sentence for a run the student stopped. The agent says it, the runner stops a run with it, and the runner's summary
# relies on it ending in the same "No application was sent." the other failure sentences end in, so there is one copy.
STOPPED = "You stopped this run. No application was sent."

# The pipe between the runner (parent) and its child. Every message is a dict with "op".
OP_PROGRESS = "progress"            # child -> parent: {"op", "step", "text"}
OP_HEARTBEAT = "heartbeat"          # child -> parent: {"op"}
OP_REPLAN = "replan"                # child -> parent: {"op", "id", "scan", "uploads_on_attach"}; parent replies OP_REPLAN_REPLY
OP_REPLAN_REPLY = "replan_reply"    # parent -> child: {"op", "id", "plan", "error"}
OP_HAND_OVER = "hand_over"          # child -> parent: {"op", "id"}; parent commits, then replies OP_HAND_OVER_REPLY
OP_HAND_OVER_REPLY = "hand_over_reply"  # parent -> child: {"op", "id", "ok"}; anything but ok=True is False
OP_CANCEL = "cancel"                # parent -> child: {"op"}
OP_RESULT = "result"                # child -> parent: {"op", "result": RunResult}
OP_ERROR = "error"                  # child -> parent: {"op", "error": exception type name only, never its message}


@dataclass(frozen=True)
class ApplyTimeouts:
    settle_s: float = 2.0            # after the page settles, before the first input
    between_fields_s: float = 0.25
    choice_settle_s: float = 3.0     # after a react-select choice: no request in flight, or this, whichever is first
    navigation_s: float = 30.0
    outcome_s: float = 30.0          # 6.14 window (submit, M6)
    captcha_s: float = 12.0          # D14 B only (not chosen)
    person_s: float = 5 * 60         # D14 A: the student ticks a CAPTCHA box before hand-over (M6)
    handoff_s: float = 20 * 60       # M5b
    security_code_s: float = 10 * 60
    reply_s: float = 10.0            # how long the child waits for a hand-over answer
    replan_s: float = 30.0           # how long the child waits for a plan
    orphan_s: float = 10.0           # a child process whose runner is gone (or whose deadline passed) ends itself this long after, however stuck its page is


@dataclass(frozen=True)
class FilePayload:
    """A file to attach, by its bytes and the name the student sees (never the storage file name)."""

    name: str
    mime_type: str
    buffer: bytes = field(repr=False)
    sha256: str = ""

    def as_playwright(self) -> dict[str, Any]:
        return {"name": self.name, "mimeType": self.mime_type, "buffer": self.buffer}


@dataclass(frozen=True)
class LookupRequest:
    """One typeahead to look up: type ``text`` into the field ``key`` and read the options it offers."""

    key: str                          # the schema field name, which is the control's id on the page
    field: str                        # the apply_ats_labels field: location, school, degree, discipline, ...
    question: str                     # the form's label, for progress and reasons
    text: str = field(repr=False, default="")   # what the student typed; sent to Greenhouse's lookup service only


@dataclass(frozen=True)
class AgentJob:
    """Everything a child needs to run once. Pickled into the child; holds values in memory only."""

    run_id: str
    mode: str                         # one of AGENT_MODES
    page_url: str                     # greenhouse.canonical_url(token, job_id)
    plan: Any                         # apply.policy.Plan: the draft plan from the schema alone (values in memory)
    schema: list[Any]                 # apply.policy.SchemaField list from Greenhouse's listing
    files: dict[str, FilePayload]     # by plan field key ("resume"); empty for a lookup
    lookup: LookupRequest | None
    screenshot_dir: str               # absolute; "" means take no screenshot
    timeouts: ApplyTimeouts = ApplyTimeouts()
    deadline_s: float = 0.0           # the run's deadline in seconds from its start; a child process ends itself ``timeouts.orphan_s`` after it. 0 means none.


@dataclass
class RunResult:
    """What one run found. Value-free: sentences, public page text, MACs and hashes, request facts."""

    outcome: str                                                    # one of OUTCOMES
    reasons: list[str] = field(default_factory=list)                # plain sentences, never a value
    plan: list[dict[str, Any]] = field(default_factory=list)        # apply.policy.plan_entries(live plan)
    plan_hash: str = ""
    join_problems: list[dict[str, Any]] = field(default_factory=list)   # problem_dict(checks.Problem) from the join
    check_problems: list[dict[str, Any]] = field(default_factory=list)  # problem_dict(...) from checks.check_required
    options: dict[str, list[str]] = field(default_factory=dict)     # lookup only: {LookupRequest.field: [labels]}
    screenshots: list[dict[str, Any]] = field(default_factory=list) # {"step", "path" (absolute), "sha256", "masked": [keys]}
    refused: list[dict[str, Any]] = field(default_factory=list)     # checks.Abort.record(method) entries, and websockets
    requests: list[dict[str, Any]] = field(default_factory=list)    # after hand-over only (M5b/M6); empty in M5a
    evidence: dict[str, Any] = field(default_factory=dict)          # see plan section 3.4
    handed_over: bool = False
    after_click: bool = False


def problem_dict(problem: Any) -> dict[str, Any]:
    """A checks.Problem (or anything with its attributes) as the plain dict a RunResult carries."""
    return {
        "kind": str(getattr(problem, "kind", "") or ""),
        "key": str(getattr(problem, "key", "") or ""),
        "message": str(getattr(problem, "message", "") or ""),
        "question": str(getattr(problem, "question", "") or ""),
        "required": bool(getattr(problem, "required", True)),
    }


class ApplyAgentLike(Protocol):
    def __enter__(self) -> "ApplyAgentLike": ...
    def __exit__(self, *exc: Any) -> None: ...
    def run(
        self, plan: Any, *, page_url: str, schema: list[Any], files: dict[str, FilePayload],
        lookup: LookupRequest | None = None,
        replan: Callable[[list[dict[str, Any]], bool], Any] | None = None,
        hand_over: Callable[[], bool] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> RunResult: ...


class ApplyAgentFactory(Protocol):
    """A picklable module-level object. ``isolation`` is "process" for anything that opens a browser."""

    isolation: str

    def available(self) -> str: ...
    def __call__(
        self, *, mode: str, run_id: str, screenshot_dir: Path | None, timeouts: ApplyTimeouts,
        on_progress: Callable[[str, str], None], heartbeat: Callable[[], None],
    ) -> ApplyAgentLike: ...
