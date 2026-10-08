"""Apply for me: the read-only check a saved Greenhouse or Lever role gets when the student opens it (spec 6.0, 10.3).

``check`` answers "what would the app fill, and what does it still need from me?" without opening a browser.
It reads the role, the student's data and the ATS's public listing (Greenhouse's job board API; for Lever, the posting's own
application page), and it **writes nothing**: no application, no interaction, no event, no run. Opening the section changes
nothing in the tracker (G9).

``answer_missing`` is the one write that goes with it, and it is the student's own act: a question the check
listed as missing is answered once and saved to the answer library, for this company, as a row with no field
ids in it, so it carries over to the next posting at the same company that asks the same words.

Neither returns a field's value. The check names questions, options, sources and sentences only.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline_core.identity import employer_key
from pipeline_core.visibility import capture_visible_sql

from . import ats as apply_ats, classify as apply_classify, lever as apply_lever, policy as apply_policy, runs as apply_runs, sensitive as apply_sensitive
from ..automation import ledger as automation
from ..student import preparation
from ..applications.actions import OpportunityNotFoundError
from .schema_client import PageClient, SchemaClient, SchemaUnavailable
from .checks import question_key

# {ats} is the ATS's display name (apply.ats.name_of), filled where the sentence is said.
NOT_FOUND = "The app couldn't find this posting on {ats}. It may be closed"
NO_ANSWER = "{ats} did not answer. Try again later"
DUPLICATE_TICK = "I know Apply for me handed an application to {company} to {ats} on {date} (it may not have gone through). Apply anyway."
SWITCH_OFF = "Apply for me works with {ats} postings once you turn it on in Apply agent settings"
LEFT_FOR_YOU = ". {count} more {is_are} left for you to answer on the {ats} form"
YOURS_TO_ANSWER = "The app has everything it can fill. {count} question{s_are} yours to answer on the {ats} form"


def not_supported() -> str:
    """What the check says about a role no registered ATS recognises."""
    return f"Apply for me works with {apply_ats.supported_names()} postings only, for now"
SCHEMA_CACHE_SECONDS = 3600.0
# What each field's own words can hold, so a saved answer is not a whole document.
MAX_ANSWER_CHARS = 10_000


class AnswerRefused(ValueError):
    """The student's answer cannot be saved for this question; the message says why."""


class SchemaCache:
    """The parsed listings the check has fetched, kept in memory for an hour so opening a role does not refetch.

    A listing is kept under (ATS, board token, job id): a token is the name of a board within its ATS, so the same token and job id on
    another ATS is another posting. A run always fetches fresh; only the check reads this. Only a listing that came back is kept: a 404
    or an error is asked about again.
    """

    def __init__(self, ttl: float = SCHEMA_CACHE_SECONDS, clock: Any = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._items: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str, str]) -> dict[str, Any] | None:
        with self._lock:
            held = self._items.get(key)
            if held is None or self._clock() - held[0] >= self._ttl:
                self._items.pop(key, None)
                return None
            return copy.deepcopy(held[1])

    def put(self, key: tuple[str, str, str], listing: dict[str, Any]) -> None:
        with self._lock:
            self._items[key] = (self._clock(), copy.deepcopy(listing))


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _opportunity(conn: sqlite3.Connection, user_id: str, opportunity_id: str) -> Any:
    row = conn.execute(
        f"SELECT o.id, o.title, o.company FROM opportunities o WHERE o.id=? AND {capture_visible_sql('o')}", (opportunity_id, user_id),
    ).fetchone()
    if row is None:
        raise OpportunityNotFoundError(opportunity_id)
    return row


def require_opportunity(conn: sqlite3.Connection, user_id: str, opportunity_id: str) -> None:
    """Raise OpportunityNotFoundError unless this student can see the role (an unknown id, or another student's capture)."""
    _opportunity(conn, user_id, opportunity_id)


def _listing(
    client: SchemaClient, cache: SchemaCache | None, ats: str, token: str, job_id: str,
) -> tuple[dict[str, Any] | None, str, bool]:
    """(the listing, a sentence when there is none, whether it came from the cache)."""
    if cache is not None:
        held = cache.get((ats, token, job_id))
        if held is not None:
            return held, "", True
    try:
        listing = client.fetch(token, job_id)
    except SchemaUnavailable:
        return None, NO_ANSWER.format(ats=apply_ats.name_of(ats)), False
    if listing is None:
        return None, NOT_FOUND.format(ats=apply_ats.name_of(ats)), False
    if cache is not None:
        cache.put((ats, token, job_id), listing)
    return listing, "", False


LEVER_RESUME_YOURS = "Your résumé: you attach it yourself {there}, because Lever reads it as soon as it is attached"
LEVER_RESUME_ATTACHED = ("Your résumé: the app attaches it itself, because you let it in Apply agent settings. "
                         "Lever reads it as soon as it is attached, so it is sent to Lever before you press Submit")


def _offers(ats: apply_ats.AtsSpec) -> dict[str, Any]:
    """Which window actions the page may offer for this ATS (rehearsal, Finish in browser), and the sentence for the one that is not there."""
    refusals = {name: apply_ats.mode_refusal(ats, name) for name in ("rehearse", "handoff")}
    missing = refusals["handoff"] or refusals["rehearse"]
    return {"rehearse": refusals["rehearse"] is None, "handoff": refusals["handoff"] is None, "note": missing[1] if missing else ""}


def _asks(
    conn: sqlite3.Connection, user_id: str, opportunity_id: str, ats: str, ident: tuple[str, str], company: str, now: datetime,
) -> tuple[Any, list[dict[str, str]]]:
    """(a block that stops the application outright, or None; the ticks the student would give to go on)."""
    token, job = ident
    acknowledged: list[str] = []
    asks: list[dict[str, str]] = []
    while True:
        block = apply_runs.duplicate_block(
            conn, user_id, opportunity_id=opportunity_id, ats=ats, job_ref=f"{token}/{job}",
            company=employer_key(company), acknowledged=acknowledged, now=now, reading=True,
        )
        if block is None or block.kind != "ask":
            return block, asks
        asks.append({"code": block.code, "message": block.message})
        acknowledged.append(block.code)


def _action(entry: apply_policy.PlanField, facts: dict[str, Any], letter: dict[str, Any] | None = None, ats: str = "greenhouse") -> dict[str, Any]:
    kind = entry.problem_kind
    if kind == "window":
        # Nothing the app can fill and no control here: the student does it in the window. Counted with the questions left to the student.
        return {"type": "manual", "category": "", "words": "", "allowable": False}
    if kind in ("missing_answer", "answer_mismatch"):
        # Apply for me saves an answer for this company only: there is no "use for any company" (spec 7.1 "As built").
        return {"type": "answer", "control": entry.control, "options": list(entry.options), "answer_key": entry.answer_key}
    if kind == "conflicting_answers":
        return {"type": "library"}
    if kind == "name":
        return {"type": "profile", "field": "name_parts"}
    if kind == "profile_fact":
        return {"type": "profile", "field": "contact"}
    if kind.startswith("resume"):
        return {"type": "resume", "chooser": kind == "resume_unsure"}
    if kind.startswith("cover_letter"):
        # No letter yet: Draft one. The latest version is a draft: Open the draft (D11). Both open the Prepare page for this role.
        draft = kind == "cover_letter_draft"
        return {"type": "cover_letter", "state": "draft" if draft else "missing", "document_id": str((letter or {}).get("document_id") or "") if draft else ""}
    if kind == "label_needed":
        suggestion = facts.get(entry.label_field) if entry.label_field in ("school", "degree") else ""
        return {"type": "ats_label", "field": entry.label_field, "suggestion": suggestion if isinstance(suggestion, str) else "", "ats": ats,
                # Look up options is a run in a window that reads the form's own list: Greenhouse has one, Lever's comes later (LV5).
                "lookup": ats != apply_lever.ATS_LEVER}
    if kind in ("sensitive_missing", "sensitive_mismatch"):
        form = _sensitive_form(entry, kind)
        if form:
            return form
    if kind in ("sensitive_never", "sensitive_not_allowed", "sensitive_missing", "sensitive_mismatch"):
        return {"type": "manual", "category": entry.sensitive or "", "words": apply_classify.CATEGORY_WORDS.get(entry.sensitive or "", ""),
                # A category the student may switch on in Apply for me settings, so the view can say so.
                "allowable": kind == "sensitive_not_allowed" and (entry.sensitive or "") in apply_sensitive.STORABLE}
    return {"type": "none"}


def _sensitive_state(entry: apply_policy.PlanField, sources: apply_policy.Sources, company: str) -> str:
    """"missing" or "mismatch" for a sensitive field the student switched on and the plan could not fill, else "".

    Required or optional alike: an optional field left blank has lost its problem, so this asks the store again. A
    stored entry that exists but did not fit (another decline label, a statement's address changed) is a mismatch.
    """
    if not entry.sensitive or entry.sensitive not in sources.sensitive_allowed or entry.source.kind != "none" or not entry.statement or entry.text_cut:
        return ""
    if entry.problem_kind == "sensitive_never":
        # The plan already said the app can neither match nor store this one (a statement too short, or on a field with no tick).
        return ""
    stored = sources.sensitive_lookup(
        category=entry.sensitive, question_key=question_key(entry.statement), company_key=employer_key(company), mode="submit",
        company_only=entry.company_only,
    )
    return "mismatch" if stored else "missing"


def _sensitive_form(entry: apply_policy.PlanField, kind: str) -> dict[str, Any] | None:
    """The Needs you form for a sensitive question the student allowed the app to answer, or None when nothing may be stored.

    An EEO question offers only the form's own decline options: the app never stores a demographic value (D5 C (i)),
    and a form that has no decline option gets no form. A statement is stored word for word, for this company only when
    it points to a document. The category, the wording and the options come from the form, never from the browser.
    """
    category = entry.sensitive or ""
    statement = category in apply_classify.STATEMENT_CATEGORIES
    # A data-processing consent's statement is on the page only, not in Greenhouse's listing, so there is nothing yet to
    # store it under: it is left for the student, or added word for word in Apply agent settings.
    if entry.section == "data_compliance" or entry.text_cut:
        return None
    options = list(entry.options)
    if category in apply_sensitive.EEO_CATEGORIES:
        options = [option for option in options if apply_sensitive.is_decline(option)]
        if entry.control not in ("select",) or not options:
            return None
    elif entry.control not in ("select", "multiselect", "text", "textarea", "checkbox"):
        return None
    if statement and not apply_classify.statement_control(entry.control, entry.options):
        # A statement is stored only as ticked, so a box or a Yes/No question can carry it and a text or list field cannot.
        return None
    return {
        "type": "sensitive", "control": entry.control, "options": options, "category": category,
        "words": apply_classify.CATEGORY_WORDS.get(category, ""), "statement": entry.statement if statement or entry.control == "checkbox" else "", "links": list(entry.links),
        "decline_only": category in apply_sensitive.EEO_CATEGORIES,
        # A question that depends on its company, a mismatch (this form's own wording or address) and a statement that
        # points to a document or leans on text elsewhere are saved for this company only; the tick for any company is then
        # not offered.
        "company_only": entry.company_only or kind == "sensitive_mismatch",
        "consent_text": apply_sensitive.CONSENT_TEXT,
    }


def _problem_view(entry: apply_policy.PlanField, facts: dict[str, Any], letter: dict[str, Any] | None = None, ats: str = "greenhouse") -> dict[str, Any]:
    return {"key": entry.key, "question": entry.question, "required": bool(entry.required), "kind": entry.problem_kind,
            "message": entry.problem, "action": _action(entry, facts, letter, ats)}


def _view(
    plan: apply_policy.Plan, facts: dict[str, Any], letter: dict[str, Any] | None = None, ats: str = "greenhouse",
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    named = False
    for entry in plan.fields:
        if not entry.problem:
            continue
        # First and last name are two fields with one fix.
        if entry.problem_kind == "name":
            if named:
                continue
            named = True
            problems.append({"key": "name_parts", "question": "First and last name for applications", "required": True, "kind": "name",
                             "message": entry.problem, "action": {"type": "profile", "field": "name_parts"}})
            continue
        problems.append(_problem_view(entry, facts, letter, ats))
    fields = [
        {"key": entry.key, "question": entry.question, "required": bool(entry.required), "disposition": entry.disposition,
         "source": entry.source.label or entry.source.kind if entry.source.kind != "none" else "", "note": entry.note,
         "links": list(entry.source.links)}
        for entry in plan.fields
    ]
    filled = [entry for entry in plan.fields if entry.disposition in ("fill", "deferred")]
    counts = {
        "total": len(plan.fields), "filled": len(filled),
        "left_blank": sum(1 for entry in plan.fields if entry.disposition == "blank" and not entry.required),
        "sensitive_left": sum(1 for entry in plan.fields if entry.sensitive and entry.source.kind == "none"),
    }
    return problems, counts, fields


def _prepare(
    conn: sqlite3.Connection, user_id: str, opportunity_id: str, *, client: SchemaClient, cache: SchemaCache | None,
    resume_root: Path | None, moment: datetime, mode: str = "check", key: bytes | None = None, page_client: PageClient | None = None,
) -> tuple[dict[str, Any], apply_policy.Plan | None, apply_policy.Sources | None, list[apply_policy.SchemaField] | None]:
    """6.0 steps 2 to 6: the answer so far, the plan when there is one (None when the check ends earlier), and the parsed
    listing it was built from. Writes nothing. ``mode`` is the plan's ("check", or "rehearse" for a run); ``key`` is the
    install's value-MAC key (None: a fresh one, which only the check can do with). ``client`` reads Greenhouse's listing and
    ``page_client`` Lever's page; an ATS whose client was not given did not answer."""
    opportunity = _opportunity(conn, user_id, opportunity_id)
    company = str(opportunity["company"] or "")
    result: dict[str, Any] = {
        "opportunity_id": opportunity_id, "title": str(opportunity["title"] or ""), "company": company, "ats": "", "status": "unavailable",
        "message": not_supported(), "problems": [], "asks": [], "fields": [], "optional_sensitive": [], "counts": {}, "eligibility": {}, "application": {"exists": False, "stage": ""},
        "checked_at": moment.isoformat(timespec="seconds"), "from_cache": False, "ats_name": "",
        "posting": {"title": "", "company": "", "url": "", "differs": False, "difference": ""},
        # What the page may start for this ATS, and the sentence for what it cannot (Lever has no window action yet), and facts the student should know.
        "offers": {"rehearse": False, "handoff": False, "note": ""}, "notes": [],
    }
    found = apply_ats.identify(conn, opportunity_id)
    if found is None:
        return result, None, None, None
    ats, ident = found
    token, job = ident
    result.update(
        ats=ats.key, ats_name=ats.display_name, board_token=token, job_id=job, canonical_url=apply_ats.canonical_url_of(ats, ident),
        offers=_offers(ats),
    )
    if ats.switch and automation.mode(conn, user_id, ats.switch) != "on":
        return {**result, "message": SWITCH_OFF.format(ats=ats.display_name)}, None, None, None
    application = conn.execute("SELECT stage FROM applications WHERE opportunity_id=? AND user_id=?", (opportunity_id, user_id)).fetchone()
    if application is not None:
        result["application"] = {"exists": True, "stage": str(application["stage"])}
    block, asks = _asks(conn, user_id, opportunity_id, ats.key, ident, company, moment)
    result["asks"] = asks
    if block is not None:
        return {**result, "status": "failed", "message": block.message}, None, None, None
    reader = ats.listings(client, page_client, ident)
    if reader is None:
        return {**result, "status": "failed", "message": NO_ANSWER.format(ats=ats.display_name)}, None, None, None
    listing, sentence, cached = _listing(reader, cache, ats.key, token, job)
    if listing is None:
        return {**result, "status": "failed", "message": sentence}, None, None, None
    result["from_cache"] = cached
    # Which posting was read, so the student can see it, and whether it looks like the role they saved (source integrity).
    difference = ats.posting_difference(company, str(opportunity["title"] or ""), listing, ats_name=ats.display_name)
    result["posting"] = {
        "title": str(listing.get("title") or ""), "company": str(listing.get("company_name") or ""), "url": result["canonical_url"],
        "differs": bool(difference), "difference": difference,
    }
    upload = ats.key == apply_lever.ATS_LEVER and automation.mode(conn, user_id, "apply_lever_resume_upload") == "on"
    sources = apply_policy.sources_for(conn, user_id, opportunity_id, company=company, storage_root=resume_root, key=key, ats=ats.key, resume_upload=upload)
    schema = ats.parse_schema(listing)
    plan = apply_policy.build_plan(
        schema, None, sources, company, mode,
        ats_name=ats.display_name, canonical_url=result["canonical_url"], adapter_version=ats.adapter_version, ats=ats.key,
        window=ats.adapter_built,
    )
    if ats.key == apply_lever.ATS_LEVER:
        there = "in the window" if ats.adapter_built else "on Lever's application page"
        result["notes"] = [LEVER_RESUME_ATTACHED if upload else LEVER_RESUME_YOURS.format(there=there)]
    return result, plan, sources, schema


def _eligibility(
    conn: sqlite3.Connection, user_id: str, result: dict[str, Any], moment: datetime,
) -> dict[str, dict[str, Any]]:
    """Section 7.5's Handoff and Submit columns (and whether a rehearsal may start), for the check's own answer.

    Each is {allowed, needs_tick, reason}. ``needs_tick`` means the student's explicit tick lets it through (the
    company limit, or one of the duplicate questions). Nothing is decided here that the run does not decide again
    inside its claim; this only says what the student would be told now. D1 B keeps the last one, Submit, unused:
    the student always presses Submit, so no button for it exists yet.
    """
    if result["status"] in ("unavailable", "failed"):
        closed = {"allowed": False, "needs_tick": False, "reason": result["message"]}
        return {"rehearse": dict(closed), "handoff": {**closed, "ticks": []}, "submit": dict(closed)}
    company_words = employer_key(result["company"])
    token = result["board_token"]
    tick = bool(result["asks"])
    ask_reason = "; ".join(item["message"] for item in result["asks"])
    rehearsal = apply_runs.rehearsal_block(conn, user_id, moment)
    rows: dict[str, dict[str, Any]] = {"rehearse": {"allowed": not rehearsal, "needs_tick": False, "reason": rehearsal or ""}}
    for name, mode in (("handoff", "handoff"), ("submit", "one_click")):
        block = apply_runs.limit_check(conn, user_id, company_words, result["ats"], token, mode, moment)
        blocked = block is not None and block.kind == "failed"
        ticked = tick or (block is not None and block.kind == "ask")
        rows[name] = {
            "allowed": not blocked, "needs_tick": ticked and not blocked,
            "reason": "; ".join(part for part in ((block.message if block else ""), ask_reason if not blocked else "") if part),
        }
        if name == "handoff":
            # The ticks Finish in browser asks for, each one a code the start request carries (D4): the ask's own sentence,
            # and for the company limit the date of the application it is about, so the tick says what it agrees to.
            ticks = [{"code": item["code"], "label": item["message"]} for item in result["asks"]]
            if block is not None and block.kind == "ask" and block.code == apply_runs.ASK_COMPANY_LIMIT:
                ticks.append({"code": block.code, "label": (
                    # Worded from what the record shows: the claim counts from the hand-over, and an attempt the ATS refused, one that
                    # ended unconfirmed or one the student released still counts, so it is never stated as an application made.
                    DUPLICATE_TICK.format(company=result["company"], ats=apply_ats.name_of(result["ats"]), date=block.date)
                )})
            rows[name]["ticks"] = [] if blocked else ticks
    met, count, needed = apply_runs.gate(conn, user_id, result["ats"])
    if result["status"] != "ready":
        rows["submit"].update(allowed=False, reason=result["message"])
    elif not met:
        rows["submit"].update(allowed=False, reason=f"{count} of {needed} clean rehearsals at different companies so far")
    # What this ATS does not do (Lever supports Finish in browser only, and has no driver yet) is closed here with the reason, whatever the limits say.
    spec = apply_ats.spec_for(result["ats"])
    for name in ("rehearse", "handoff"):
        refusal = apply_ats.mode_refusal(spec, name)
        if refusal:
            rows[name].update(allowed=False, needs_tick=False, reason=refusal[1])
            if name == "handoff":
                rows[name]["ticks"] = []
    if "one_click" not in spec.claim_modes:
        rows["submit"].update(allowed=False, needs_tick=False, reason=f"{spec.display_name} supports Finish in browser only, for now")
    return rows


def check(
    conn: sqlite3.Connection, user_id: str, opportunity_id: str, *, client: SchemaClient, cache: SchemaCache | None = None,
    resume_root: Path | None = None, now: datetime | None = None, page_client: PageClient | None = None,
) -> dict[str, Any]:
    """The read-only preflight for one role (6.0 steps 2 to 7). Writes nothing and returns no value.

    ``status`` is unavailable (not a Greenhouse posting), failed (nothing to do: already applied, or Greenhouse
    has no such posting), needs_you (questions the app cannot answer yet, listed with an action each) or ready.
    """
    moment = _now(now)
    result, plan, sources, _schema = _prepare(
        conn, user_id, opportunity_id, client=client, cache=cache, resume_root=resume_root, moment=moment, page_client=page_client,
    )
    if plan is None or sources is None:
        if result["ats"]:
            result["eligibility"] = _eligibility(conn, user_id, result, moment)
        return result
    problems, counts, fields = _view(plan, sources.facts, sources.cover_letter, result["ats"])
    required = [item for item in problems if item["required"]]
    # A question with no control in the app (a sensitive one) is the student's to answer on the form: it is counted apart,
    # so "need an answer first" only counts what the student can do something about here.
    yours = {item["key"] for item in required if item["action"]["type"] == "manual"}
    open_here = {item["key"] for item in required} - yours
    counts.update(needs_answer=len(open_here), left_for_you=len(yours))
    # Optional sensitive questions (EEO, mostly) the student allowed the app to answer and has not yet: left blank, and
    # offered here with the same form, since an optional question is never listed as a problem.
    optional = []
    for entry in plan.fields:
        state = _sensitive_state(entry, sources, result["company"]) if not entry.required else ""
        form = _sensitive_form(entry, f"sensitive_{state}") if state else None
        if form:
            optional.append({"key": entry.key, "question": entry.question, "message": entry.note, "action": form})
    result.update(problems=problems, counts=counts, fields=fields, optional_sensitive=optional)
    if open_here:
        count = len(open_here)
        message = f"{count} question{'s' if count != 1 else ''} need{'' if count != 1 else 's'} an answer first"
        if yours:
            message += LEFT_FOR_YOU.format(count=len(yours), is_are="is" if len(yours) == 1 else "are", ats=apply_ats.name_of(result["ats"]))
        result.update(status="needs_you", message=message)
    elif yours:
        count = len(yours)
        result.update(status="needs_you", message=YOURS_TO_ANSWER.format(count=count, s_are="s are" if count != 1 else " is", ats=apply_ats.name_of(result["ats"])))
    elif result["posting"]["differs"]:
        result.update(status="needs_you", message=f"Check the posting first. {result['posting']['difference']}")
    else:
        filled, blank = counts["filled"], counts["left_blank"]
        tail = f"; {blank} optional field{'s' if blank != 1 else ''} left blank" if blank else ""
        result.update(status="ready", message=f"Ready: {filled} field{'s' if filled != 1 else ''} from your profile and saved answers{tail}")
    result["eligibility"] = _eligibility(conn, user_id, result, moment)
    return result


@dataclass
class RunInputs:
    """What a rehearsal or a lookup starts from: the answer so far, the parsed listing, and the draft plan from the listing alone."""

    result: dict[str, Any]                           # the check's early keys: title, company, status, message, board_token, job_id, canonical_url, asks, posting
    schema: list[apply_policy.SchemaField] | None
    plan: apply_policy.Plan | None


def run_inputs(
    conn: sqlite3.Connection, user_id: str, opportunity_id: str, *, client: SchemaClient, mode: str = "rehearse",
    resume_root: Path | None = None, apply_root: Path | None = None, now: datetime | None = None, page_client: PageClient | None = None,
) -> RunInputs:
    """6.0 steps 2 to 6 for a run: always a fresh listing (no cache), the plan in ``mode``, the install's MAC key. Writes nothing.

    ``result["status"]`` is unavailable or failed when the role cannot be run (then ``schema`` and ``plan`` are None), else
    ready or needs_you. A role with questions left open may still be rehearsed: the rehearsal says what it could not fill.
    """
    moment = _now(now)
    key = apply_policy.mac_key(apply_root) if apply_root is not None else None
    result, plan, _sources, schema = _prepare(
        conn, user_id, opportunity_id, client=client, cache=None, resume_root=resume_root, moment=moment, mode=mode, key=key, page_client=page_client,
    )
    if plan is None or schema is None:
        return RunInputs(result, None, None)
    result.update(status="ready" if plan.ready else "needs_you", message="")
    return RunInputs(result, schema, plan)


def _existing_row(conn: sqlite3.Connection, user_id: str, text: str, company: str) -> Any:
    key = question_key(text)
    for row in conn.execute("SELECT id, question, company, tags_json FROM answer_library WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall():
        if question_key(row["question"]) == key and apply_policy.company_matches(str(row["company"] or ""), company):
            return row
    return None


def _stored_answer(entry: apply_policy.PlanField, answer: Any) -> str:
    """The text a saved answer holds, checked against the field's own kind: options by label only."""
    if entry.control in ("text", "textarea"):
        text = " ".join(str(answer).split()) if entry.control == "text" else str(answer).strip()
        if not text:
            raise AnswerRefused("Type an answer first")
        if len(text) > MAX_ANSWER_CHARS:
            raise AnswerRefused("That answer is too long")
        return text
    if entry.control == "checkbox":
        word = str(answer).strip().casefold()
        if word in apply_policy.YES_WORDS:
            return "Yes"
        if word in apply_policy.NO_WORDS:
            return "No"
        raise AnswerRefused("Choose yes or no")
    if entry.control in ("select", "multiselect"):
        parts = answer if isinstance(answer, list) else [answer]
        text = "\n".join(str(part) for part in parts)
        chosen, why = apply_policy.match_options(text, entry.options, several=entry.control == "multiselect")
        if not chosen:
            raise AnswerRefused(f"{why}. Choose from the list")
        return "\n".join(chosen)
    raise AnswerRefused("The app doesn't fill this kind of field")


def answer_missing(
    conn: sqlite3.Connection, user_id: str, opportunity_id: str, *, key: str, answer: Any, reusable: bool = False,
    client: SchemaClient, cache: SchemaCache | None = None, resume_root: Path | None = None, now: datetime | None = None,
    posting_confirmed: bool = False, page_client: PageClient | None = None,
) -> dict[str, Any]:
    """Save the student's answer to one question the check listed as missing. The one write of the missing-answers view.

    The answer goes to the answer library for this role's company, filed under the question's own words (or, for a
    follow-up, under its parent's as well), with no field id in it, and it is used at this company only: there is no
    "use for any company" here, so ``reusable=True`` is refused (spec 7.1 "As built"). A sensitive question is never saved
    here: it has no ordinary answer. When the form Greenhouse returned does not look like the saved role, the answer is
    refused until the student says it is the right posting (``posting_confirmed``), because it is filed under the
    role's company. Returns the fresh check.
    """
    moment = _now(now)
    result, plan, _sources, _schema = _prepare(
        conn, user_id, opportunity_id, client=client, cache=cache, resume_root=resume_root, moment=moment, page_client=page_client,
    )
    if plan is None:
        raise AnswerRefused(result["message"])
    if result["posting"]["differs"] and not posting_confirmed:
        raise AnswerRefused(f"{result['posting']['difference']}. Confirm it is the right posting before saving an answer for {result['company']}")
    entry = plan.get(key)
    if entry is None:
        raise AnswerRefused("The form no longer asks that question")
    if entry.sensitive or entry.net_never:
        # A sensitive question has no ordinary answer, and neither does one the broad net leaves to the student (criminal history,
        # personal details, pay, security, or a box that agrees to something): no answer to it is saved, for any company.
        raise AnswerRefused("The app doesn't save answers to this kind of question")
    if entry.problem_kind not in ("missing_answer", "answer_mismatch") and entry.source.kind != "answer":
        raise AnswerRefused("The app can't fill this question from a saved answer")
    text = _stored_answer(entry, answer)
    if reusable:
        raise AnswerRefused("Apply for me saves an answer for this company only, so it is not saved for any company")
    company = result["company"]
    existing = _existing_row(conn, user_id, entry.answer_key, company)
    # A row this company already has keeps its tags: the student's own tagging (the extension may reuse a reusable row) is theirs.
    tags = [str(tag) for tag in (json.loads(existing["tags_json"] or "[]") if existing else [])]
    saved = preparation.save_answer(
        conn, entry.answer_key, text, company, tags, answer_id=existing["id"] if existing else None, user_id=user_id,
    )
    return {"answer_id": saved["id"], "check": check(conn, user_id, opportunity_id, client=client, cache=cache, resume_root=resume_root, now=moment, page_client=page_client)}


def answer_sensitive(
    conn: sqlite3.Connection, user_id: str, opportunity_id: str, *, key: str, answer: Any, consent: bool, any_company: bool = False,
    client: SchemaClient, cache: SchemaCache | None = None, resume_root: Path | None = None, now: datetime | None = None,
    posting_confirmed: bool = False, page_client: PageClient | None = None,
) -> dict[str, Any]:
    """Store the student's answer to one sensitive question the check listed, with the consent ticked (Needs you, 10.3).

    Unlike ``answer_missing`` this saves to the sensitive-answers store, and only for a question the plan says the
    student switched on and has no stored answer for (or a stored one that does not fit this form). The category, the
    exact wording, the options and the links come from the form the app read, not from the browser: the browser sends
    only its answer and the tick. An option answer must be one of the form's own labels, an EEO answer must be a
    decline, and a statement is stored word for word. It is saved for this company unless ``any_company`` is ticked,
    which is refused for a statement that points to a document and for a question that depends on its company.
    Returns the fresh check.
    """
    moment = _now(now)
    result, plan, sources, _schema = _prepare(
        conn, user_id, opportunity_id, client=client, cache=cache, resume_root=resume_root, moment=moment, page_client=page_client,
    )
    if plan is None:
        raise AnswerRefused(result["message"])
    if result["posting"]["differs"] and not posting_confirmed:
        raise AnswerRefused(f"{result['posting']['difference']}. Confirm it is the right posting before saving an answer for {result['company']}")
    entry = plan.get(key)
    if entry is None:
        raise AnswerRefused("The form no longer asks that question")
    state = _sensitive_state(entry, sources, result["company"]) if sources is not None else ""
    if not state:
        raise AnswerRefused("The app can't store an answer to this question")
    form = _sensitive_form(entry, f"sensitive_{state}")
    if form is None:
        raise AnswerRefused("The app stores only a decline answer for this kind of question, and this form offers none")
    kind = {"select": "option", "multiselect": "options", "text": "text", "textarea": "text", "checkbox": "checkbox"}[entry.control]
    if entry.control == "checkbox":
        if str(answer).strip().casefold() not in ("checked", "true", "yes"):
            raise AnswerRefused("Tick the box to agree to this statement")
        text: Any = "checked"
    elif entry.control in ("select", "multiselect"):
        parts = answer if isinstance(answer, list) else [answer]
        chosen, why = apply_policy.match_options("\n".join(str(part) for part in parts), form["options"], several=entry.control == "multiselect")
        if not chosen:
            raise AnswerRefused(f"{why}. Choose from the list")
        text = chosen if entry.control == "multiselect" else chosen[0]
    else:
        text = " ".join(str(answer).split())
    everyone = bool(any_company) and not form["company_only"]
    if any_company and form["company_only"]:
        raise AnswerRefused("This answer is saved for this company only")
    try:
        saved = apply_sensitive.add_entry(
            conn, user_id, category=entry.sensitive, question=entry.statement, answer=text, answer_kind=kind,
            company="" if everyone else result["company"], links=entry.links, company_only=form["company_only"], consent=consent, now=moment, from_form=True,
        )
    except apply_sensitive.StoreRefused as exc:
        raise AnswerRefused(str(exc)) from exc
    return {"entry_id": saved["id"], "check": check(conn, user_id, opportunity_id, client=client, cache=cache, resume_root=resume_root, now=moment, page_client=page_client)}
