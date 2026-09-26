"""Notes to prepare for a call once an outreach target writes back.

The notes are short bullets the student can read at a glance and write into:
what the company does, what their reply said, talking points, what the
student could bring, questions to ask, and blanks to fill in during the call.

The same grounding as a cold email draft applies. Every bullet about the
company or the student cites the research, the logged reply, the sent email,
or a confirmed profile field, and a number found in none of them is refused.
Only "what I could bring" may rest on inference, and it says so. Profile
entries the student marked "omit" for outreach are never shown to the model.

Notes are written only from a logged reply: the reply is what the call is
about, and without it the notes would guess. Generating replaces the text in
the editor; the text it replaces is kept in the history, so a hand-edited set
of notes is never lost.

Generation runs in the background as a durable job in job_queue. Moving a
company to a reply status with a reply logged, or logging a reply on one,
queues it on its own. A thread inside the web app runs the jobs. Because the
job's state is in the database, a server that stops mid-generation picks the
job back up when it starts, and a model call cut off by a sleeping laptop is
retried with backoff.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agent_providers import CliAgentProvider, complete_text
from .operations import enqueue_job, recover_stale_jobs, run_next_job
from .outreach import CALL_PREP_STATUSES, OutreachNotFoundError, _log, get_target
from .outreach_drafting import (
    DRAFT_FACT_FIELDS, INFERENCE_BASIS, RESEARCH_FIELDS, ProviderFactory, _entry_name, _field_basis,
    _unsupported_numbers, outreach_proof, resolve_provider,
)
from .preparation import confirmed_facts
from .schema import connect_product, utc_now

LOGGER = logging.getLogger(__name__)
JOB_TYPE = "outreach_call_prep"
# The first try plus three retries, 1, 2, then 4 minutes apart: long enough to
# ride out a laptop waking up and reconnecting.
MAX_ATTEMPTS = 4
ACTIVE_JOB_STATES = {"queued", "running", "retry"}
REPLY_REQUIRED = "Paste their reply under Replies and history first. Call prep is written from it."

# Sections the model writes, in the order they print, with their headings.
MODEL_SECTIONS = (
    ("about_them", "ABOUT THEM"),
    ("their_reply", "WHAT THEY SAID"),
    ("talking_points", "TALKING POINTS (say these)"),
    ("what_i_bring", "WHAT I CAN BRING"),
    ("questions", "QUESTIONS TO ASK"),
)
# Bases each cited section may use. Questions cite nothing: they assert nothing.
REPLY_BASIS = "reply"
SENT_EMAIL_BASIS = "sent_email"
SECTION_INFERENCE = {"what_i_bring"}
# Blanks the student fills in on the call. Written here, not by the model.
DURING_CALL = (
    "Who I talked to (name, role):",
    "What they're working on right now:",
    "Where they could use help:",
    "Role details (dates, part-time or full, in person, pay):",
    "Next step, and by when:",
    "Thank-you note sent on:",
)
MAX_BULLETS = 6
MAX_BULLET_CHARS = 300

INSTRUCTIONS = """You write call-prep notes for one university student. A small company answered the student's cold email about an internship, and the student will talk to them soon.

The notes are read at a glance a few minutes before the call and kept open during it. Write short bullets, not paragraphs: fragments are fine, each under 20 words, plain words, no filler. At most 6 bullets per section.

Sections:
- about_them: what the company builds and sells, who runs it, where it stands (pilots, funding, programs), using only company_research, the reply, and the source URLs. The things worth knowing cold before the call.
- their_reply: what the reply actually says and asks, including any scheduling detail, in the order it matters. Leave it empty when replies is empty.
- talking_points: things the student should say, each a result or fact from the student's profile, phrased the way the student would say it out loud ("Cut our drone's weight from 1.5 kg to 500 g"). Lead with the results the sent email used, since the company has already read those, then add the ones that fit this company best. Copy every number exactly.
- what_i_bring: concrete things the student could do for this company's specific product or work, each tying one piece of the student's experience or skills to one piece of the company's work. These are the student's pitch, so they may rest on inference: cite "inference" when the company fact or the fit is not stated outright. Keep them modest and specific; never promise results.
- questions: 4 to 6 short questions the student could ask, about the company's actual work, the role, and the next step. Only this company could answer them. No generic "what is the culture like".

Rules:
- Use only facts in the JSON input. Never invent achievements, numbers, dates, people, customers, or anything about the company.
- Profile facts cite "profile:<field>". Company facts cite "research:<field>", a source URL, or "reply" when the reply says it. Anything from the email the student sent cites "sent_email". unverified_research must cite "unverified:<field>".
- Plain text, no markdown inside the bullets, no em dashes or en dashes.

Reply with exactly one JSON object and nothing else:
{"about_them": [{"text": "...", "basis": "..."}], "their_reply": [{"text": "...", "basis": "..."}], "talking_points": [{"text": "...", "basis": "..."}], "what_i_bring": [{"text": "...", "basis": "..."}], "questions": ["..."]}"""


class CallPrepRejected(ValueError):
    """The model's notes cited or stated something the inputs do not support."""


class ReplyRequired(ValueError):
    """No reply is logged, and call prep is written from the reply."""


class NotReplied(ValueError):
    """The company is not at a reply status."""


def logged_replies(target: dict[str, Any]) -> list[dict[str, str]]:
    """Every reply the student pasted in, oldest first."""
    events = target.get("events") or []
    return [
        # The date alone: a full timestamp's digits would let the number check
        # accept a made-up figure that happens to appear in its microseconds.
        {"logged_on": event["created_at"][:10], "text": event["detail"]}
        for event in reversed(events) if event["event_type"] == "reply_logged" and event.get("detail")
    ]


def check_can_prep(target: dict[str, Any]) -> None:
    if target["status"] not in CALL_PREP_STATUSES:
        raise NotReplied("Call prep opens once the company has replied")
    if not logged_replies(target):
        raise ReplyRequired(REPLY_REQUIRED)


def call_prep_inputs(conn: sqlite3.Connection, target: dict[str, Any], user_id: str) -> dict[str, Any]:
    facts = confirmed_facts(conn, user_id)
    student = {field: facts[field] for field in DRAFT_FACT_FIELDS if field in facts}
    if not student.get("name"):
        raise ValueError("Confirm your name in your profile before generating call prep")
    proof, lead = outreach_proof(facts)
    student.update(proof)
    research = {field: target[field] for field in (*RESEARCH_FIELDS, "location") if target.get(field)}
    if target.get("research_confidence") == "unverified":
        confirmed = {field: value for field, value in research.items() if field in {"company", "website"}}
        unverified = {field: value for field, value in research.items() if field not in confirmed}
    else:
        confirmed, unverified = research, {}
    return {
        "student": student,
        "lead_with": lead,
        "company_research": confirmed,
        "unverified_research": unverified,
        "source_urls": target["source_urls"],
        "sent_email": {"subject": target.get("email_subject", ""), "body": target.get("email_body", "")} if target.get("email_body") else {},
        "sent_on": target.get("sent_at"),
        "replies": logged_replies(target),
        "status": target["status"],
    }


def _allowed_bases(inputs: dict[str, Any]) -> set[str]:
    bases = {f"profile:{field}" for field in inputs["student"]}
    bases |= {f"research:{field}" for field in inputs["company_research"]}
    bases |= {f"unverified:{field}" for field in inputs["unverified_research"]}
    bases |= set(inputs["source_urls"])
    if inputs["company_research"].get("website"):
        bases.add(inputs["company_research"]["website"])
    if inputs["replies"]:
        bases.add(REPLY_BASIS)
    if inputs["sent_email"]:
        bases.add(SENT_EMAIL_BASIS)
    return bases


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())[:MAX_BULLET_CHARS]


def validate_call_prep(raw: str, inputs: dict[str, Any]) -> tuple[dict[str, list[Any]], list[str]]:
    """Parse a model reply into sections and list every reason it cannot be stored."""
    try:
        parsed = CliAgentProvider.extract_json(raw)
    except ValueError as exc:
        raise CallPrepRejected("The model did not return call prep notes") from exc
    allowed = _allowed_bases(inputs)
    sections: dict[str, list[Any]] = {}
    problems: list[str] = []
    for key, _ in MODEL_SECTIONS:
        value = parsed.get(key)
        entries = value if isinstance(value, list) else []
        if key == "questions":
            sections[key] = [text for text in (_clean(entry) for entry in entries) if text][:MAX_BULLETS]
            continue
        kept = []
        for entry in entries[:MAX_BULLETS]:
            if not isinstance(entry, dict):
                continue
            text = _clean(entry.get("text"))
            basis = _field_basis(str(entry.get("basis") or "").strip())
            if not text:
                continue
            if basis == INFERENCE_BASIS and key not in SECTION_INFERENCE:
                problems.append(f"{key} rests {text[:60]!r} on inference; only what_i_bring may")
            elif basis != INFERENCE_BASIS and basis not in allowed:
                problems.append(f"{key} cites {basis or 'nothing'} for {text[:60]!r}, which is not in the inputs")
            kept.append({"text": text, "basis": basis})
        sections[key] = kept
    if not sections["talking_points"]:
        problems.append("it gives no talking points")
    if inputs["replies"] and not sections["their_reply"]:
        problems.append("it leaves out what the reply said")
    written = "\n".join(
        entry if isinstance(entry, str) else entry["text"] for key, _ in MODEL_SECTIONS for entry in sections[key]
    )
    numbers = _unsupported_numbers(written, inputs)
    if numbers:
        problems.append("it states numbers found in neither your profile, the research, nor the reply: " + ", ".join(numbers))
    if re.search("[–—]", written):
        problems.append("it uses em or en dashes")
    return sections, problems


def template_call_prep(inputs: dict[str, Any]) -> dict[str, list[Any]]:
    """Deterministic notes when no model provider is configured: facts only, no inference."""
    research = inputs["company_research"]
    about = [
        {"text": _clean(research[field]), "basis": f"research:{field}"}
        for field in ("summary", "activity_signal", "location") if research.get(field)
    ]
    if research.get("contact_name"):
        who = ", ".join(part for part in (research["contact_name"], research.get("contact_role", "")) if part)
        about.append({"text": f"Contact: {who}", "basis": "research:contact_name"})
    points = []
    for field in ("experience", "projects"):
        for entry in inputs["student"].get(field, []):
            if not isinstance(entry, dict) or _entry_name(entry) not in inputs["lead_with"]:
                continue
            points += [{"text": _clean(line), "basis": f"profile:{field}"} for line in entry.get("highlights", [])]
    company = research.get("company", "the company")
    return {
        "about_them": about,
        # No model to summarize it, so the latest reply as they wrote it.
        "their_reply": [{"text": _clean(inputs["replies"][-1]["text"]), "basis": REPLY_BASIS}] if inputs["replies"] else [],
        "talking_points": points[:MAX_BULLETS],
        "what_i_bring": [],
        "questions": [
            f"What is {company} working on over the next few months?",
            "Where would an intern help most right now?",
            "What would a first project look like?",
            "What are the next steps, and who should I follow up with?",
        ],
    }


def render_call_prep(company: str, sections: dict[str, list[Any]], inputs: dict[str, Any], generated_on: str) -> str:
    """Plain-text notes: short headings and dashes, easy to read and to write into."""
    lines = [
        f"CALL PREP: {company}",
        f"Written {generated_on} from your confirmed profile, the research on file, and their reply. Check it before the call.",
    ]
    for key, heading in MODEL_SECTIONS:
        entries = sections.get(key) or []
        if key == "what_i_bring" and not entries:
            entries = ["(Write one or two: which part of your work fits theirs, and what you'd do.)"]
        if not entries:
            continue
        lines += ["", heading]
        for entry in entries:
            if isinstance(entry, str):
                lines.append(f"- {entry}")
            else:
                guess = " (my guess)" if entry["basis"] == INFERENCE_BASIS else ""
                lines.append(f"- {entry['text']}{guess}")
    lines += ["", "DURING THE CALL (fill in)", *(f"- {blank} " for blank in DURING_CALL), "", "OTHER NOTES", "- "]
    return "\n".join(line.rstrip() if not line.startswith("- ") or line.strip() != "-" else line for line in lines)


def generate_call_prep(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    provider_factory: ProviderFactory,
    provider: str | None = None,
    keep_existing: bool = False,
) -> dict[str, Any]:
    """Write call prep now. ``keep_existing`` leaves notes the student already has alone."""
    target = get_target(conn, target_id, user_id=user_id, include_events=True)
    check_can_prep(target)
    if keep_existing and target.get("call_prep"):
        return target
    inputs = call_prep_inputs(conn, target, user_id)
    provider_id, model = resolve_provider(provider, purpose="call_prep")
    if provider_id == "legacy":
        sections = template_call_prep(inputs)
        generated_by = "template"
    else:
        agent = provider_factory(provider_id, model)
        content = json.dumps(inputs, ensure_ascii=False, indent=2)
        raw = complete_text(agent, INSTRUCTIONS, content, max_output_tokens=2500)
        sections, problems = validate_call_prep(raw, inputs)
        if problems:
            retry = f"{content}\n\nYour previous notes were rejected because " + "; ".join(problems) + ". Write them again following every rule."
            raw = complete_text(agent, INSTRUCTIONS, retry, max_output_tokens=2500)
            sections, problems = validate_call_prep(raw, inputs)
        if problems:
            raise CallPrepRejected("The call prep was not grounded in your profile, the research, and the reply: " + "; ".join(problems))
        generated_by = f"{provider_id}:{model}"

    timestamp = utc_now()
    text = render_call_prep(target["company"], sections, inputs, timestamp[:10])
    claims = [
        {"text": entry["text"], "basis": entry["basis"], "section": key}
        for key, _ in MODEL_SECTIONS for entry in sections.get(key, []) if isinstance(entry, dict)
    ]
    with conn:
        # Read again: the student may have saved notes while the model worked.
        row = conn.execute("SELECT call_prep FROM outreach_targets WHERE id=? AND user_id=?", (target_id, user_id)).fetchone()
        if row is None:
            raise OutreachNotFoundError(target_id)
        current = row[0] or ""
        if keep_existing and current:
            return get_target(conn, target_id, user_id=user_id)
        if current and current != text:
            # Kept whole so hand-written notes survive a regeneration.
            _log(conn, target_id, user_id, "call_prep_replaced", detail=current)
        conn.execute(
            """
            UPDATE outreach_targets
            SET call_prep=?, call_prep_claims_json=?, call_prep_generated_by=?, call_prep_generated_at=?, updated_at=?
            WHERE id=? AND user_id=?
            """,
            (text, json.dumps(claims, ensure_ascii=False), generated_by, timestamp, timestamp, target_id, user_id),
        )
        _log(conn, target_id, user_id, "call_prep_generated", detail=generated_by)
    return get_target(conn, target_id, user_id=user_id)


def queue_call_prep(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, replace: bool, reason: str,
) -> dict[str, Any]:
    """Queue a background job to write call prep, unless one is already on its way.

    ``replace`` is the student asking for new notes; without it the job leaves
    any notes already there alone, which is what an automatic start wants.
    """
    target = get_target(conn, target_id, user_id=user_id, include_events=True)
    check_can_prep(target)
    job = target.get("call_prep_job")
    if job and job["state"] in ACTIVE_JOB_STATES:
        return get_target(conn, target_id, user_id=user_id)
    queued = enqueue_job(
        conn, JOB_TYPE, {"target_id": target_id, "user_id": user_id, "replace": replace},
        f"call-prep:{target_id}:{uuid4().hex}", max_attempts=MAX_ATTEMPTS,
    )
    with conn:
        conn.execute(
            "UPDATE outreach_targets SET call_prep_job_id=?, updated_at=? WHERE id=? AND user_id=?",
            (queued["id"], utc_now(), target_id, user_id),
        )
        _log(conn, target_id, user_id, "call_prep_queued", detail=reason)
    return get_target(conn, target_id, user_id=user_id)


def auto_queue_call_prep(conn: sqlite3.Connection, target_id: str, *, user_id: str, reason: str) -> bool:
    """Start call prep on its own when a replied company has a reply and no notes yet."""
    try:
        target = get_target(conn, target_id, user_id=user_id, include_events=True)
        if target.get("call_prep") or (target.get("call_prep_job") or {}).get("state") in ACTIVE_JOB_STATES:
            return False
        check_can_prep(target)
    except (OutreachNotFoundError, ValueError):
        return False
    queue_call_prep(conn, target_id, user_id=user_id, replace=False, reason=reason)
    return True


class CallPrepWorker:
    """Runs queued call prep jobs on one background thread inside the web app.

    The jobs live in job_queue, so nothing is lost when the process stops. On
    start, a job still marked running was cut off by the last shutdown, since
    only this thread runs these jobs, and goes back in line. A job whose model
    call fails, as one cut off by a sleeping laptop does, is retried by the
    queue with backoff; the thread polls, so a retry runs soon after it is due.
    """

    def __init__(
        self,
        platform_target: Path | str,
        *,
        provider_factory: ProviderFactory,
        provider: str | None = None,
        poll_seconds: float = 20.0,
    ) -> None:
        self.platform_target = platform_target
        self._provider_factory = provider_factory
        self._provider = provider
        self._poll_seconds = poll_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def recover_interrupted(self) -> int:
        with closing(connect_product(self.platform_target)) as conn:
            return recover_stale_jobs(
                conn, stale_before="9999-12-31T00:00:00+00:00", job_types=(JOB_TYPE,),
                reason="Interrupted when the app stopped; trying again",
            )

    def _run(self, payload: dict[str, Any]) -> None:
        with closing(connect_product(self.platform_target)) as conn:
            try:
                generate_call_prep(
                    conn, payload["target_id"], user_id=payload["user_id"],
                    provider_factory=self._provider_factory, provider=self._provider,
                    keep_existing=not payload.get("replace"),
                )
            except (OutreachNotFoundError, ReplyRequired, NotReplied):
                # Removed, its reply gone, or moved off a reply status while the
                # job waited: nothing to write. Anything else is retried.
                return

    def run_pending(self) -> int:
        """Run every job that is due now. Returns how many ran."""
        ran = 0
        with closing(connect_product(self.platform_target)) as conn:
            while not self._stop.is_set():
                record = run_next_job(conn, {JOB_TYPE: self._run}, only_handled=True)
                if record is None:
                    return ran
                ran += 1
                if record["state"] in {"retry", "dead"}:
                    LOGGER.warning("Call prep job %s: %s (%s)", record["id"], record["state"], record["last_error"])
        return ran

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.recover_interrupted()
        self._thread = threading.Thread(target=self._loop, name="call-prep-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_pending()
            except Exception:  # the thread must outlive any one bad pass
                LOGGER.exception("Call prep worker pass failed")
            self._wake.wait(self._poll_seconds)
            self._wake.clear()
