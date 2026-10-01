"""Notes to prepare for a call once an outreach target writes back.

The goal is a call where the interviewer talks about themselves: people enjoy
talking about their own work. So the notes research what can be known, and
save the call for what only the interviewer can answer, each question built on
something the student read.

Students copy these notes out by hand before the call, so every line is short
and note-like, and nothing is dropped for being long-winded: the numbers, names,
and specifics stay. The notes open with what to ask, so the rest is read with
the questions in mind:

- ASK, in call order: questions that get the interviewer talking (rapport,
  their own story, then the product, including where its design came from and
  how customers compare it with others), then the student's standing questions
  in their own words (outreach_call_questions.py). A standing question may gain
  a hook from the research ("I read the seed is going into scaling production
  in Texas") and a sharper ask; its lead-in is never reworded. Each line names
  what it builds on, and each standing question what to have ready.
- TALKING POINTS: the results the sent email led with, each with where it lands
  for this company ("→ scaling their production").
- KNOW: who they are talking to and their path (outreach_interviewer.py), a
  marked reading of the company (its ideal customer, what sets it apart, what
  makes that possible, where it is heading), the research behind it, and what
  the web does not say.
- DURING THE CALL: blanks to write the answers in, then a short source list.

The student knows their email thread, so the reply is not summarized; it is
still sent to the model, which writes around it.

The interviewer shown is always the one named now. What the last look-up
stored is used only when it was made for that person; when the student names
someone else, or a newer message changes who wrote, and the look-up did not
finish, the notes name the current person with "LinkedIn not read yet" and use
nothing stored about the other one. LinkedIn notes from a profile that never
named the company are printed and listed marked "(profile not confirmed as
them)".

The company side comes from the research (outreach_research.py), each fact
confirmed on the page it cites, printed as the research kept it with a numbered
source, never rewritten by a model. A fact from the company's own site that
turned the check away is printed marked (not checked). With no research yet,
the notes fall back to the one-line research on file. A job researches the
company first when its brief is missing or stale, at most once a day, and not
for an automatic job on a reply the inbox read as a decline. A research failure
never holds up the notes: they say research is missing and why.

A model writes the questions, the standing questions' hooks, where each
talking point lands, and the reading. Each line lists the facts and interviewer
notes it builds on. A number those do not carry is refused outright. Then the
same second read the research uses checks each line against what it cites
(LINE_CHECK_INSTRUCTIONS), and a line that states more than its sources is left
out (as is one it did not answer): a question, a hook, a "lands on" or a
reading line is optional, and the notes stand without it. Talking points cite the sent email or a confirmed profile field.
The reading is the one place for inference, printed as "my read". A competitor
only the research agent picked is never called a competitor or rival, by
citing it or by naming it in any line. A line built only on what the research
could not find must ask, not state. The
research agent's list of what it could not find is shown to the model as
topics to ask about, and a number in that list does not count as found.
Profile entries the student marked "omit" for outreach are never shown to the
model.

Notes are written only from a logged reply: the reply is what the call is
about, and without it the notes would guess. Generating replaces the text in
the editor; the text it replaces is kept in the history, so a hand-edited set
of notes is never lost.

Generation runs in the background as a durable job in job_queue. Moving a
company to a reply status with a reply logged, or logging a reply on one,
queues it on its own. A thread inside the web app runs the jobs, and the
student's Research this company jobs too. Because the job's state is in the
database, a server that stops mid-generation picks the job back up when it
starts, and a model call cut off by a sleeping laptop is retried with backoff.

A job the app started on its own is automatic: it sends the company's name to
the research agent, and the reply, the research, and profile highlights to a
model provider, without a click. While the student has paused automation, the
worker holds such a job in line (not failed, no try used) and runs it after
they resume. A job the student asked for with Write call prep or Research this
company is their own act and runs whatever the pause.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from . import automation
from . import outreach_research as research
from . import quote_check
from .background import PollingWorker
from .outreach_call_questions import standing_questions
from .outreach_interviewer import (
    TOPICS as INTERVIEWER_TOPICS, who_key, find_interviewer, interviewer_due, interviewer_of,
)
from .agent_providers import CliAgentProvider, complete_text
from .operations import JobDeferred, enqueue_job, recover_stale_jobs, run_next_job
from .outreach import CALL_PREP_STATUSES, OutreachNotFoundError, log_event, get_target, local_today
from .outreach_drafting import (
    DRAFT_FACT_FIELDS, IDENTIFIER_KEYS, INFERENCE_BASIS, RESEARCH_FIELDS, ProviderFactory, ADDRESS_PATTERN, entry_name, field_basis,
    outreach_proof,
)
from .outreach_config import resolve_provider
from .preparation import confirmed_facts
from .schema import connect_product
from .timestamps import utc_now

LOGGER = logging.getLogger(__name__)
JOB_TYPE = "outreach_call_prep"
# Every job type the call prep thread runs.
JOB_TYPES = (JOB_TYPE, research.JOB_TYPE)
# The first try plus three retries, 1, 2, then 4 minutes apart: long enough to
# ride out a laptop waking up and reconnecting.
MAX_ATTEMPTS = 4
ACTIVE_JOB_STATES = {"queued", "running", "retry"}
REPLY_REQUIRED = "Paste their reply under Replies and history first. Call prep is written from it."
# How often a job held by the student's pause is looked at again, so it starts about this soon after they resume.
PAUSE_RECHECK = timedelta(minutes=1)
PAUSED_WAIT = "Waiting: automation is paused, so call prep starts after you resume"
RESEARCH_CUT_OFF = "Research was cut off when the app stopped. Press Research this company to run it again."
RESEARCH_WAIT = "Waiting: the company research you asked for is still to run, so call prep starts after it"

# Researches one company and stores its brief: (conn, target_id, user_id).
Researcher = Callable[[sqlite3.Connection, str, str], Any]

TITLE_LEGEND = "[n] = source at bottom | LI = their LinkedIn | my read = my inference, not stated by them"
ASK_HEADING = "=== ASK (in call order: rapport → their story → product → my role) ==="
TALKING_HEADING = "TALKING POINTS (from my email → where it lands for them)"
KNOW_HEADING = "=== KNOW ==="
READING_HEADING = "MY READ (not stated by them)"
DURING_HEADING = "=== DURING THE CALL ==="
# What each line of the reading is about, in the order they print.
READING_ABOUT = {
    "ideal_customer": "Ideal customer",
    "selling_point": "Edge",
    "enabling_technology": "Enabled by",
    "direction": "Heading",
}
# Short headings for the research, for notes copied out by hand.
FACT_HEADINGS = {
    "product": "WHAT THEY BUILD",
    "customers": "WHO THEY SELL TO",
    "edge": "WHAT SETS THEM APART (their words)",
    "competitors": "COMPETITORS",
    "technology": "HOW IT WORKS",
    "engineering": "BUILT WITH",
    "growth": "EXPANDING",
    "hiring": "HIRING",
    "team": "WHO BUILDS IT",
    "traction": "FUNDING + PARTNERS",
    "news": "NEWS",
}
# The interviewer's LinkedIn notes, by topic, as short labels.
NOTE_LABELS = {"now": "Now", "path": "Before", "education": "Education", "posts": "Posts", "other": "Also"}
FALLBACK_HEADING = "ABOUT THEM (deep search only; no web research yet)"
GAPS_HEADING = "NOT ONLINE (the research agent's list; ask or listen)"
# Bases a talking point may cite.
SENT_EMAIL_BASIS = "sent_email"
# Blanks the student fills in on the call. Written here, not by the model; the
# standing questions add their own before these.
DURING_CALL = (
    "Role details (dates, PT/FT, in person, pay):",
    "Next step + by when:",
    "Thank-you sent:",
)
MAX_QUESTIONS = 7
MAX_TALKING_POINTS = 3
MAX_BULLET_CHARS = 300
MAX_READING_CHARS = 200
MAX_QUESTION_CHARS = 300
MAX_HOOK_CHARS = 200
MAX_LANDS_CHARS = 120
SOURCE_CHARS = 45
# Ids the model uses to say what a line builds on: research facts, notes on the
# interviewer, gaps, and the student's standing questions.
FACT_ID, NOTE_ID, GAP_ID, STANDING_ID = "f", "p", "g", "s"
# How the problems with lines that are simply left out begin. The notes stand
# without those lines, so none holds them back once the retry has had its chance.
READING_PROBLEM = "reading "
DROPPED_QUESTION = "a question "
DROPPED_HOOK = "a standing question's hook "
DROPPED_LANDS = "where a talking point lands "
NO_QUESTIONS = "it gives no questions"
# What the research agent's pick of a competitor is called in the model's input, and in the notes.
PICK_BASIS = "the research agent's pick; the page does not say they compete"
PICK_MARK = "the research agent's pick of a competitor"
UNCONFIRMED_MARK = "(profile not confirmed as them)"
_COMPETES = re.compile(r"\b(?:compet\w*|rival\w*)\b", re.IGNORECASE)
# A line that states something about the company or the person, as against asking about it.
_STATES = re.compile(
    r"\b(?:i (?:read|saw|noticed|found|know|heard)|you(?:'re|'ve| are| have| built| lead| run| make| use)|they(?:'re| are| have| built| lead| use))\b",
    re.IGNORECASE,
)
# Where a number in the notes may come from: what the student, the research, and the reply say. Not ids, links,
# dates, the agent's list of gaps (it states nothing), or the research on file the model is not sent.
_NOT_A_SOURCE = IDENTIFIER_KEYS | {"sent_on", "logged_on", "research_gaps", "unverified_research", "status"}

INSTRUCTIONS = """You write call-prep notes for one university student. A small company answered the student's cold email about an internship, and the student will talk to someone there soon.

The student copies these notes out by hand, so write every line short and note-like: fragments, "→" for "leads to", "+" for "and", common abbreviations (mfg, eng, FT). Keep every specific: numbers, names, products. No filler, no full sentences where a fragment will do.

The goal of the call is to get the interviewer talking about themselves: people enjoy talking about their own work, and a question that shows the student did the reading invites a real story. What the company does is already in the notes, printed from technical_research; do not repeat it. You write four things:

- questions: 4 to 7 questions, in the order to ask them: first rapport (something the student shares with the interviewer: a school, a field, a city, from the student's profile and the interviewer notes), then the interviewer's own story (their path, a change of field, a decision they made), then the product (how they built it, what was hard, what they would change). When technical_research allows, include one on where the design came from (a founder's earlier company or product) and one on how customers compare them with the other companies technical_research names. Each opens with a short hook from what the student read ("Aerospace test rigs → EV charging robots now.") and asks something open that only this person can answer (how, why, what surprised you, what would you change). Never ask for what the research already answers; build on it. No yes-or-no questions, nothing generic, nothing the standing_questions already ask. Under 30 words each. In "from", list the ids of the facts (f...), interviewer notes (p...), or gaps (g...) each builds on.
- standing: the student's own standing_questions are asked in their words. For each (by its id), you may add a hook from the research that makes it informed ("I read the seed is going into scaling production in Texas."), and an ask that sharpens it but still asks the same thing ("What's the bottleneck in getting there?"). Leave hook and ask empty to keep the student's question as it is. Never repeat or reword the lead_in. List in "from" what the hook builds on.
- talking_points: at most 3 results from the student's profile that the sent email used, short ("BOM $1,500 → $600, across 30 units"), each citing "profile:<field>" or "sent_email". For each, lands_on: a few words on which of this company's needs it answers, from technical_research ("their low-cost arm + scaling production"), with the fact ids in "from"; empty when nothing fits.
- reading: your short reading of the company, built only on technical_research, one line each (under 20 words) for any of: "ideal_customer", "selling_point" (what sets them apart), "enabling_technology" (what makes that possible), "direction" (where they are heading). Each line continues its label, so start it lowercase unless it starts with a name, and lists its fact ids in "facts". Leave out any line the facts do not support.

Rules:
- Use only facts in the JSON input. Never invent achievements, numbers, dates, people, customers, or anything about the company. A line may state only what the facts, notes, and profile it lists carry; a gap is something to ask about, never something to state.
- A technical_research fact with competitor_basis is only the research agent's pick. Never call it a competitor or rival, or say the two companies compete.
- research_gaps is the research agent's own list of what it could not find online. It states nothing.
- interviewer notes come from their LinkedIn profile, checked against it. Use them as hooks; never add to them.
- Plain text, no markdown, no em dashes or en dashes.

Reply with exactly one JSON object and nothing else:
{"questions": [{"text": "...", "from": ["p2", "f4"]}], "standing": [{"id": "s1", "hook": "...", "ask": "...", "from": ["f3"]}], "talking_points": [{"text": "...", "basis": "sent_email", "lands_on": "...", "from": ["f2"]}], "reading": [{"about": "ideal_customer", "text": "...", "facts": ["f1", "f3"]}]}"""

LINE_CHECK_INSTRUCTIONS = """You check lines of a student's call notes against what each line says it is built on. Each item gives a line and the facts or notes it cites, as they were checked earlier.

For each item, answer supported=true only when the line states nothing about the company, its products, its people, or the interviewer beyond what the cited items say. Asking about something is fine; stating it is not. Wording, shortening, and note style are fine. A line that names another company, person, customer, or number the cited items do not carry, or turns a question into a claim, is not supported. When unsure, answer false.

Reply with exactly one JSON object and nothing else:
{"verdicts": [{"id": "...", "supported": true, "why": "under 15 words"}]}"""


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


def reply_read_as_declined(conn: sqlite3.Connection, target: dict[str, Any], user_id: str) -> bool:
    """Whether the newest reply reads as a decline: a suggestion still waiting on the company, or the reading kept with the reply.

    A pasted reply's suggestion is returned to the student and not stored on the
    company, so the reading kept with the reply event is the one to ask. Jev's
    answer stands when it gave one; the rules' otherwise, as when the reply was logged.
    """
    if (target.get("reply_suggestion") or {}).get("status") == "declined":
        return True
    row = conn.execute(
        "SELECT detail_json FROM outreach_events WHERE target_id=? AND user_id=? AND event_type='reply_logged' "
        "ORDER BY created_at DESC LIMIT 1",
        (target["id"], user_id),
    ).fetchone()
    try:
        readings = (json.loads(row[0] or "{}") if row else {}).get("readings") or {}
        jev = readings.get("jev")
        status = (jev or {}).get("label") if isinstance(jev, dict) else None
        return (status or (readings.get("rules") or {}).get("status")) == "declined"
    except (TypeError, ValueError, AttributeError):
        return False


def current_interviewer(conn: sqlite3.Connection, target: dict[str, Any], user_id: str) -> dict[str, Any]:
    """The target with its interviewer record only when the record is about the person named now.

    What the last look-up stored stays on the company when the next look-up
    does not finish (LinkedIn timed out, the app stopped), even after the
    student names someone else. A record for another person is set aside: the
    person named now is shown with no notes, and nothing stored about the
    other one is printed or sent to a model.
    """
    who = find_interviewer(conn, target, user_id)
    key = who_key(who["name"], target.get("interviewer_linkedin") or "")
    record = interviewer_of(target)
    if record.get("key") == key:
        return target
    if not who["name"]:
        return {**target, "interviewer": {}, "interviewer_error": who["evidence"]}
    stand_in = {**who, "key": key, "linkedin": None, "notes": [], "refused": [], "candidate_count": 0}
    return {**target, "interviewer": stand_in, "interviewer_error": ""}


def call_prep_inputs(conn: sqlite3.Connection, target: dict[str, Any], user_id: str) -> dict[str, Any]:
    target = current_interviewer(conn, target, user_id)
    facts = confirmed_facts(conn, user_id)
    student = {field: facts[field] for field in DRAFT_FACT_FIELDS if field in facts}
    if not student.get("name"):
        raise ValueError("Confirm your name in your profile before generating call prep")
    proof, lead = outreach_proof(facts)
    student.update(proof)
    on_file = {field: target[field] for field in (*RESEARCH_FIELDS, "location") if target.get(field)}
    if target.get("research_confidence") == "unverified":
        confirmed = {field: value for field, value in on_file.items() if field in {"company", "website"}}
        unverified = {field: value for field, value in on_file.items() if field not in confirmed}
    else:
        confirmed, unverified = on_file, {}
    brief = research.brief_of(target)
    questions, _ = standing_questions()
    return {
        "student": student,
        "lead_with": lead,
        "company_research": confirmed,
        "unverified_research": unverified,
        # A fact whose page turned the check away is printed in the notes, marked,
        # but nothing is built on it. Ids number the facts the reading cites.
        "technical_research": [
            {"id": f"{FACT_ID}{index}", "section": fact["section"], "text": fact["text"],
             # A rival only the agent picked: the page does not say the two compete.
             **({"competitor_basis": PICK_BASIS, "competitor": str(fact.get("competitor") or "")} if _is_pick(fact) else {})}
            for index, fact in enumerate(checked_facts(target), start=1)
        ],
        "interviewer": interviewer_input(target),
        "standing_questions": [
            {"id": f"{STANDING_ID}{index}", "lead_in": item["lead_in"], "ask": item["ask"]}
            for index, item in enumerate(questions, start=1)
        ],
        "research_gaps": [{"id": f"{GAP_ID}{index}", "text": gap} for index, gap in enumerate(brief.get("gaps") or [], start=1)],
        "source_urls": target["source_urls"],
        "sent_email": {"subject": target.get("email_subject", ""), "body": target.get("email_body", "")} if target.get("email_body") else {},
        # The day only: a full timestamp's digits are not a source for any number.
        "sent_on": str(target.get("sent_at") or "")[:10],
        "replies": logged_replies(target),
        "status": target["status"],
    }


def interviewer_input(target: dict[str, Any]) -> dict[str, Any]:
    """Who the call is with, and their LinkedIn notes when the profile is confirmed to be theirs."""
    record = interviewer_of(target)
    if not record.get("name"):
        return {}
    confirmed = bool((record.get("linkedin") or {}).get("confirmed"))
    return {
        "name": record["name"],
        "notes": [
            {"id": f"{NOTE_ID}{index}", "topic": note["topic"], "text": note["text"]}
            for index, note in enumerate(record.get("notes") or [], start=1)
        ] if confirmed else [],
    }


def checked_facts(target: dict[str, Any]) -> list[dict[str, Any]]:
    """The facts whose quote was found, in brief order: the only ones anything is built on."""
    return [fact for fact in research.brief_of(target).get("facts") or [] if fact.get("checked")]


def _is_pick(fact: dict[str, Any]) -> bool:
    """A competitor only the research agent picked: its page does not say the two companies compete."""
    return fact.get("section") == quote_check.COMPETITORS and str(fact.get("note", "")).startswith("picked")


def _id_list(value: Any) -> list[str]:
    """The ids in a list the model wrote, or the one id it wrote instead of a list."""
    items = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(str(item) for item in items if isinstance(item, (str, int)) and not isinstance(item, bool)))


def _numbers_beyond(text: str, basis: str) -> list[str]:
    """Numbers in ``text`` that ``basis`` does not carry. Whether its names and claims stay within is the second read's question."""
    return sorted(quote_check.number_tokens(quote_check.word_tokens(text)) - quote_check.number_tokens(quote_check.word_tokens(basis)))


def _said_by_id(inputs: dict[str, Any]) -> dict[str, str]:
    """What each fact and interviewer note says, by the id a line cites it with."""
    interviewer = inputs.get("interviewer") or {}
    return {
        **{fact["id"]: fact["text"] for fact in inputs["technical_research"]},
        **{note["id"]: note["text"] for note in interviewer.get("notes") or []},
    }


def _picks(inputs: dict[str, Any]) -> set[str]:
    return {fact["id"] for fact in inputs["technical_research"] if fact.get("competitor_basis")}


def _pick_names(inputs: dict[str, Any]) -> list[str]:
    """The names of the companies only the research agent picked as competitors."""
    return [name for name in (str(fact.get("competitor") or "").strip() for fact in inputs["technical_research"] if fact.get("competitor_basis")) if name]


def _calls_pick_a_competitor(text: str, cited: set[str], inputs: dict[str, Any]) -> bool:
    """Whether a line uses a competition word about a pick: by citing one, or by naming one whatever it cites.

    A model that cites another fact but writes a pick's name beside "competitor"
    has still called it one, so the name is looked for in every line.
    """
    if not _COMPETES.search(text):
        return False
    if _picks(inputs) & cited:
        return True
    return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE) for name in _pick_names(inputs))


def _gap_only(hooks: list[str], inputs: dict[str, Any]) -> bool:
    """Whether a line builds only on the research agent's list of what it could not find."""
    gaps = {gap["id"] for gap in inputs["research_gaps"]}
    return bool(hooks) and all(item in gaps for item in hooks)


def _reading_problems(entry: dict[str, Any], by_id: dict[str, dict[str, Any]], inputs: dict[str, Any] | None = None) -> list[str]:
    """Why one line of the reading cannot stand on the facts it cites."""
    text = entry["text"]
    cited = [by_id[number] for number in entry["facts"]]
    if not cited:
        return [f"reading {entry['about']} cites no technical_research fact"]
    if (any(fact.get("competitor_basis") for fact in cited) and _COMPETES.search(text)) or (
        inputs is not None and _calls_pick_a_competitor(text, set(entry["facts"]), inputs)
    ):
        return [f"reading {entry['about']} calls a company a competitor that only the research agent's pick says is one"]
    numbers = _numbers_beyond(text, " ".join(fact["text"] for fact in cited))
    return [f"reading {entry['about']} states {', '.join(numbers)}, which its facts do not"] if numbers else []


def _validate_reading(value: Any, inputs: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    by_id = {fact["id"]: fact for fact in inputs["technical_research"]}
    kept: list[dict[str, Any]] = []
    problems: list[str] = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("about"), str) or entry["about"] not in READING_ABOUT:
            continue
        text = _text(entry.get("text"), MAX_READING_CHARS)
        if not text or any(item["about"] == entry["about"] for item in kept):
            continue
        item = {"about": entry["about"], "text": text, "facts": [number for number in _id_list(entry.get("facts")) if number in by_id]}
        found = _reading_problems(item, by_id, inputs)
        # A line that goes past its facts is left out, never printed.
        problems += found
        if not found:
            kept.append(item)
    order = list(READING_ABOUT)
    kept.sort(key=lambda item: order.index(item["about"]))
    return kept, problems


def _hook_ids(inputs: dict[str, Any]) -> set[str]:
    return set(_said_by_id(inputs)) | {gap["id"] for gap in inputs["research_gaps"]}


def _text(value: Any, limit: int) -> str:
    """A line the model wrote, or "" when it wrote something other than text."""
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _validate_questions(entries: list[Any], inputs: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Questions with what each builds on.

    A question that states a number the facts and notes it cites do not carry,
    or calls the agent's pick of a competitor a competitor, is left out. One
    that builds on nothing is sent back, when there was anything to build on.
    """
    known, said = _hook_ids(inputs), _said_by_id(inputs)
    kept, problems = [], []
    for entry in entries:
        text = _text(entry.get("text") if isinstance(entry, dict) else entry, MAX_QUESTION_CHARS)
        if not text:
            continue
        hooks = [item for item in _id_list(entry.get("from") if isinstance(entry, dict) else None) if item in known]
        numbers = _numbers_beyond(text, " ".join(said[item] for item in hooks if item in said))
        if numbers:
            problems.append(f"{DROPPED_QUESTION}{text[:60]!r} states {', '.join(numbers)}, which the facts and notes it builds on do not")
            continue
        if _calls_pick_a_competitor(text, set(hooks), inputs):
            problems.append(f"{DROPPED_QUESTION}{text[:60]!r} calls a company a competitor that only the research agent's pick says is one")
            continue
        if _gap_only(hooks, inputs) and ("?" not in text or _STATES.search(text)):
            # What the web does not say is something to ask about: a line built only on it that states something states a guess.
            problems.append(f"{DROPPED_QUESTION}{text[:60]!r} states something about what the research could not find")
            continue
        if known and not hooks:
            problems.append(f"the question {text[:60]!r} builds on nothing in the research or the interviewer notes")
        kept.append({"text": text, "from": hooks})
    return kept, problems


def _validate_standing(value: Any, inputs: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Each standing question's hook and sharper ask, by its id. The student's lead-in is never the model's."""
    ids = {item["id"] for item in inputs["standing_questions"]}
    known, said = _hook_ids(inputs), _said_by_id(inputs)
    kept: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict) or str(entry.get("id")) not in ids or str(entry.get("id")) in kept:
            continue
        hook, ask = _text(entry.get("hook"), MAX_HOOK_CHARS), _text(entry.get("ask"), MAX_QUESTION_CHARS)
        hooks = [item for item in _id_list(entry.get("from")) if item in known]
        numbers = _numbers_beyond(f"{hook} {ask}", " ".join(said[item] for item in hooks if item in said))
        stated = _gap_only(hooks, inputs) and any(part and ("?" not in part or _STATES.search(part)) for part in (hook, ask))
        if (hook or ask) and (numbers or not hooks or stated or _calls_pick_a_competitor(f"{hook} {ask}", set(hooks), inputs)):
            problems.append(f"{DROPPED_HOOK}for {entry['id']} goes past what it builds on")
            continue
        if hook or ask:
            kept[str(entry["id"])] = {"hook": hook, "ask": ask, "from": hooks}
    return kept, problems


def _validate_talking_points(entries: list[Any], inputs: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    allowed = _allowed_bases(inputs)
    by_id = {fact["id"]: fact["text"] for fact in inputs["technical_research"]}
    kept, problems = [], []
    for entry in entries[:MAX_TALKING_POINTS]:
        if not isinstance(entry, dict):
            continue
        text = _text(entry.get("text"), MAX_BULLET_CHARS)
        basis = field_basis(str(entry.get("basis") or "").strip())
        if not text:
            continue
        if basis == INFERENCE_BASIS:
            problems.append(f"talking_points rests {text[:60]!r} on inference; nothing may")
        elif basis not in allowed:
            problems.append(f"talking_points cites {basis or 'nothing'} for {text[:60]!r}, which is not in the inputs")
        lands, lands_from = _text(entry.get("lands_on"), MAX_LANDS_CHARS), [item for item in _id_list(entry.get("from")) if item in by_id]
        if lands and (
            not lands_from or _numbers_beyond(lands, " ".join(by_id[item] for item in lands_from))
            or _calls_pick_a_competitor(lands, set(lands_from), inputs)
        ):
            problems.append(f"{DROPPED_LANDS}for {text[:40]!r} goes past the facts it cites")
            lands, lands_from = "", []
        kept.append({"text": text, "basis": basis, "lands_on": lands, "from": lands_from})
    return kept, problems


def _strings(value: Any):
    """Every piece of text in the inputs a number may come from."""
    if isinstance(value, str):
        yield ADDRESS_PATTERN.sub(" ", value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in _NOT_A_SOURCE:
                yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _unsupported_numbers(text: str, inputs: dict[str, Any]) -> list[str]:
    """Numbers in the notes that are no whole number in the inputs' own words.

    Whole numbers, not pieces of text: a 9 is not found in a 90 or a date.
    Email drafts check whole numbers the same way (outreach_drafting); this one
    skips the same identifier keys (outreach_drafting.IDENTIFIER_KEYS) and more, none of which are the student's own words.
    """
    allowed: set[str] = set()
    for piece in _strings(inputs):
        allowed |= quote_check.number_tokens(quote_check.word_tokens(piece))
    found = [token for token in quote_check.word_tokens(ADDRESS_PATTERN.sub(" ", text)) if token[0].isdigit() and token not in allowed]
    return list(dict.fromkeys(found))


def _allowed_bases(inputs: dict[str, Any]) -> set[str]:
    bases = {f"profile:{field}" for field in inputs["student"]}
    if inputs["sent_email"]:
        bases.add(SENT_EMAIL_BASIS)
    return bases


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())[:MAX_BULLET_CHARS]


def validate_call_prep(raw: str, inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Parse a model reply into its parts and list every reason it cannot be stored as it is."""
    try:
        parsed = CliAgentProvider.extract_json(raw)
    except ValueError as exc:
        raise CallPrepRejected("The model did not return call prep notes") from exc
    sections: dict[str, Any] = {}
    problems: list[str] = []
    questions = parsed.get("questions") if isinstance(parsed.get("questions"), list) else []
    sections["questions"], found = _validate_questions(questions[:MAX_QUESTIONS], inputs)
    problems += found
    sections["standing"], found = _validate_standing(parsed.get("standing"), inputs)
    problems += found
    points = parsed.get("talking_points") if isinstance(parsed.get("talking_points"), list) else []
    sections["talking_points"], found = _validate_talking_points(points, inputs)
    problems += found
    sections["reading"], found = _validate_reading(parsed.get("reading"), inputs)
    problems += found
    if not sections["questions"]:
        problems.append(NO_QUESTIONS)
    written = "\n".join(
        [entry["text"] for entry in sections["questions"]]
        + [f"{entry['hook']} {entry['ask']}" for entry in sections["standing"].values()]
        + [f"{entry['text']} {entry['lands_on']}" for entry in sections["talking_points"]]
        + [entry["text"] for entry in sections["reading"]]
    )
    # The agent's list of gaps is not a source: a number in it counts as found nowhere.
    numbers = _unsupported_numbers(written, inputs)
    if numbers:
        problems.append("it states numbers found in neither your profile, the research, nor the reply: " + ", ".join(numbers))
    if re.search("[–—]", written):
        problems.append("it uses em or en dashes")
    return sections, problems


def second_read_lines(sections: dict[str, Any], inputs: dict[str, Any], judge: Callable[[str, str], str]) -> dict[str, Any]:
    """Check each written line against what it cites, and leave out the ones that state more (LINE_CHECK_INSTRUCTIONS).

    A question or a reading line that goes past its sources is dropped; a
    standing question falls back to the student's own wording; a talking point
    keeps its text without "lands on". An optional line the second read does not
    answer is left out too: the word checks cannot tell a claim from a question,
    so a line nothing has read against its sources is not printed as if it had
    been. The student's own standing questions and the sent email's talking
    points do not depend on it.
    """
    said = _said_by_id(inputs)
    gaps = {gap["id"]: gap["text"] for gap in inputs["research_gaps"]}

    def cited(ids: list[str]) -> list[str]:
        return [said[item] if item in said else f"(a topic the research could not find, to ask about) {gaps[item]}" for item in ids if item in said or item in gaps]

    items = []
    for index, entry in enumerate(sections["questions"]):
        items.append({"id": f"q{index}", "line": entry["text"], "cites": cited(entry["from"])})
    for key, entry in sections["standing"].items():
        items.append({"id": f"s:{key}", "line": f"{entry['hook']} {entry['ask']}".strip(), "cites": cited(entry["from"])})
    for index, entry in enumerate(sections["talking_points"]):
        if entry["lands_on"]:
            items.append({"id": f"t{index}", "line": f"lands on: {entry['lands_on']}", "cites": cited(entry["from"])})
    for index, entry in enumerate(sections["reading"]):
        items.append({"id": f"r{index}", "line": entry["text"], "cites": cited(entry["facts"])})
    verdicts = quote_check.second_read(items, judge, LINE_CHECK_INSTRUCTIONS)
    yes = {key for key, (supported, _why) in verdicts.items() if supported}
    return {
        "questions": [entry for index, entry in enumerate(sections["questions"]) if f"q{index}" in yes],
        "standing": {key: entry for key, entry in sections["standing"].items() if f"s:{key}" in yes},
        "talking_points": [
            entry if not entry["lands_on"] or f"t{index}" in yes else {**entry, "lands_on": "", "from": []}
            for index, entry in enumerate(sections["talking_points"])
        ],
        "reading": [entry for index, entry in enumerate(sections["reading"]) if f"r{index}" in yes],
    }


def template_call_prep(inputs: dict[str, Any]) -> dict[str, Any]:
    """Deterministic notes when no model provider is configured: facts only, no inference."""
    points = []
    for field in ("experience", "projects"):
        for entry in inputs["student"].get(field, []):
            if not isinstance(entry, dict) or entry_name(entry) not in inputs["lead_with"]:
                continue
            points += [{"text": _clean(line), "basis": f"profile:{field}", "lands_on": "", "from": []} for line in entry.get("highlights", [])]
    company = inputs["company_research"].get("company", "the company")
    return {
        "questions": [{"text": text, "from": []} for text in (
            f"What is {company} working on over the next few months?",
            "What would a first project look like?",
        )],
        "standing": {},
        "talking_points": points[:MAX_TALKING_POINTS],
        "reading": [],
    }


def fallback_about(inputs: dict[str, Any]) -> list[dict[str, str]]:
    """The company side without technical research: the one-line research on file, as facts."""
    on_file = {**inputs["company_research"], **inputs["unverified_research"]}
    unverified = set(inputs["unverified_research"])

    def basis(field: str) -> str:
        return f"{'unverified' if field in unverified else 'research'}:{field}"

    about = [
        {"text": _clean(on_file[field]), "basis": basis(field)}
        for field in ("summary", "activity_signal", "location") if on_file.get(field)
    ]
    if on_file.get("contact_name"):
        who = ", ".join(part for part in (on_file["contact_name"], on_file.get("contact_role", "")) if part)
        about.append({"text": f"Contact: {who}", "basis": basis("contact_name")})
    return about


def research_line(target: dict[str, Any], researched_on: str = "") -> str:
    """Where the company side came from, or why it is thin, in one short line. ``researched_on`` is the student's local date."""
    facts = research.brief_of(target).get("facts") or []
    error = " ".join(str(target.get("tech_brief_error") or "").split())
    if error and not error.endswith("."):
        error += "."
    if facts:
        sources = len({fact["source_url"] for fact in facts})
        unchecked = sum(1 for fact in facts if not fact.get("checked"))
        line = f"Web research {researched_on or str(target.get('tech_brief_at') or '')[:10]}: {len(facts)} facts, {sources} pages, each confirmed on its page"
        if unchecked:
            line += f" except {unchecked} marked (not checked)"
        line += "."
        if error:
            line += f" Latest research did not replace it: {error}"
        return line
    if error:
        return f"No web research yet: {error} Press Research this company under Research, then Rewrite call prep."
    return "No web research yet. Press Research this company under Research, then Rewrite call prep."


def interviewer_lines(target: dict[str, Any], claims: list[dict[str, Any]], read_on: str = "") -> list[str]:
    """Who the call is with, how the mailbox says so, and their LinkedIn notes, in short lines."""
    record = interviewer_of(target)
    error = " ".join(str(target.get("interviewer_error") or "").split())
    if not record.get("name"):
        return ["", "WHO YOU'RE TALKING TO", f"- Not known yet{': ' + error if error else ''}. Add their name under Call prep."]
    # How the mailbox knows, in a few words; the pane keeps the full evidence.
    how = {"mailbox_invitation": "sent the invite", "mailbox_sender": "wrote to you last"}.get(record.get("basis") or "", record.get("evidence") or "")
    said = " | ".join(part for part in (record.get("email") or "", how) if part)
    lines = ["", f"{record['name'].upper()}{f'  ({said})' if said else ''}"]
    others = [person["name"] for person in record.get("others") or []]
    if others:
        lines.append(f"- Also wrote to you: {', '.join(others)}")
    linkedin = record.get("linkedin") or {}
    if linkedin:
        confirmed = bool(linkedin.get("confirmed"))
        # Notes from a profile not confirmed as theirs carry the mark in the notes and in the claims list alike.
        mark = "" if confirmed else f" {UNCONFIRMED_MARK}"
        for topic, label in NOTE_LABELS.items():
            for note in [note for note in record.get("notes") or [] if note["topic"] == topic]:
                lines.append(f"- {label}: {note['text']}{mark}")
                claims.append({"text": f"{label}: {note['text']}{mark}", "basis": linkedin["url"], "section": "interviewer"})
        read_on = read_on or str(linkedin.get("read_at") or "")[:10]
        if confirmed:
            lines.append(f"- LI read {read_on} via test account: {_short_source(linkedin['url'])}")
        else:
            why = linkedin.get("why") or f"it never names {target['company']}"
            lines.append(f"- LI {_short_source(linkedin['url'])} (read {read_on}): {why}; check it is them. Nothing is built on it.")
    elif error:
        lines.append(f"- No LinkedIn notes: {error}")
    else:
        lines.append("- LinkedIn not read yet.")
    return lines


def _short_source(url: str) -> str:
    """A source as it is worth copying by hand: its host and the start of its path."""
    parts = urlsplit(url)
    short = f"{(parts.hostname or '').removeprefix('www.')}{parts.path.rstrip('/')}"
    return short if len(short) <= SOURCE_CHARS else f"{short[:SOURCE_CHARS].rstrip('/-')}..."


def render_call_prep(
    target: dict[str, Any], sections: dict[str, Any], inputs: dict[str, Any], generated_on: str,
    researched_on: str = "", linkedin_on: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """Plain-text notes to copy by hand, and the claims behind every cited line.

    ASK first, in call order, then TALKING POINTS, KNOW, DURING THE CALL, and
    the sources. Dates are the student's local dates.
    """
    brief = research.brief_of(target)
    facts = brief.get("facts") or []
    questions, questions_note = standing_questions()
    record = interviewer_of(target)
    sources: dict[str, int] = {}
    claims: list[dict[str, Any]] = []
    for fact in facts:
        sources.setdefault(fact["source_url"], len(sources) + 1)
    checked = checked_facts(target)
    fact_sources = {f"{FACT_ID}{index}": sources[fact["source_url"]] for index, fact in enumerate(checked, start=1)}
    # The numbers of the sources that only the research agent's pick of a competitor stands on.
    pick_sources = {fact_sources[f"{FACT_ID}{index}"] for index, fact in enumerate(checked, start=1) if _is_pick(fact)}
    headings = {key: FACT_HEADINGS.get(key, heading) for key, heading in quote_check.SECTIONS if any(fact["section"] == key for fact in facts)}

    def builds_on(hooks: list[str]) -> str:
        parts = [str(number) for number in sorted({fact_sources[hook] for hook in hooks if hook in fact_sources})]
        if any(hook.startswith(NOTE_ID) for hook in hooks):
            parts.append("LI")
        if any(hook.startswith(GAP_ID) for hook in hooks):
            parts.append("not online")
        picked = any(fact_sources.get(hook) in pick_sources for hook in hooks)
        return f" [{', '.join(parts)}{'; ' + PICK_MARK if picked else ''}]" if parts else ""

    meeting = " | ".join(part for part in (record.get("meeting") or "", record.get("name") or "") if part)
    read_part = f" | LI read {linkedin_on}" if linkedin_on and (record.get("linkedin") or {}).get("confirmed") else ""
    lines = [
        f"{target['company'].upper()}: CALL PREP",
        *([meeting] if meeting else []),
        TITLE_LEGEND,
        f"Written {generated_on}{read_part}. {research_line(target, researched_on)}",
    ]

    # ASK, in call order: the model's questions (rapport, their story, the product), then the student's own.
    lines += ["", ASK_HEADING]
    if questions_note:
        lines.append(f"({questions_note}.)")
    number = 0
    for entry in sections.get("questions") or []:
        number += 1
        lines.append(f"{number}. {entry['text']}{builds_on(entry.get('from') or [])}")
    tailored = sections.get("standing") or {}
    for index, item in enumerate(questions, start=1):
        number += 1
        extra = tailored.get(f"{STANDING_ID}{index}") or {}
        ask = extra.get("ask") or item["ask"]
        words = " ".join(part for part in (item["lead_in"], extra.get("hook", ""), ask) if part)
        lines.append(f"{number}. {words}{builds_on(extra.get('from') or [])}")
        ready = [headings[key] for key in item["research"] if key in headings]
        if ready:
            lines.append(f"   ready: {', '.join(ready)}")
        elif item["research"]:
            lines.append("   ready: nothing found online; listen for it" if facts else "   ready: no web research yet")

    points = sections.get("talking_points") or []
    if points:
        lines += ["", TALKING_HEADING]
        for entry in points:
            lands = f" → {entry['lands_on']}{builds_on(entry.get('from') or [])}" if entry.get("lands_on") else ""
            lines.append(f"- {entry['text']}{lands}")
            claims.append({"text": entry["text"], "basis": entry["basis"], "section": "talking_points"})

    # KNOW: the interviewer, the reading, the research, and what the web does not say.
    lines += ["", KNOW_HEADING]
    lines += interviewer_lines(target, claims, linkedin_on)
    reading = sections.get("reading") or []
    if reading:
        lines.extend(["", READING_HEADING])
        for entry in reading:
            numbers = sorted({fact_sources[item] for item in entry["facts"] if item in fact_sources})
            picks = [str(item) for item in numbers if item in pick_sources]
            label = READING_ABOUT[entry["about"]]
            lines.append(
                f"- {label}: {entry['text']} [{', '.join(map(str, numbers))}"
                f"{'; ' + ' and '.join(picks) + ' is ' + PICK_MARK if picks else ''}]"
            )
            claims.append({"text": f"{label}: {entry['text']}", "basis": INFERENCE_BASIS, "section": "reading"})
    if facts:
        for key, heading in headings.items():
            if key == quote_check.COMPETITORS and all(_is_pick(fact) for fact in facts if fact["section"] == key):
                heading = f"{heading} (the research agent's picks)"
            lines.extend(["", heading])
            for fact in [fact for fact in facts if fact["section"] == key]:
                mark = "" if fact.get("checked") else " (not checked)"
                if _is_pick(fact) and not heading.endswith("picks)"):
                    mark += f" ({PICK_MARK})"
                lines.append(f"- {fact['text']} [{sources[fact['source_url']]}]{mark}")
                claims.append({"text": f"{fact['text']}{mark}", "basis": fact["source_url"], "section": key})
    else:
        about = fallback_about(inputs)
        if about:
            lines.extend(["", FALLBACK_HEADING, *(
                f"- {entry['text']}{' (unverified)' if entry['basis'].startswith('unverified:') else ''}" for entry in about
            )])
            claims.extend({**entry, "section": "about_them"} for entry in about)
    gaps = brief.get("gaps") or []
    if gaps:
        lines.extend(["", GAPS_HEADING, *(f"- {gap}" for gap in gaps)])

    blanks = [*(item["blank"] for item in questions if item["blank"]), *DURING_CALL]
    lines += ["", DURING_HEADING, *(f"- {blank} " for blank in blanks)]
    linkedin = record.get("linkedin") or {}
    if sources or linkedin:
        lines += ["", "SOURCES", *(f"{number:<2} {_short_source(url)}" for url, number in sources.items())]
        if linkedin:
            lines.append(f"LI {_short_source(linkedin['url'])}")
    text = "\n".join(line.rstrip() if not line.startswith("- ") or line.strip() != "-" else line for line in lines)
    return text, claims


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
    # Only the person named now: a record stored for someone else is set aside.
    target = current_interviewer(conn, target, user_id)
    inputs = call_prep_inputs(conn, target, user_id)
    provider_id, model = resolve_provider(provider, purpose="call_prep")
    if provider_id == "legacy":
        sections = template_call_prep(inputs)
        generated_by = "template"
    else:
        agent = provider_factory(provider_id, model)
        judge = lambda instructions, text: complete_text(agent, instructions, text, max_output_tokens=2000)  # noqa: E731
        # The model is not sent the research on file that the notes print as unverified: nothing may be said from it.
        content = json.dumps({key: value for key, value in inputs.items() if key != "unverified_research"}, ensure_ascii=False, indent=2)
        raw = complete_text(agent, INSTRUCTIONS, content, max_output_tokens=2500)
        sections, problems = validate_call_prep(raw, inputs)
        if problems:
            first = sections, problems
            retry = f"{content}\n\nYour previous notes were rejected because " + "; ".join(problems) + ". Write them again following every rule."
            raw = complete_text(agent, INSTRUCTIONS, retry, max_output_tokens=2500)
            sections, problems = validate_call_prep(raw, inputs)
            if _held_back(problems) and not _held_back(first[1]):
                # The retry is worse: the first notes only had lines that are left out, so they stand.
                sections, problems = first
        # The reading and the questions that go past their sources are optional: after the retry they are
        # already left out, and the rest of the notes stand.
        left_out = [problem for problem in problems if problem.startswith(DROPPED_QUESTION)]
        problems = _held_back(problems)
        if NO_QUESTIONS in problems:
            # Nothing is left of the questions: say why each one was.
            problems += left_out
        if problems:
            raise CallPrepRejected("The call prep was not grounded in your profile, the research, and the reply: " + "; ".join(problems))
        sections = second_read_lines(sections, inputs, judge)
        generated_by = f"{provider_id}:{model}"

    timestamp = utc_now()
    written_on = local_today(conn, user_id).isoformat()
    researched_on = ""
    if target.get("tech_brief_at"):
        try:
            researched_on = local_today(conn, user_id, _aware(target["tech_brief_at"])).isoformat()
        except ValueError:
            researched_on = ""
    read_at = (interviewer_of(target).get("linkedin") or {}).get("read_at")
    linkedin_on = local_today(conn, user_id, _aware(read_at)).isoformat() if read_at else ""
    text, claims = render_call_prep(target, sections, inputs, written_on, researched_on, linkedin_on)
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
            log_event(conn, target_id, user_id, "call_prep_replaced", detail=current)
        conn.execute(
            """
            UPDATE outreach_targets
            SET call_prep=?, call_prep_claims_json=?, call_prep_generated_by=?, call_prep_generated_at=?, updated_at=?
            WHERE id=? AND user_id=?
            """,
            (text, json.dumps(claims, ensure_ascii=False), generated_by, timestamp, timestamp, target_id, user_id),
        )
        log_event(conn, target_id, user_id, "call_prep_generated", detail=generated_by)
    return get_target(conn, target_id, user_id=user_id)


def _held_back(problems: list[str]) -> list[str]:
    """The problems that keep notes from being stored: not the lines already left out for going past their sources."""
    return [problem for problem in problems if not problem.startswith((READING_PROBLEM, DROPPED_QUESTION, DROPPED_HOOK, DROPPED_LANDS))]


def _aware(stamp: str) -> datetime:
    when = datetime.fromisoformat(str(stamp))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def is_automatic(payload: dict[str, Any]) -> bool:
    """Whether a job was started by the app rather than by the student's click.

    Jobs queued before this was recorded say so by ``replace``: only the
    student's Write call prep replaces notes.
    """
    return bool(payload.get("automatic", not payload.get("replace")))


def queue_call_prep(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, replace: bool, reason: str, automatic: bool = False,
) -> dict[str, Any]:
    """Queue a background job to write call prep, unless one is already on its way.

    ``replace`` is the student asking for new notes; without it the job leaves
    any notes already there alone, which is what an automatic start wants.
    ``automatic`` marks a job the app started on its own, which a pause holds.
    The student asking while an automatic job still waits in line makes it
    theirs, so a pause no longer holds it.
    """
    target = get_target(conn, target_id, user_id=user_id, include_events=True)
    check_can_prep(target)
    job = target.get("call_prep_job")
    payload = {"target_id": target_id, "user_id": user_id, "replace": replace, "automatic": automatic}
    if job and job["state"] in ACTIVE_JOB_STATES:
        waiting = conn.execute(
            "SELECT payload_json FROM job_queue WHERE id=? AND state IN ('queued', 'retry')", (target["call_prep_job_id"],),
        ).fetchone()
        if not automatic and waiting is not None and is_automatic(json.loads(waiting[0])):
            with conn:
                if conn.execute(
                    "UPDATE job_queue SET payload_json=?, next_attempt_at=?, updated_at=? WHERE id=? AND state IN ('queued', 'retry')",
                    (json.dumps(payload), utc_now(), utc_now(), target["call_prep_job_id"]),
                ).rowcount:
                    log_event(conn, target_id, user_id, "call_prep_queued", detail=reason)
        return get_target(conn, target_id, user_id=user_id)
    queued = enqueue_job(
        conn, JOB_TYPE, payload, f"call-prep:{target_id}:{uuid4().hex}", max_attempts=MAX_ATTEMPTS,
    )
    with conn:
        conn.execute(
            "UPDATE outreach_targets SET call_prep_job_id=?, updated_at=? WHERE id=? AND user_id=?",
            (queued["id"], utc_now(), target_id, user_id),
        )
        log_event(conn, target_id, user_id, "call_prep_queued", detail=reason)
    return get_target(conn, target_id, user_id=user_id)


def auto_queue_call_prep(conn: sqlite3.Connection, target_id: str, *, user_id: str, reason: str) -> bool:
    """Start call prep on its own when a replied company has a reply and no notes yet."""
    try:
        target = get_target(conn, target_id, user_id=user_id, include_events=True)
        if target.get("call_prep") or (target.get("call_prep_job") or {}).get("state") in ACTIVE_JOB_STATES:
            return False
        if target.get("not_interested_at"):
            return False
        check_can_prep(target)
    except (OutreachNotFoundError, ValueError):
        return False
    # Queued even while paused: the worker holds it until the student resumes.
    queue_call_prep(conn, target_id, user_id=user_id, replace=False, reason=reason, automatic=True)
    return True


class CallPrepWorker(PollingWorker):
    """Runs queued call prep and company research jobs on one background thread inside the web app.

    The jobs live in job_queue, so nothing is lost when the process stops. On
    start, a job still marked running was cut off by the last shutdown, since
    only this thread runs these jobs, and goes back in line. A job whose model
    call fails, as one cut off by a sleeping laptop does, is retried by the
    queue with backoff; the thread polls, so a retry runs soon after it is due.

    ``researcher`` researches a company from the web (outreach_research.py).
    A call prep job runs it first when the company's brief is missing or
    stale; without one, call prep uses whatever research is on file.
    ``interviewer`` finds who the call is with and reads their LinkedIn
    (outreach_interviewer.py), when interviewer_due says so.
    """

    thread_name = "call-prep-worker"
    failure_message = "Call prep worker pass failed"
    logger = LOGGER

    def __init__(
        self,
        platform_target: Path | str,
        *,
        provider_factory: ProviderFactory,
        provider: str | None = None,
        researcher: Researcher | None = None,
        interviewer: Researcher | None = None,
        poll_seconds: float = 20.0,
    ) -> None:
        self.platform_target = platform_target
        self._provider_factory = provider_factory
        self._provider = provider
        self._researcher = researcher
        self._interviewer = interviewer
        super().__init__(poll_seconds)

    def recover_interrupted(self) -> int:
        with closing(connect_product(self.platform_target)) as conn:
            cut_off = conn.execute(
                "SELECT id, payload_json FROM job_queue WHERE state='running' AND job_type=?", (research.JOB_TYPE,),
            ).fetchall()
            recovered = recover_stale_jobs(
                conn, stale_before="9999-12-31T00:00:00+00:00", job_types=JOB_TYPES,
                reason="Interrupted when the app stopped; trying again",
            )
            # A research job the shutdown cut off already spent its day's try; it is marked so it does not search again.
            with conn:
                for row in cut_off:
                    try:
                        payload = json.loads(row[1] or "{}")
                    except ValueError:
                        payload = {}
                    conn.execute(
                        "UPDATE job_queue SET payload_json=? WHERE id=? AND state='retry'",
                        (json.dumps({**payload, "recovered": True}), row[0]),
                    )
            return recovered

    def _run(self, payload: dict[str, Any]) -> None:
        with closing(connect_product(self.platform_target)) as conn:
            if is_automatic(payload) and automation.paused(conn, payload["user_id"]):
                # Asked before anything leaves for the model; the job waits, as it was, for the student to resume.
                raise JobDeferred(PAUSED_WAIT, datetime.now(timezone.utc) + PAUSE_RECHECK)
            try:
                self._research_first(conn, payload)
                self._interviewer_first(conn, payload)
                if is_automatic(payload) and automation.paused(conn, payload["user_id"]):
                    # Paused while the research ran: the brief is kept, and the
                    # reply goes to the model only after the student resumes.
                    raise JobDeferred(PAUSED_WAIT, datetime.now(timezone.utc) + PAUSE_RECHECK)
                generate_call_prep(
                    conn, payload["target_id"], user_id=payload["user_id"],
                    provider_factory=self._provider_factory, provider=self._provider,
                    keep_existing=not payload.get("replace"),
                )
            except (OutreachNotFoundError, ReplyRequired, NotReplied):
                # Removed, its reply gone, or moved off a reply status while the
                # job waited: nothing to write. Anything else is retried.
                return

    @property
    def can_research(self) -> bool:
        return self._researcher is not None

    def _research_first(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        """Research the company first when research_due says so, unless the notes would not be written anyway.

        research_due allows one try a day, so a retried job does not search
        again. An automatic job on a reply the inbox read as a decline writes
        its notes from what is on file: a no needs no research. A failure is
        recorded on the company by the researcher and never holds up the notes:
        they say the research is missing and why.
        """
        if self._researcher is None:
            return
        target = get_target(conn, payload["target_id"], user_id=payload["user_id"], include_events=True)
        check_can_prep(target)
        if not payload.get("replace") and target.get("call_prep"):
            return
        if not research.brief_is_fresh(target) and (target.get("tech_brief_job") or {}).get("state") in research.ACTIVE_JOB_STATES:
            # The student's Research this company is already on its way: the notes wait for it, so they
            # are written from what it finds, not from a brief it is about to replace.
            if not (is_automatic(payload) and reply_read_as_declined(conn, target, payload["user_id"])):
                raise JobDeferred(RESEARCH_WAIT, datetime.now(timezone.utc) + PAUSE_RECHECK)
        if not research.research_due(target):
            return
        if is_automatic(payload) and reply_read_as_declined(conn, target, payload["user_id"]):
            return
        try:
            self._researcher(conn, payload["target_id"], payload["user_id"])
        except Exception as exc:  # noqa: BLE001 - recorded on the company; the notes still get written
            LOGGER.warning("Research before call prep failed for %s: %s", payload["target_id"], exc)

    def _interviewer_first(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        """Look up who the call is with, under the same rules as the research: never for notes that stay as they are,
        never for an automatic job on a decline, and never while paused. A failure is stored with the reason."""
        if self._interviewer is None:
            return
        target = get_target(conn, payload["target_id"], user_id=payload["user_id"], include_events=True)
        # The research before this can take minutes, and the company may have been marked Declined meanwhile.
        check_can_prep(target)
        if (not payload.get("replace") and target.get("call_prep")) or not interviewer_due(conn, target, payload["user_id"]):
            return
        if is_automatic(payload) and (
            reply_read_as_declined(conn, target, payload["user_id"]) or automation.paused(conn, payload["user_id"])
        ):
            return
        try:
            self._interviewer(conn, payload["target_id"], payload["user_id"])
        except Exception as exc:  # noqa: BLE001 - the notes still get written
            LOGGER.warning("Looking up the interviewer failed for %s: %s", payload["target_id"], exc)

    def research_problem(self) -> str:
        """Why company research cannot run now, or "": none wired into this app, or its research CLI is missing."""
        if self._researcher is None:
            return "Company research is not available in this app. It runs in the app on your own database."
        check = getattr(self._researcher, "problem", None)
        return check() if callable(check) else ""

    def _run_research(self, payload: dict[str, Any]) -> None:
        """The student's Research this company: their own act, so it runs whatever the pause.

        With no research CLI on this computer the job ends at once: the reason is
        stored on the company, and retrying would only fail the same way.
        """
        if self._researcher is None:
            raise research.ResearchUnavailable("Company research is not set up in this app")
        with closing(connect_product(self.platform_target)) as conn:
            if payload.get("recovered"):
                # Cut off by the app stopping: its try was spent (the day's one is on the company), so it is not run again on its own.
                try:
                    target = get_target(conn, payload["target_id"], user_id=payload["user_id"])
                except OutreachNotFoundError:
                    return
                if not research.research_due({**target, "tech_brief_job": None}):
                    if not research.brief_is_fresh(target):
                        research.record_error(
                            conn, payload["target_id"], user_id=payload["user_id"],
                            error=RuntimeError(RESEARCH_CUT_OFF),
                        )
                    return
            try:
                self._researcher(conn, payload["target_id"], payload["user_id"])
            except (OutreachNotFoundError, research.ResearchUnavailable):
                return

    def run_pending(self) -> int:
        """Run every job that is due now. Returns how many ran."""
        ran = 0
        with closing(connect_product(self.platform_target)) as conn:
            while not self._stop.is_set():
                record = run_next_job(conn, {JOB_TYPE: self._run, research.JOB_TYPE: self._run_research}, only_handled=True)
                if record is None:
                    return ran
                ran += 1
                if record["state"] in {"retry", "dead"}:
                    LOGGER.warning("%s job %s: %s (%s)", record.get("job_type", "Call prep"), record["id"], record["state"], record["last_error"])
        return ran

    def before_start(self) -> None:
        self.recover_interrupted()

    def _run_pass(self) -> None:
        self.run_pending()
