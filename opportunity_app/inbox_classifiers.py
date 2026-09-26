"""Jev for two inbox judgments, with the keyword rules as the fallback.

Both are suggestions the student confirms before anything changes:

- the status a pasted reply to a cold email points to (outreach.log_reply);
- the kind of an application email a connector delivers (connections.ingest_message).

Jev answers only when all of these hold; otherwise the rules answer, and the
result names which one did:

- the student turned Jev inbox suggestions on. It is a per-student setting,
  off by default, because it sends the message text to TypeSafe;
- TYPESAFE_API_KEY is set, so a copy without Jev access loses nothing;
- the request succeeds. A timeout, a rate limit, or a malformed answer falls
  back rather than failing the student's action;
- Jev's top answer has at least MIN_CONFIDENCE. In the 2026-09-25 blind test,
  below it Jev and the rules were each right on fewer than half the messages,
  so the rules' answer stands.

In that test Jev's suggestion matched the adjudicated label on 90% of 100
replies (the rules: 58%) and 82% of 100 application emails (the rules: 41%).
Both sets were synthetic, so these numbers describe that test, not a promise
about real mail.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .schema import utc_now
from .typesafe_decisions import DecisionClient, TypeSafeClient, TypeSafeError

SETTING_KEY = "jev_inbox_suggestions"
QUESTION_SET_VERSION = "inbox-classifiers-v1"
MIN_CONFIDENCE = 0.5
# The student is waiting on a pasted reply, so a slow TypeSafe gives way to the rules quickly.
TIMEOUT_SECONDS = 8.0

ClientFactory = Callable[[], "DecisionClient | None"]

# The wording is the frozen spec the blind test used, so its numbers apply to these questions.
REPLY_QUESTION = {
    "type": "choice",
    "instructions": (
        "A university student sent a cold email to a small company asking about internship opportunities. "
        "The text is the company's reply. Which status best describes where things stand after this reply? "
        "If the reply fits more than one status, use the first that applies in this order: offer, call_scheduled, "
        "paused, declined, replied. (A reply that says they are not hiring but proposes a call is call_scheduled; "
        "one that says not now but get back in touch next spring is paused.)"
    ),
    "criteria": {
        "offer": "The reply offers the student an internship or position.",
        "call_scheduled": "The reply proposes a call or meeting, or asks for the student's availability to talk.",
        "paused": "The reply asks the student to get back in touch at a later time (for example next semester or after a funding round) rather than now.",
        "declined": "The reply says they cannot take the student on, and proposes no call or meeting and no later contact.",
        "replied": "None of the above: for example a question back, a referral to someone else, a plain acknowledgement, or an automatic out-of-office reply.",
    },
}
REPLY_REASONS = {
    "offer": "It mentions an offer",
    "call_scheduled": "It proposes a call or asks for your availability",
    "paused": "It asks you to come back later",
    "declined": "It says they are not hiring or cannot take you on",
    "replied": "They replied; nothing in it points to a more specific outcome",
}

EMAIL_QUESTION = {
    "type": "choice",
    "instructions": (
        "This email arrived in a student's inbox. Which kind of message is it, with respect to the student's job or "
        "internship applications? If the email fits more than one kind, use the first that applies in this order: "
        "offer, rejected, interview, application_confirmation, deadline, recruiter_reply, unknown."
    ),
    "criteria": {
        "offer": "It extends a job or internship offer to the student.",
        "rejected": "It says the student's application will not move forward.",
        "interview": "It invites the student to interview or asks for their availability to interview.",
        "application_confirmation": "It confirms that the student's application was received.",
        "deadline": "It asks the student to complete something by a date (for example an assessment or a form), and is none of the above.",
        "recruiter_reply": "A personal message from a recruiter or someone on a hiring team that is none of the above.",
        "unknown": "None of the above: for example a newsletter, a job alert, marketing, or something unrelated to the student's applications.",
    },
}


def enabled(conn: sqlite3.Connection, *, user_id: str) -> bool:
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, SETTING_KEY),
    ).fetchone()
    return bool(row) and row[0] == "on"


def set_enabled(conn: sqlite3.Connection, value: bool, *, user_id: str) -> bool:
    with conn:
        conn.execute(
            """
            INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (user_id, SETTING_KEY, "on" if value else "off", utc_now()),
        )
    return value


def build_client() -> DecisionClient | None:
    """A TypeSafe client when a key is set, else None. Never raises."""
    try:
        client = TypeSafeClient(timeout=TIMEOUT_SECONDS, max_attempts=2)
    except TypeSafeError:
        return None
    return client if client.configured else None


def client_for(conn: sqlite3.Connection, factory: ClientFactory, *, user_id: str) -> DecisionClient | None:
    """The client to classify with for this student, or None to use the rules."""
    if not enabled(conn, user_id=user_id):
        return None
    try:
        client = factory()
    except TypeSafeError:
        return None
    return client if client is not None and client.configured else None


def _ask(
    client: DecisionClient | None, state: dict[str, Any], question: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Jev's choice and confidence, or None and why the rules answer instead."""
    if client is None:
        return None, ""
    try:
        raw = client.evaluate(state=state, questions={"label": question})
    except TypeSafeError as exc:
        return None, f"Jev was unavailable ({exc})"
    try:
        answer = raw["answers"]["label"]
        confidence = float(answer["confidence"])
        label = str(answer["choice"])
        model = str(raw["model"])
    except (KeyError, TypeError, ValueError):
        return None, "Jev returned an answer this app could not read"
    if label not in question["criteria"]:
        return None, "Jev returned an answer this app could not read"
    if confidence < MIN_CONFIDENCE:
        return None, f"Jev was unsure ({round(confidence * 100)}% sure)"
    return {"label": label, "confidence": confidence, "model": model}, ""


def classify_reply(text: str, rules: Callable[[str], dict[str, str]], client: DecisionClient | None) -> dict[str, Any]:
    """The status a reply suggests, from Jev when it can answer and the rules otherwise."""
    answer, fallback = _ask(client, {"reply": text[:20_000]}, REPLY_QUESTION)
    if answer is None:
        suggestion = dict(rules(text))
        return {**suggestion, "source": "rules", "confidence": None, "model": "", "fallback_reason": fallback}
    percent = round(answer["confidence"] * 100)
    return {
        "status": answer["label"],
        "reason": f"{REPLY_REASONS[answer['label']]} (Jev suggestion, {percent}% sure)",
        "source": "jev", "confidence": answer["confidence"], "model": answer["model"], "fallback_reason": "",
    }


def classify_email(
    subject: str, body: str, rules: Callable[[str, str], tuple[str, float]], client: DecisionClient | None,
) -> tuple[str, float, dict[str, Any]]:
    """Event type, confidence, and how it was decided, for one monitored email."""
    answer, fallback = _ask(client, {"email": {"subject": subject[:1_000], "body": body[:20_000]}}, EMAIL_QUESTION)
    if answer is None:
        event_type, confidence = rules(subject, body)
        return event_type, confidence, {"source": "rules", "model": "", "fallback_reason": fallback}
    return answer["label"], answer["confidence"], {
        "source": "jev", "model": answer["model"], "question_set_version": QUESTION_SET_VERSION, "fallback_reason": "",
    }


def status(conn: sqlite3.Connection, factory: ClientFactory, *, user_id: str) -> dict[str, Any]:
    """What the settings panel shows: whether Jev can run here and whether this student turned it on."""
    try:
        client = factory()
    except TypeSafeError:
        client = None
    return {
        "available": bool(client is not None and client.configured),
        "enabled": enabled(conn, user_id=user_id),
        "min_confidence": MIN_CONFIDENCE,
        "sends": "The text of replies you paste and of application emails a connector delivers.",
    }
