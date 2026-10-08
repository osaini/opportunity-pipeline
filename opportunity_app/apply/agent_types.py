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
BUILT_MODES = ("lookup", "rehearse", "handoff")
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
    "your_turn": "Your turn: complete the form in the window, then press Submit application there",
    "submitting": "Submitting to Greenhouse…",
    "security_code": ("Greenhouse emailed you a security code. The app is looking for it in your Gmail; "
                      "you can also type it into the window yourself"),
    "code_typed": "The app typed the security code from your email. Press Submit application in the window",
    "code_yours": "Type the security code Greenhouse emailed you into the window, then press Submit application",
    "challenge": "Greenhouse showed a check in the window. Finish it there",
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
OP_FILE_CHECK = "file_check"        # child -> parent: {"op", "id", "key", "ref", "sha256"}; asks whether the document the plan names for field
                                    #   "key" (its source ref, and the SHA-256 of its text) is still the one to attach (M7: the cover letter);
                                    #   the parent replies OP_FILE_CHECK_REPLY
OP_FILE_CHECK_REPLY = "file_check_reply"  # parent -> child: {"op", "id", "ok"}; anything but ok=True is False
OP_CANCEL = "cancel"                # parent -> child: {"op"}
OP_RESULT = "result"                # child -> parent: {"op", "result": RunResult}
OP_ERROR = "error"                  # child -> parent: {"op", "error": exception type name only, never its message}

# M5b part 2: Finish in browser.
OP_HANDOFF_READY = "handoff_ready"     # child -> parent, one-way: {"op", "plan": [value-free entries], "plan_hash",
                                       #   "left": [{"key", "question", "reason"}], "screenshot": {...} | None,
                                       #   "captcha_widget": bool, "page_defaults": [keys],
                                       #   "handoff_in_s": seconds the agent will really keep the window for the student}
OP_SECURITY_CODE = "security_code"     # child -> parent: {"op", "id"}. The parent answers for its own run's claim,
                                       #   never for a token the child names. Sent again only after the last ask's
                                       #   reply arrived (never while one is outstanding).
OP_SECURITY_CODE_REPLY = "security_code_reply"   # parent -> child: {"op", "id", "status": "waiting" | "found" |
                                       #   "fallback", "reason"} plus "code" only when found. Never logged or stored.
OP_SECURITY_CODE_RESULT = "security_code_result" # child -> parent, one-way: {"op", "id", "typed": bool,
                                       #   "reason": "" | "inputs_not_empty" | "inputs_missing" | "bad_code" |
                                       #   "page_closed" | "already_typed"}. Only this makes the parent record "typed".
OP_FRONT = "front"                     # parent -> child, one-way: bring the window to the front
# OP_HAND_OVER (M5a) gains "expires": time.monotonic() + timeouts.reply_s, stamped by the child when it sends.

# The handoff's sentences. The runner uses some when it stops a run itself; the UI never parses them.
HANDOFF_NOT_SUBMITTED = "You didn't submit it in the window. Your application was not sent."
HANDOFF_CRASHED = ("The browser window stopped working before you submitted. Your application was not sent. "
                   "Try again.")
HANDOFF_UNRECORDED = "The app couldn't record this submission, so it stopped it. Nothing was sent. Try again."
HANDOFF_ELSEWHERE = ("The form tried to send to an address the app doesn't recognize, so the app stopped it. "
                     "Nothing was sent. Apply from the posting instead.")
HANDOFF_EARLY = ("The form tried to send before the app finished filling it, so the app stopped it and closed the "
                 "window. Nothing was sent. Try again.")
HANDOFF_NO_LOADER = ("The app couldn't find where this form sends applications, so it can't keep track of your "
                     "Submit. Apply from the posting instead.")
HANDOFF_S3 = ("This board uploads files as soon as they are attached, which the app does not support yet. "
              "Nothing was sent. Apply from the posting instead.")
HANDOFF_UPLOAD = ("The form tried to upload a file, which the app does not allow yet, so the app stopped it and closed "
                  "the window. Nothing was sent. Apply from the posting instead.")
HANDOFF_HIDDEN = ('The form has a hidden field where the app expected "{question}", so the app stopped before filling '
                  "it. Nothing was sent. Apply from the posting instead.")
WINDOW_CLOSED = "You closed the window. No application was sent."
WINDOW_UNCONFIRMED = ("The app couldn't confirm the Chromium window closed, so it can't be sure nothing was sent. "
                      "Check your email for a confirmation from Greenhouse.")
YOUR_TURN = "The form is filled in the Chromium window. Complete the fields below, then press Submit application there."
YOUR_TURN_NONE_LEFT = "The form is filled in the Chromium window. Check the form, then press Submit application there."
LEFT_FIELD = 'The app could not fill "{question}". Fill it in yourself.'
LEFT_CAPTCHA = "Tick the CAPTCHA box in the window yourself before you press Submit application."
LEFT_COVER_LETTER_CHANGED = "Your cover letter for this role changed while the app was working, so it was not attached. Attach yours in the window."
LEFT_UNPLANNED = "The page put something in \"{question}\" that the app didn't. Check it before you press Submit application."


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
    fill_s: float = 5 * 60           # the fill before the student's turn (the rehearsal budget); the deadline counts it
    code_read_s: float = 10 * 60     # D10 B: how long the parent's reader looks for the code email (= security_code.CODE_WINDOW)
    code_poll_s: float = 15.0        # between two asks while the reader says waiting (= security_code.POLL_EVERY)
    code_reply_s: float = 45.0       # an ask with no reply after this is SHOWN as waiting; it stays pending (= REPLY_TIMEOUT_S)
    heartbeat_s: float = 20.0        # every wait loop heartbeats at least this often (spec 5.2 rule 4: 30 s)

    @property
    def after_hand_over_s(self) -> float:   # one budget shared by every wait after hand-over except the outcome windows
        return self.code_read_s + self.security_code_s


@dataclass(frozen=True)
class FilePayload:
    """A file to attach, by its bytes and the name the student sees (never the storage file name)."""

    name: str
    mime_type: str
    buffer: bytes = field(repr=False)
    sha256: str = ""
    # A generated document (the cover letter): the SHA-256 of the approved text this file was rendered from, which the plan names too.
    content_sha256: str = ""

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
    files: dict[str, FilePayload]     # by plan field key ("resume", "cover_letter"); empty for a lookup
    lookup: LookupRequest | None
    screenshot_dir: str               # absolute; "" means take no screenshot
    timeouts: ApplyTimeouts = ApplyTimeouts()
    deadline_s: float = 0.0           # the run's deadline in seconds from its start; a child process ends itself ``timeouts.orphan_s`` after it. 0 means none.
    ends_at: float = 0.0              # a time.monotonic() instant (system-wide); every agent wait is capped by it; 0 = no cap


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
    confirmation_seen: bool = False   # True only when decide_outcome gave "submitted" with resolved_by == "page"


def problem_dict(problem: Any) -> dict[str, Any]:
    """A checks.Problem (or anything with its attributes) as the plain dict a RunResult carries."""
    return {
        "kind": str(getattr(problem, "kind", "") or ""),
        "key": str(getattr(problem, "key", "") or ""),
        "message": str(getattr(problem, "message", "") or ""),
        "question": str(getattr(problem, "question", "") or ""),
        "required": bool(getattr(problem, "required", True)),
    }


class HandoffLink(Protocol):
    """The handoff's extra half of the pipe. None outside handoff (and in tests that play the parent themselves)."""

    def ready(self, message: dict[str, Any]) -> None: ...          # sends OP_HANDOFF_READY
    def ask_code(self) -> int: ...                                 # sends OP_SECURITY_CODE; returns its id; never blocks
    def code_reply(self, ident: int) -> dict[str, Any] | None: ... # the reply if it arrived, else None; never blocks
    def code_result(self, ident: int, typed: bool, reason: str = "") -> None: ...  # sends OP_SECURITY_CODE_RESULT
    def abandon_code(self) -> None: ...                            # the agent stops waiting for its ask: a reply that comes later is dropped
    def front_requested(self) -> bool: ...                         # True once for each OP_FRONT
    def parent_gone(self) -> bool: ...                             # the pipe hit end-of-file or a bad frame


class ApplyAgentLike(Protocol):
    def __enter__(self) -> "ApplyAgentLike": ...
    def __exit__(self, *exc: Any) -> None: ...
    def run(
        self, plan: Any, *, page_url: str, schema: list[Any], files: dict[str, FilePayload],
        lookup: LookupRequest | None = None,
        replan: Callable[[list[dict[str, Any]], bool], Any] | None = None,
        hand_over: Callable[[], bool] | None = None,
        cancelled: Callable[[], bool] | None = None,
        link: "HandoffLink | None" = None,
        check_file: Callable[[str, str, str], bool] | None = None,   # (key, ref, sha256); given only when a cover letter is to be attached (M7)
    ) -> RunResult: ...


class ApplyAgentFactory(Protocol):
    """A picklable module-level object. ``isolation`` is "process" for anything that opens a browser."""

    isolation: str

    def available(self) -> str: ...
    def __call__(
        self, *, mode: str, run_id: str, screenshot_dir: Path | None, timeouts: ApplyTimeouts,
        on_progress: Callable[[str, str], None], heartbeat: Callable[[], None],
    ) -> ApplyAgentLike: ...
