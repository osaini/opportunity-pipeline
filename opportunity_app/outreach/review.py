"""Checks made just before the app sends an email on its own.

Two gates, both fail closed: when a check cannot be made, the email waits.

- ``fresh_look``: right before an automatic send, Gmail is read again for a
  bounce or a reply about this company, rather than trusting the last
  background check, which can be minutes old.
- ``review_follow_up``: a follow-up is read by a second model before it goes
  out, with the whole thread: the first email, every reply and automatic reply,
  and the student's facts. It answers a fixed set of questions, and the
  follow-up goes only on a clean pass. An out-of-office reply that names a
  return date holds it until then. By default the reviewer is Codex, a
  different family from the Claude drafter, so it does not share the drafter's
  blind spots. The student picks the reviewer under AI models
  (PIPELINE_OUTREACH_REVIEW_PROVIDER); on Automatic it is a different family
  from the follow-up writer when one is set up, and says so when none is.

Plain code decides everything it can (a reply exists, the first email
bounced); the model is asked only for what needs reading.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from ..integrations import agent_providers
from .agents import Runner
from .config import REVIEW_ENV, resolve_provider
from ..integrations.agent_providers import CLAUDE_NO_TOOLS, cli_binary, codex_command, codex_failure_detail, failure_detail, run_headless
from .targets import get_target
from .delivery import check_deliveries
from .inbox import OnReply, capture_replies
from ..integrations.gmail_client import ClientFactory, GmailThrottled
from ..student.preparation import confirmed_facts
from ..integrations.typesafe_decisions import DecisionClient

REVIEW_TIMEOUT_SECONDS = 240

REVIEW_INSTRUCTIONS = """You check a follow-up email before it is sent automatically on a university student's behalf.
Nobody else reads it first, so be strict: when unsure, do not send.

The JSON input has today's date where the recipient is, the company, the current contact, the first email the
student sent and when, every reply and automatic reply that came back, and the follow-up about to go out.

Answer each question:
1. Has anyone at the company replied (not an automatic reply)? Then do not send.
2. Does an automatic reply say the contact is away, on leave, or out of office? If it gives a return date, put it in
   away_until as YYYY-MM-DD, and do not send if today is before it. If it gives no date, do not send.
3. Does the follow-up state anything the first email or the student's facts do not support: a new achievement,
   number, date, mutual contact, or claim about the company? Then do not send.
4. Is it addressed to the current contact and the right company, and does its greeting fit them? If not, do not send.
5. Is it short, polite, and free of pressure, guilt, or sarcasm? If not, do not send.

List every problem you found, in one plain sentence each. send is true only when you found none.
Reply with exactly one JSON object and nothing else:
{"send": true, "away_until": null, "problems": []}"""


FRESH_LOOK_REASONS = {
    "not_connected": "Gmail is not connected",
    "needs_reconnect": "Gmail needs to be reconnected",
    "unreachable": "Gmail could not be reached",
    "throttled": "Gmail asked the app to slow down",
}


def fresh_look(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, client_factory: ClientFactory,
    decisions: DecisionClient | None = None, on_reply: OnReply | None = None,
) -> dict[str, Any]:
    """Read Gmail again for this company's bounces and replies. ``ok`` is False when Gmail could not be read.

    Both checks report Gmail trouble as a state rather than raising. Should one
    raise anyway, the email still only waits: a slowdown or an unreachable
    Gmail never reaches the send as a failure. ``ok`` says only that Gmail
    could be read: a company capture_replies does not watch
    (outreach_inbox.watched_ids) is not searched at all, so a caller that
    needs its replies checked asks that first. ``decisions`` and ``on_reply``
    are passed to capture_replies, so a reply found here is handled as the
    InboxWatcher would handle it.
    """
    try:
        delivery = check_deliveries(conn, user_id=user_id, client_factory=client_factory, force_target=target_id)
        replies = capture_replies(
            conn, user_id=user_id, client_factory=client_factory, force=True, force_target=target_id,
            decisions=decisions, on_reply=on_reply,
        )
    except GmailThrottled:
        return {"ok": False, "reason": FRESH_LOOK_REASONS["throttled"]}
    except httpx.HTTPError:
        return {"ok": False, "reason": FRESH_LOOK_REASONS["unreachable"]}
    failed = next((state for state in (delivery["state"], replies["state"]) if state != "ok"), "")
    return {"ok": not failed, "reason": FRESH_LOOK_REASONS.get(failed, failed)}


# Which company's models a provider runs: a reviewer from the drafter's own
# family shares its blind spots, so the automatic choice avoids it.
FAMILY = {"claude-code": "anthropic", "anthropic": "anthropic", "codex-cli": "openai", "openai": "openai"}
# Subscriptions first: they cost nothing per review.
AUTOMATIC_ORDER = ("codex-cli", "claude-code", "openai", "anthropic")


def review_choice(purpose: str = "follow_up") -> tuple[str, str]:
    """The provider that reviews an automatic email, and a note: a compromise, or a Codex setting that was left out."""
    provider, note = _review_choice(purpose)
    if provider == "codex-cli":
        note = "; ".join(part for part in (note, *agent_providers.codex_setting_notes()) if part)
    return provider, note


def _review_choice(purpose: str) -> tuple[str, str]:
    """The provider that reviews an automatic email, and a note when it is a compromise.

    The student's pick when it is set up; otherwise a set-up provider from a
    different family than the writer of that ``purpose`` (a follow-up, or the
    thank-you after a decline), else the best one there is.
    """
    catalog = {item["id"]: item for item in agent_providers.provider_catalog()}
    chosen = os.environ.get(REVIEW_ENV, "").strip()
    if chosen:
        if chosen not in catalog:
            raise ValueError(f"{REVIEW_ENV} must be one of: {', '.join(catalog)}")
        if not catalog[chosen]["configured"]:
            raise ValueError(f"{catalog[chosen]['display_name']} is not set up: {catalog[chosen]['setup_hint']}")
        return chosen, ""
    ready = [provider for provider in AUTOMATIC_ORDER if catalog.get(provider, {}).get("configured")]
    if not ready:
        raise ValueError(f"No model is set up on this computer to review {'thank-yous' if purpose == 'thank_you' else 'follow-ups'}")
    try:
        drafter = resolve_provider(None, purpose=purpose)[0]
    except ValueError:
        drafter = ""
    other = [provider for provider in ready if FAMILY.get(provider) != FAMILY.get(drafter)]
    if other:
        return other[0], ""
    writer = "thank-you writer" if purpose == "thank_you" else "follow-up writer"
    return ready[0], f"same family as the {writer}; no other model is set up"


def review_runner(purpose: str = "follow_up") -> tuple[str, Runner]:
    """The reviewer's name and a function that sends it a prompt. CLIs run with no tools and no web."""
    provider, note = review_choice(purpose)
    name = f"{provider} ({note})" if note else provider
    if provider not in {"codex-cli", "claude-code"}:
        model = next(item["model"] for item in agent_providers.provider_catalog() if item["id"] == provider)
        agent = agent_providers.build_provider(provider, model)
        return name, lambda prompt: agent_providers.complete_text(agent, "Reply with exactly one JSON object and nothing else.", prompt)

    def run(prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="outreach-review-") as workdir:
            answer = Path(workdir) / "answer.txt"
            if provider == "codex-cli":
                # No tools, MCP servers or web, and the final message alone from its own file.
                command = codex_command(cli_binary("codex-cli"), extra=("--output-last-message", str(answer)))
            else:
                command = [cli_binary("claude-code"), *CLAUDE_NO_TOOLS]
            completed = run_headless(command, prompt, timeout=REVIEW_TIMEOUT_SECONDS, cwd=workdir)
            if completed.returncode != 0:
                detail = (codex_failure_detail if provider == "codex-cli" else failure_detail)(completed)
                raise RuntimeError(f"{provider} exited {completed.returncode}: {detail[-300:] or 'no output'}")
            return answer.read_text(encoding="utf-8") if answer.exists() else completed.stdout

    return name, run


def _thread(target: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    kinds = {"reply_logged": "replies", "auto_reply": "automatic_replies"}
    thread: dict[str, list[dict[str, str]]] = {"replies": [], "automatic_replies": []}
    for event in reversed(target.get("events") or []):
        if event["event_type"] in kinds and event.get("detail"):
            thread[kinds[event["event_type"]]].append({"on": event["created_at"][:10], "text": event["detail"][:4_000]})
    return thread


def one_answer(output: Any) -> dict[str, Any] | None:
    """The reviewer's answer when its reply is exactly one JSON object, else None.

    A reply with two objects (the example echoed back, then the real answer)
    or anything else around the object is not read as a pass or a hold: it is
    unclear, and unclear holds.
    """
    if not isinstance(output, str):
        return None
    text = output.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        answer = json.loads(text)
    except json.JSONDecodeError:
        return None
    return answer if isinstance(answer, dict) else None


def ask_reviewer(
    runner: Runner, prompt: str, held: dict[str, Any], *, catch: tuple[type[BaseException], ...],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Send the prompt to the reviewer and read its answer, failing closed: (answer, None), or (None, a hold).

    The answer is returned only when it is exactly one object whose "send" is a bool and whose "problems" is a list of
    strings. ``catch`` is what a runner may raise that means "could not run": a follow-up lets any other error
    propagate to its scheduler, the thank-you passes Exception. The messages are shown on the card, and
    outreach_thank_you.notice_reason matches the first by its prefix, so they stay as they are.
    """
    try:
        output = runner(prompt)
    except catch as exc:
        return None, {**held, "problems": [f"The reviewer could not run: {exc}"[:300]]}
    answer = one_answer(output)
    if answer is None:
        return None, {**held, "problems": ["The reviewer's answer could not be read"]}
    send, problems = answer.get("send"), answer.get("problems")
    if not isinstance(send, bool) or not isinstance(problems, list) or not all(isinstance(item, str) for item in problems):
        return None, {**held, "problems": ["The reviewer's answer could not be read"]}
    return answer, None


def review_log_detail(name: str, verdict: dict[str, Any]) -> str:
    """The history line for a review: who passed or held it, and why."""
    return (f"Passed by {name}" if verdict["send"] else f"Held by {name}: " + "; ".join(verdict["problems"]))[:1_000]


def review_follow_up(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, runner: Runner, reviewer: str, today: date,
) -> dict[str, Any]:
    """Whether a follow-up may go out now, with the reviewer's problems and any return date.

    Every failure to get a clear answer is a hold, never a pass.
    """
    target = get_target(conn, target_id, user_id=user_id, include_events=True)
    thread = _thread(target)
    held = {"send": False, "away_until": None, "reviewer": reviewer}
    # Decided without a model: these need no reading.
    if thread["replies"] or target["status"] != "sent":
        return {**held, "problems": ["They already replied, or the company is no longer waiting on a follow-up"]}
    if target["contact_bounced"]:
        return {**held, "problems": [f"Email to {target['contact_email']} bounced"]}
    facts = confirmed_facts(conn, user_id)
    payload = {
        "today_where_they_are": today.isoformat(),
        "company": target["company"],
        "contact": {"name": target["contact_name"], "email": target["contact_email"], "role": target["contact_role"]},
        "student": {field: facts[field] for field in ("name", "school", "degree") if facts.get(field)},
        "first_email": {"sent_on": target["sent_at"], "subject": target["email_subject"], "body": target["email_body"]},
        **thread,
        "follow_up": {"subject": target["follow_up_subject"], "body": target["follow_up_body"]},
    }
    prompt = f"{REVIEW_INSTRUCTIONS}\n\nJSON input:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    answer, hold = ask_reviewer(runner, prompt, held, catch=(RuntimeError, OSError, subprocess.SubprocessError, ValueError))
    if hold is not None:
        return hold
    send, problems, away = answer["send"], answer["problems"], answer.get("away_until")
    away_until = None
    if away:
        try:
            away_until = date.fromisoformat(str(away))
        except ValueError:
            return {**held, "problems": ["The reviewer named a return date that is not a date"]}
    if away_until and away_until > today:
        return {**held, "away_until": away_until, "problems": problems or [f"They are away until {away_until.isoformat()}"]}
    if not send or problems:
        return {**held, "problems": [item[:300] for item in problems] or ["The reviewer did not pass it and gave no reason"]}
    return {"send": True, "away_until": None, "problems": [], "reviewer": reviewer}
