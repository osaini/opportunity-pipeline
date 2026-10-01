"""Apply for me, the policy: what may go into each field of a Greenhouse form, and where the value comes from.

This is the pure half of the agent (docs/phase5-apply-agent-spec.md, sections 4.5 and 7). No browser, no
network, and no write: it reads Greenhouse's own field listing and the student's data, and answers with a
plan. The plan says, for every field, whether the app may fill it, from which source, and if not, exactly
why in words the student can act on.

    parse_schema        Greenhouse's Job Board API listing -> the fields the form has
    classify_sensitive  which questions the app never answers on its own (7.3)
    sources_for         everything a value may come from, gathered once
    build_plan          each field's value, source and disposition (7.1, 7.2)
    plan_hash           the fingerprint the student approves
    resume_for          which résumé goes with a role (6.9)
    identify            which Greenhouse board and job a saved role is (4.4)

A value has four sources and no others: a confirmed profile fact through a short mapping list, an answer
the student saved (only when the question is word for word the same and it was saved for this company: an
answer never travels to another company here, whatever tag it carries), an exact option label the student
confirmed, and the résumé or cover letter for the role.
Sensitive answers come from one function, ``stored_sensitive_answer``, which reads the store the student
filled in on purpose (apply_sensitive.py) and nothing else. Nothing is ever guessed: no fuzzy match, no first
option, no label regex.

The value of a field is held in the plan in memory only. What is stored and hashed is a keyed MAC of it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from pipeline_core.identity import employer_key, identity_tokens, normalized_text

from . import apply_sensitive, preparation, resume_variants
from .apply_checks import ALTERNATE_TEXT_FIELDS, BOARD_HOSTS, Problem, join, question_key
from .apply_classify import (
    CATEGORY_TOPIC,
    CATEGORY_WORDS,
    INHERITING_PARENTS,
    NET_WORDS,
    NEVER_TOPICS,
    SECOND_PERSON,
    STATEMENT_CATEGORIES,
    TICKABLE,
    classify_item,
    classify_sensitive,
    context_dependent,
    field_net,
    follow_up_wording,
    most_restrictive,
    needs_label_key,
    net_topics,
    plain_text,
    statement_control,
    statement_of,
    without_enumeration,
)
from .extension_apply import ExtensionApplyError, confirmed_resume_file
from .json_values import json_as

__all__ = [
    "ALLOWED_ATS_LABEL_FIELDS", "ATS_GREENHOUSE", "Plan", "PlanField", "SchemaField", "Source", "Sources", "build_plan", "canonical_url",
    "company_matches", "control_of", "cover_letter_for", "identify", "mac_key", "match_options", "name_parts", "parse_schema", "plan_entries",
    "plan_hash", "question_key", "resume_for", "schema_url", "sources_for", "stored_sensitive_answer", "value_mac", "with_page_labels",
]

ATS_GREENHOUSE = "greenhouse"

# --- Identifying the posting (4.4) --------------------------------------------------------------

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_JOB_ID = re.compile(r"^\d+$")
_JOB_PATH = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]{0,79})/jobs/(\d+)/?$")


def canonical_url(board_token: str, job_id: str) -> str:
    return f"https://job-boards.greenhouse.io/{board_token}/jobs/{job_id}"


def schema_url(board_token: str, job_id: str) -> str:
    """Greenhouse's public, keyless listing of what an application form asks (read-only GET)."""
    return f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}?questions=true"


def _from_url(url: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or host not in BOARD_HOSTS:
        return None
    match = _JOB_PATH.match(parts.path)
    if match:
        return match.group(1), match.group(2)
    if parts.path.rstrip("/") == "/embed/job_app":
        query = parse_qs(parts.query)
        token, job = (query.get("for") or [""])[0], (query.get("token") or [""])[0]
        if _TOKEN.match(token) and _JOB_ID.match(job):
            return token, job
    return None


def identify(conn: sqlite3.Connection, opportunity_id: str) -> tuple[str, str] | None:
    """The Greenhouse (board token, job id) this saved role is, or None when it is not one the app can fill.

    A token parsed from the role's own URL, then from a source URL, wins. Otherwise the source key
    ``greenhouse:<token>`` and the source's external id are used, and the id must be all digits. A company
    site that carries only ``gh_jid`` is not supported.
    """
    row = conn.execute("SELECT url FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    if row is None:
        return None
    found = _from_url(row[0])
    if found:
        return found
    sources = conn.execute(
        "SELECT source_url, source_key, external_id FROM opportunity_sources WHERE opportunity_id=? ORDER BY last_seen_at DESC, source_key",
        (opportunity_id,),
    ).fetchall()
    for source in sources:
        found = _from_url(source[0])
        if found:
            return found
    # The pattern is a parameter, not part of the SQL: a literal % breaks on PostgreSQL, where ? becomes %s.
    for source in conn.execute(
        "SELECT source_key, external_id FROM opportunity_sources WHERE opportunity_id=? AND source_key LIKE ? ORDER BY last_seen_at DESC, source_key",
        (opportunity_id, "greenhouse:%"),
    ).fetchall():
        token, job = str(source[0])[len("greenhouse:"):], str(source[1] or "")
        if _TOKEN.match(token) and _JOB_ID.match(job):
            return token, job
    return None


# --- What the form asks: Greenhouse's own listing (4.5) ------------------------------------------

STANDARD_FIELDS = frozenset({
    "first_name", "last_name", "preferred_name", "email", "phone", "resume", "resume_text", "cover_letter", "cover_letter_text",
})
_EDUCATION = re.compile(r"^(?:educations?(?:\b|_)|school|degree|discipline|(?:start|end)_date|(?:start|end)_(?:month|year))")
# The typeahead lists whose options only the page knows. Each is answered from a label the student confirmed (5.5).
ALLOWED_ATS_LABEL_FIELDS = (
    "location", "school", "degree", "discipline", "phone_country",
    "education_start_month", "education_start_year", "education_end_month", "education_end_year",
)
_SELECTS = frozenset({"multi_value_single_select", "multi_value_multi_select"})
MAX_DESCRIPTION_CHARS = 2000


@dataclass(frozen=True)
class SchemaField:
    """One field of the application form, as Greenhouse's listing describes it.

    ``section`` is standard, custom, location, education, compliance, demographic or data_compliance.
    ``parent`` is the label of the question above it in the listing, which gives a follow-up its meaning.
    ``derived_name`` marks a name the live listing does not carry (a demographic question has only an id,
    a data_compliance entry only its consent flags): it is derived, and the page's own controls decide.
    ``label_from_page`` marks a label the listing does not carry: a consent statement is on the form only.
    ``description_cut`` marks a description longer than the app keeps: text a statement is built from must be whole.
    """

    name: str
    label: str
    required: bool
    type: str
    options: tuple[str, ...] = ()
    section: str = "custom"
    parent: str = ""
    description: str = ""
    compliance_type: str = ""
    derived_name: bool = False
    label_from_page: bool = False
    description_cut: bool = False


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _option_labels(values: Any) -> tuple[str, ...]:
    labels = []
    for item in values if isinstance(values, list) else []:
        label = _text(item.get("label")) if isinstance(item, Mapping) else _text(item)
        if label:
            labels.append(label)
    return tuple(labels)


def _section_of(name: str, block_section: str) -> str:
    if block_section != "questions":
        return block_section
    if re.match(r"^question_", name):
        return "custom"
    if _EDUCATION.match(name):
        return "education"
    return "standard"


def parse_schema(listing: Mapping[str, Any]) -> list[SchemaField]:
    """The fields of a Job Board API listing (``?questions=true``), in the order the form shows them.

    Covers ``questions``, ``location_questions``, ``compliance`` (EEOC, keyed by its own field names),
    ``demographic_questions`` (an object with no field names: each question has an id) and
    ``data_compliance`` (consent flags with no name or label). The two shapes the live API leaves nameless
    are given derived names (``question_{id}``, ``{type}_consent_given``), flagged as derived.
    """
    found: list[SchemaField] = []
    previous = ""

    def question_fields(block: Mapping[str, Any], section: str, compliance_type: str = "") -> None:
        nonlocal previous
        label = _text(block.get("label"))
        required = bool(block.get("required"))
        raw_description = str(block.get("description") or "")
        description = raw_description[:MAX_DESCRIPTION_CHARS]
        shown = False
        for entry in block.get("fields") if isinstance(block.get("fields"), list) else []:
            if not isinstance(entry, Mapping) or not entry.get("name"):
                continue
            name = str(entry["name"])
            kind = str(entry.get("type") or "")
            # The paste-instead alternative of an upload is listed inside the same required block, but the form
            # shows it only after "Enter manually", which the agent never presses: it is never required.
            found.append(SchemaField(
                name=name, label=label or name, required=required and name not in ALTERNATE_TEXT_FIELDS, type=kind,
                options=_option_labels(entry.get("values")),
                # An EEOC question is named by its field, never by the question above it: it continues no other question.
                section=_section_of(name, section), parent="" if section == "compliance" else previous, description=description,
                compliance_type=compliance_type, description_cut=len(raw_description) > MAX_DESCRIPTION_CHARS,
            ))
            shown = shown or kind != "input_hidden"
        if label and shown:
            previous = label

    for block in listing.get("questions") or []:
        if isinstance(block, Mapping):
            question_fields(block, "questions")
    for block in listing.get("location_questions") or []:
        if isinstance(block, Mapping):
            question_fields(block, "location")
    for block in listing.get("compliance") or []:
        if not isinstance(block, Mapping):
            continue
        for question in block.get("questions") or []:
            if isinstance(question, Mapping):
                question_fields(question, "compliance", str(block.get("type") or ""))
    demographic = listing.get("demographic_questions")
    for question in (demographic.get("questions") if isinstance(demographic, Mapping) else None) or []:
        if not isinstance(question, Mapping) or question.get("id") in (None, ""):
            continue
        found.append(SchemaField(
            name=f"question_{question['id']}", label=_text(question.get("label")) or f"question_{question['id']}",
            required=bool(question.get("required")), type=str(question.get("type") or ""),
            options=_option_labels(question.get("answer_options")), section="demographic",
            description=str(question.get("description") or "")[:MAX_DESCRIPTION_CHARS], derived_name=True,
        ))
    for entry in listing.get("data_compliance") or []:
        if not isinstance(entry, Mapping):
            continue
        kind = re.sub(r"[^a-z0-9]+", "_", str(entry.get("type") or "").lower()).strip("_")
        # Consent is required when Greenhouse says so. Its statement is only on the form, and so is its control's name.
        if kind and any(entry.get(flag) for flag in ("requires_consent", "requires_processing_consent", "requires_retention_consent")):
            found.append(SchemaField(
                name=f"{kind}_consent_given", label=f"{kind.upper()} data consent {apply_sensitive.PLACEHOLDER_NOTE}", required=True,
                type="multi_value_multi_select", options=(), section="data_compliance", compliance_type=kind,
                derived_name=True, label_from_page=True,
            ))
    return found


def with_page_labels(schema: Sequence[SchemaField], scan: Iterable[Any]) -> list[SchemaField]:
    """The schema with each label the listing lacks (a consent statement) read from the page's own field.

    The statement is the field's question on the form, and the exact-match rule for a consent box compares
    it with the statement the student stored. A field the page does not have keeps its placeholder.
    """
    scans = list(scan)
    result = []
    for item in schema:
        if item.label_from_page:
            for control in scans:
                get = control.get if isinstance(control, Mapping) else lambda name, default=None: getattr(control, name, default)
                if str(get("name") or "") == item.name and get("question"):
                    item = replace(item, label=_text(get("question")), label_from_page=False)
                    break
        result.append(item)
    return result


def control_of(item: SchemaField) -> str:
    """text, textarea, select, multiselect, checkbox, file, hidden or unknown (one option in a multi select is a checkbox)."""
    if item.type == "input_hidden":
        return "hidden"
    if item.type == "input_file":
        return "file"
    if item.type == "input_text":
        return "text"
    if item.type == "textarea":
        return "textarea"
    if item.type == "multi_value_single_select":
        return "select"
    if item.type == "multi_value_multi_select":
        return "checkbox" if len(item.options) <= 1 else "multiselect"
    return "unknown"


# --- The one stored answer a sensitive question may take (5.4) ----------------------------------------


def stored_sensitive_answer(
    conn: sqlite3.Connection, user_id: str, *, category: str, question_key: str, company_key: str, mode: str, company_only: bool = False,
) -> dict[str, Any] | None:
    """The stored answer the student deliberately added for this sensitive question, or None.

    The one place the plan asks (apply_sensitive.lookup, spec 5.4). The entry it returns holds ``id``,
    ``answer_kind`` (option, options, text or checkbox), ``answer``, the statement's ``links`` and ``added``,
    and only when the exact key, the category, the company and the consent scope for ``mode`` all match. ``company_only``
    (the form's question depends on its company, or its statement on a document) skips a row saved for any company.
    It reads only: the check and the plan write nothing.
    """
    return apply_sensitive.lookup(
        conn, user_id, category=category, question_key=question_key, company_key=company_key, mode=mode, company_only=company_only,
    )


# --- Where a value may come from (7.1) --------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """kind is profile, ats_label, answer, sensitive, resume, cover_letter or none. ``ref`` names the row, never a value.

    ``links`` are the addresses of the documents a ticked statement points to, shown next to the tick (D9 B).
    """

    kind: str = "none"
    ref: str = ""
    company: str = ""
    reusable: bool = False
    label: str = ""
    links: tuple[str, ...] = ()


NO_SOURCE = Source()


@dataclass
class Sources:
    """Everything a value may come from for one student and one role, read once."""

    facts: dict[str, Any] = field(default_factory=dict)
    answers: list[dict[str, Any]] = field(default_factory=list)
    ats_labels: dict[str, str] = field(default_factory=dict)
    sensitive_allowed: frozenset[str] = frozenset()
    sensitive_lookup: Callable[..., dict[str, Any] | None] = lambda **_: None
    resume: dict[str, Any] = field(default_factory=dict)
    cover_letter: dict[str, Any] = field(default_factory=dict)
    mac_key: bytes = b""


def mac_key(apply_root: Path | None) -> bytes:
    """The per-install key that keys every value MAC (spec 5.3): 32 random bytes in ``hash-key``.

    Created on first use, never exported, logged or sent. With no folder, a fresh key: the MACs then only
    agree within one call, which is all the read-only check needs and writes nothing.
    """
    if apply_root is None:
        return secrets.token_bytes(32)
    path = Path(apply_root) / "hash-key"
    try:
        key = path.read_bytes()
    except OSError:
        key = b""
    if len(key) == 32:
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _canonical_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "\n".join(sorted(str(item) for item in value))
    return str(value if value is not None else "")


def value_mac(key: bytes, value: Any) -> str:
    """HMAC-SHA256 of a value. A plain hash of "Yes" or a phone number could be reversed with a short dictionary."""
    if value is None or value == "":
        return ""
    return hmac.new(key, _canonical_value(value).encode("utf-8"), hashlib.sha256).hexdigest()


def _norm(text: Any) -> str:
    return " ".join(str(text if text is not None else "").split()).casefold()


# --- The résumé and the cover letter (6.9) -----------------------------------------------------------

def resume_for(conn: sqlite3.Connection, user_id: str, opportunity_id: str, storage_root: Path | None = None) -> dict[str, Any]:
    """Which résumé goes with this role. ``problem_kind`` is set, with a sentence in ``problem``, when none can.

    1. The pick in force: the student's own, or an automatic one with status picked. The file is that
       résumé's confirmed version; with none confirmed, a problem.
    2. An automatic pick that is unsure opens the chooser (a problem).
    3. No pick at all, the usual case: the most recently confirmed résumé, shown as "Your confirmed résumé".
    With ``storage_root`` the file is also found and its hash checked against the one stored at upload.
    """
    pick = resume_variants.stored_pick(conn, user_id, opportunity_id)
    result: dict[str, Any] = {"kind": "confirmed", "version_id": "", "file_id": "", "label": "Your confirmed résumé",
                              "original_name": "", "sha256": "", "problem_kind": "", "problem": ""}
    if pick is not None and pick.get("status") == "unsure":
        return {**result, "problem_kind": "resume_unsure",
                "problem": "The app couldn't tell which of your résumés fits this role. Choose one for it"}
    if pick is not None:
        confirmed = conn.execute(
            "SELECT rv.id AS version_id FROM resume_versions rv WHERE rv.resume_file_id=? AND rv.user_id=? AND rv.status='confirmed' "
            "ORDER BY rv.confirmed_at DESC, rv.created_at DESC LIMIT 1", (pick["resume_file_id"], user_id),
        ).fetchone()
        label = pick.get("label") or "the résumé picked for this role"
        if confirmed is None:
            return {**result, "kind": "pick", "file_id": pick["resume_file_id"], "label": f'Résumé "{label}"',
                    "problem_kind": "resume_unconfirmed", "problem": "The résumé picked for this role has no confirmed version"}
        result.update(kind="pick", version_id=str(confirmed["version_id"]), file_id=str(pick["resume_file_id"]), label=f'Résumé variant "{label}"')
    else:
        latest = conn.execute(
            "SELECT rv.id AS version_id, rf.id AS file_id FROM resume_files rf JOIN resume_versions rv ON rv.resume_file_id=rf.id "
            "WHERE rf.user_id=? AND rv.status='confirmed' ORDER BY rv.confirmed_at DESC, rv.created_at DESC LIMIT 1", (user_id,),
        ).fetchone()
        if latest is None:
            return {**result, "problem_kind": "resume_missing", "problem": "Confirm a résumé on your Profile page first"}
        result.update(version_id=str(latest["version_id"]), file_id=str(latest["file_id"]))
    if storage_root is not None:
        try:
            _path, name, _media, sha = confirmed_resume_file(conn, result["version_id"], storage_root, user_id=user_id, verify=True)
        except ExtensionApplyError as exc:
            return {**result, "problem_kind": "resume_file", "problem": f"{exc}. Upload the résumé again"}
        result.update(original_name=name, sha256=sha)
    else:
        row = conn.execute(
            "SELECT rf.original_name, rf.sha256 FROM resume_versions rv JOIN resume_files rf ON rf.id=rv.resume_file_id WHERE rv.id=?",
            (result["version_id"],),
        ).fetchone()
        if row is not None:
            result.update(original_name=str(row["original_name"]), sha256=str(row["sha256"]))
    return result


def cover_letter_for(conn: sqlite3.Connection, user_id: str, opportunity_id: str) -> dict[str, Any]:
    """The cover letter that may be attached (D11): the latest version for this role, and only when it is approved."""
    latest = conn.execute(
        "SELECT id, version, status, content FROM generated_documents WHERE user_id=? AND opportunity_id=? AND document_type='cover_letter' "
        "ORDER BY version DESC LIMIT 1", (user_id, opportunity_id),
    ).fetchone()
    if latest is None:
        return {"problem_kind": "cover_letter_missing", "problem": "No cover letter is approved for this role. Draft one"}
    if latest["status"] != "approved":
        return {"problem_kind": "cover_letter_draft",
                "problem": "Your cover letter for this role has a newer draft. Approve it or discard it"}
    return {"document_id": str(latest["id"]), "version": int(latest["version"]),
            "content_sha256": hashlib.sha256(str(latest["content"]).encode("utf-8")).hexdigest(), "problem_kind": "", "problem": ""}


def sources_for(
    conn: sqlite3.Connection, user_id: str, opportunity_id: str, *, company: str = "", storage_root: Path | None = None,
    key: bytes | None = None,
) -> Sources:
    """Gather everything a value may come from, reading only. Nothing here changes a row."""
    facts = preparation.confirmed_facts(conn, user_id)
    answers = [
        {**dict(row), "tags": [str(tag) for tag in json_as(row["tags_json"], [])]}
        for row in conn.execute(
            "SELECT id, question, answer, company, tags_json, updated_at FROM answer_library WHERE user_id=? ORDER BY updated_at DESC, id", (user_id,),
        ).fetchall()
    ]
    labels = {
        str(row["field"]): str(row["label"])
        for row in conn.execute("SELECT field, label FROM apply_ats_labels WHERE user_id=? AND ats=?", (user_id, ATS_GREENHOUSE)).fetchall()
    }
    allowed = apply_sensitive.allowed_categories(conn, user_id)

    def lookup(**kwargs: Any) -> dict[str, Any] | None:
        return stored_sensitive_answer(conn, user_id, **kwargs)

    return Sources(
        facts=facts, answers=answers, ats_labels=labels, sensitive_allowed=allowed, sensitive_lookup=lookup,
        resume=resume_for(conn, user_id, opportunity_id, storage_root), cover_letter=cover_letter_for(conn, user_id, opportunity_id),
        mac_key=key if key is not None else mac_key(None),
    )


# --- The plan ---------------------------------------------------------------------------------------

@dataclass
class PlanField:
    """What the app would do with one field. ``value`` is held in memory only; the record keeps ``value_mac``."""

    key: str
    question: str
    control: str
    required: bool
    options: tuple[str, ...] = ()
    section: str = "custom"
    sensitive: str | None = None
    source: Source = NO_SOURCE
    value: Any = field(default=None, repr=False, compare=False)
    value_mac: str = ""
    file_sha256: str = ""
    file_name: str = ""
    disposition: str = "blank"
    problem: str = ""
    problem_kind: str = ""
    # The text a saved answer is filed under: the question, or "{parent} / {question}" for a follow-up.
    answer_key: str = ""
    context_dependent: bool = False
    # Why an optional field was left blank, or what the page will not allow (never a value).
    note: str = ""
    # A rehearsal cannot put this in the page (a file on a board that uploads as you attach it).
    defer: bool = False
    # Which typeahead list this field is (5.5), when its option must be one the student confirmed.
    label_field: str = ""
    # A sensitive field: the exact text a stored answer is matched on (a checkbox's statement, else the question), and
    # the addresses of the documents the form's statement links to. Never a value.
    statement: str = ""
    links: tuple[str, ...] = ()
    # A sensitive field whose stored answer may only be one saved for this company: its question depends on the company,
    # or its statement points to a document or leans on text elsewhere on the form.
    company_only: bool = False
    # A sensitive field whose statement is built from text the app did not keep whole, so it cannot be matched word for word.
    text_cut: bool = False
    # What the broad net found (NET_TOPICS): in the question's own words, in the question above it when this one follows it, or
    # from a precisely sensitive question above it. ``net_company`` keeps an ordinary question's answer for one company;
    # ``net_never`` names the never-storable topics (or an agreement box), which leave the question to the student.
    net: tuple[str, ...] = ()
    net_company: bool = False
    net_never: tuple[str, ...] = ()


@dataclass
class Plan:
    fields: list[PlanField]
    problems: list[Problem]
    plan_hash: str = ""

    @property
    def ready(self) -> bool:
        return not any(problem.required for problem in self.problems)

    @property
    def status(self) -> str:
        return "ready" if self.ready else "needs_you"

    def get(self, key: str) -> PlanField | None:
        return next((item for item in self.fields if item.key == key), None)


_PROFILE_KEYS = {
    "linkedin": "linkedin", "linkedin profile": "linkedin", "linkedin url": "linkedin", "linkedin profile url": "linkedin",
    "github": "github", "github url": "github", "github profile": "github",
    "website": "portfolio", "portfolio": "portfolio", "personal website": "portfolio", "portfolio url": "portfolio", "website url": "portfolio",
}
_NAME_PROBLEM = "Add your first and last name for applications in your profile"


def _contact(facts: Mapping[str, Any], name: str) -> str:
    contact = facts.get("contact")
    value = contact.get(name) if isinstance(contact, Mapping) else None
    return " ".join(value.split()) if isinstance(value, str) and value.strip() else ""


def name_parts(facts: Mapping[str, Any]) -> tuple[str, str, str]:
    """(first, last, preferred) for applications: the confirmed name_parts, else a confirmed name of exactly two words.

    Splitting a longer name is a guess, so the app does not: "Ana María de la Cruz" gives ("", "", "").
    """
    parts = facts.get("name_parts")
    first = last = preferred = ""
    if isinstance(parts, Mapping):
        first, last, preferred = _text(parts.get("first")), _text(parts.get("last")), _text(parts.get("preferred"))
        if first and last:
            return first, last, preferred
        if first or last:
            # Half a name written for applications is unclear, not a reason to read the plain name instead.
            return "", "", preferred
    name = facts.get("name")
    words = str(name).split() if isinstance(name, str) else []
    return (words[0], words[1], preferred) if len(words) == 2 else ("", "", preferred)


def label_field_of(item: SchemaField) -> str:
    """Which typeahead list a field is (5.5), or ''."""
    name = item.name.lower()
    if item.section == "location" or name in ("location", "location_city", "candidate_location"):
        return "location"
    if name == "phone_country":
        return "phone_country"
    if item.section == "education":
        for kind in ("school", "degree", "discipline"):
            if kind in name:
                return kind
        match = re.search(r"(start|end)[_ ]?(?:date)?[_ ]?(month|year)", name)
        if match:
            return f"education_{match.group(1)}_{match.group(2)}"
    return ""


def match_options(answer: str, options: Sequence[str], *, several: bool) -> tuple[list[str] | None, str]:
    """The option label(s) an answer names, by label only. (labels, "") or (None, why not)."""
    parts = [part.strip() for part in re.split(r"[\n;]", answer) if part.strip()] if several else [answer.strip()]
    chosen: list[str] = []
    for part in parts:
        found = [option for option in options if _norm(option) == _norm(part)]
        if len(found) != 1:
            # No quote of the answer: this sentence is kept in a run's plan, and a stored record holds no value.
            return None, "Your saved answer " + ("matches more than one option" if found else "is not one of the form's options")
        if found[0] not in chosen:
            chosen.append(found[0])
    return (chosen or None), "" if chosen else "The saved answer is empty"


YES_WORDS = frozenset({"yes", "true", "checked", "on", "1"})
NO_WORDS = frozenset({"no", "false", "unchecked", "off", "0"})


def _choice_value(control: str, answer: str, options: Sequence[str]) -> tuple[Any, str]:
    """The value a saved answer gives a field of this kind, or (None, why not) when it does not fit exactly."""
    if control in ("text", "textarea"):
        return (answer.strip(), "") if answer.strip() else (None, "The saved answer is empty")
    if control == "select":
        chosen, why = match_options(answer, options, several=False)
        return (chosen[0], "") if chosen else (None, why)
    if control == "multiselect":
        chosen, why = match_options(answer, options, several=True)
        return (chosen, "") if chosen else (None, why)
    if control == "checkbox":
        word = _norm(answer)
        if word in YES_WORDS or (options and word == _norm(options[0])):
            return True, ""
        if word in NO_WORDS:
            return False, ""
        return None, "A checkbox answer must be yes or no"
    return None, "The app doesn't fill this kind of field"


@dataclass
class _Context:
    sources: Sources
    company: str
    mode: str
    repeated: dict[str, list[str]]
    uploads_on_attach: bool


def _answer_key(item: SchemaField, repeated: Mapping[str, list[str]]) -> tuple[str, bool]:
    """(the text a saved answer is filed under, whether this question depends on its context).

    A follow-up, an opener, a very short question, or a question the form asks twice is filed under its
    parent: "{parent} / {question}" (spec 7.1), so the same words under two questions are two keys.
    """
    if item.section == "compliance":
        # An EEOC field is found by its own name and label (7.3 step 2): one decline serves every company and every form.
        return item.label, False
    key = question_key(item.label)
    dependent = context_dependent(key)
    if needs_label_key(key) or len(repeated.get(key, [])) > 1:
        return (f"{item.parent} / {item.label}" if item.parent else item.label), True
    return item.label, dependent


def company_matches(row_company: str, company: str) -> bool:
    mine, theirs = identity_tokens(company), identity_tokens(row_company)
    return bool(mine) and mine == theirs


# Words a role title shares with almost every other title, so they say nothing about whether two titles are one role.
_GENERIC_TITLE_WORDS = frozenset({"intern", "interns", "internship", "co", "op", "coop", "summer", "fall", "spring", "winter", "student", "and", "the", "of", "for", "a", "an", "in", "at"})


def posting_difference(company: str, title: str, listing: Mapping[str, Any]) -> str:
    """Why the listing Greenhouse returned does not look like the role the student saved, or "" when it does or cannot be told.

    The board and job id come from the role's link, and a wrong link (an aggregator's, a merged duplicate, a parent
    company's board) would put another employer's questions on this role and file the student's answers under the wrong
    company. So the employer must be the same words, and the titles must share a word besides the generic ones. A listing
    that names neither is not judged.
    """
    theirs_company, theirs_title = _text(listing.get("company_name")), _text(listing.get("title"))
    if theirs_company and company and not company_matches(theirs_company, company):
        return f"Greenhouse's form is for {theirs_title or 'a posting'} at {theirs_company}, not {company}"
    mine = set(re.findall(r"[a-z0-9]+", title.lower())) - _GENERIC_TITLE_WORDS
    theirs = set(re.findall(r"[a-z0-9]+", theirs_title.lower())) - _GENERIC_TITLE_WORDS
    mine = {word for word in mine if not word.isdigit()}
    theirs = {word for word in theirs if not word.isdigit()}
    if mine and theirs and not mine & theirs:
        return f"Greenhouse's form is for {theirs_title}, not {title}"
    return ""


def _saved_answer(item: SchemaField, text: str, ctx: _Context) -> tuple[dict[str, Any] | None, str, str]:
    """(the usable saved answer, problem_kind, problem): only a row saved for this company, and only one answer for it (7.1).

    Apply for me never carries an answer from one company to another (spec 7.1 "As built"): no classifier can tell every
    personal, legal or agreement question from an ordinary one, so the safe default is that nothing travels. A row tagged
    reusable (saved in the library or by the extension) is read here as the row for the company it was saved at, and at any
    other company it is one saved elsewhere. The student's own browser extension still proposes a reusable answer for
    review; the agent does not.
    """
    key = question_key(text)
    rows = [row for row in ctx.sources.answers if question_key(row["question"]) == key and str(row["answer"]).strip()]
    usable = [row for row in rows if company_matches(str(row["company"] or ""), ctx.company)]
    elsewhere = [row for row in rows if row not in usable]
    answers = {str(row["answer"]).strip() for row in usable}
    if len(answers) > 1:
        return None, "conflicting_answers", f'You have two different saved answers for "{item.label}". Keep one'
    if usable:
        return usable[0], "", ""
    if elsewhere:
        names = sorted({str(row["company"]) for row in elsewhere if row["company"]})
        where = f" (saved for {', '.join(names[:2])})" if names else ""
        return None, "missing_answer", f'No saved answer for this company{where}'
    return None, "missing_answer", "No saved answer for this company"


def _plan_file(item: SchemaField, entry: PlanField, ctx: _Context) -> PlanField:
    if item.name not in ("resume", "cover_letter"):
        # Row U: only the two documents the app holds are ever attached. A transcript, a writing sample or a
        # custom "Cover letter" question is some other upload, and the résumé is not the answer to it.
        entry.problem_kind, entry.problem = "unsupported", f'The app doesn\'t fill this kind of field ("{item.label}")'
        return entry
    if item.name == "cover_letter":
        letter = ctx.sources.cover_letter
        if not item.required:
            entry.note = "Optional cover letters are left empty"
            return entry
        if letter.get("problem_kind"):
            entry.problem_kind, entry.problem = letter["problem_kind"], letter["problem"]
            return entry
        entry.source = Source("cover_letter", f'{letter["document_id"]}@{letter["version"]}', label=f'Approved cover letter, version {letter["version"]}')
        entry.file_sha256, entry.value = letter["content_sha256"], letter["document_id"]
        return entry
    resume = ctx.sources.resume
    if resume.get("problem_kind"):
        entry.problem_kind, entry.problem = resume["problem_kind"], resume["problem"]
        return entry
    entry.source = Source("resume", resume["version_id"], label=resume["label"])
    entry.file_sha256, entry.file_name, entry.value = resume["sha256"], resume["original_name"], resume["version_id"]
    if ctx.uploads_on_attach:
        entry.defer = True
        entry.note = "This board uploads a file as soon as it is attached, so the app can't attach it without sending it"
    return entry


def _plan_sensitive(item: SchemaField, entry: PlanField, category: str, ctx: _Context, *, follows: bool = False) -> PlanField:
    words = CATEGORY_WORDS.get(category, "a personal question")
    sources = ctx.sources
    # A checkbox is matched on its own statement, a follow-up on its parent's question too: neither on the bare heading.
    checkbox = entry.control == "checkbox"
    # A Yes/No agreement question is matched like a box: on the question and its description, never on the question alone.
    boxlike = checkbox or (category in STATEMENT_CATEGORIES and statement_control(entry.control, entry.options))
    entry.statement = statement_of(item, entry.control, category, entry.answer_key) if boxlike else entry.answer_key or item.label
    key = question_key(entry.statement)
    if checkbox or category in STATEMENT_CATEGORIES:
        entry.links = apply_sensitive.links_in(item.label, *item.options, item.description)
    # Text the app cut off is text it cannot compare, and a link past the cut is a link it never saw (D9 B).
    entry.text_cut = item.description_cut and (checkbox or category in STATEMENT_CATEGORIES)
    # A question that depends on its company is never answered from another company's entry (7.1), and neither is a
    # statement that points to a document or leans on words outside its option. An EEOC field is found by its own
    # name and holds only a decline, so it is the same at every company. Every stored statement (an acknowledgment or a consent)
    # and every tick box or typed answer is kept for one company, whatever its words: no list can prove a statement names no
    # document. Only a select's exact option label, for work authorization, sponsorship or 18 or older, is kept for any company.
    entry.company_only = (
        category in STATEMENT_CATEGORIES
        or (category in TICKABLE and (entry.control != "select" or "agreement" in net_topics(" ".join((item.label, plain_text(item.description), *item.options)))))
        or (item.section != "compliance" and (context_dependent(key) or (not checkbox and entry.context_dependent)))
    )
    if category in STATEMENT_CATEGORIES and not statement_control(entry.control, entry.options):
        # A statement is stored only as ticked: a text field, a list or a choice that is not Yes/No has nothing it could be typed as.
        entry.problem_kind = "sensitive_never"
        entry.problem = f"This asks for {words} in a way the app can't answer for you. Finish in browser leaves it for you"
        return entry
    if category == "uncategorized" or category not in sources.sensitive_allowed:
        entry.problem_kind = "sensitive_never" if category == "uncategorized" else "sensitive_not_allowed"
        entry.problem = (
            f"This follows a question the app doesn't answer for you ({words}), so it is left for you too. Finish in browser leaves it for you"
            if follows else f"The app doesn't answer this kind of question for you ({words}). Finish in browser leaves it for you"
        )
        return entry
    if item.label_from_page or entry.text_cut:
        # A consent's statement is on the page only until the page is read, and a description the app cut is text it cannot
        # compare: nothing stored is matched to either, so the student reads it and ticks it (5.4, D9 B).
        entry.problem_kind = "sensitive_never"
        entry.problem = (
            "The statement for this box is only on the form, so the app can't match it to one you stored. Finish in browser leaves it for you"
            if item.label_from_page else
            "The text around this box is too long for the app to check word for word, so it is left for you. Finish in browser leaves it for you"
        )
        return entry
    if boxlike and category in STATEMENT_CATEGORIES and len(normalized_text(entry.statement).split()) < 3:
        # A statement of one or two words ("Acknowledgment") says nothing the student could be shown as agreed to, and the store
        # refuses to hold one: it is left for the student rather than offered a form that cannot be saved.
        entry.problem_kind = "sensitive_never"
        entry.problem = "The statement for this box is too short for the app to match to one you stored. Finish in browser leaves it for you"
        return entry
    stored = sources.sensitive_lookup(
        category=category, question_key=key, company_key=employer_key(ctx.company), mode=ctx.mode, company_only=entry.company_only,
    )
    if not stored:
        entry.problem_kind = "sensitive_missing"
        entry.problem = f"You haven't added an answer for this ({words}) in Apply agent settings"
        return entry
    kind = str(stored.get("answer_kind") or "")
    answer = str(stored.get("answer") or "")
    stored_links = tuple(stored.get("links") or ())
    if boxlike:
        if kind != "checkbox" or _norm(answer) != "checked":
            value, why = None, "This box needs a statement you ticked, and the answer you stored is not one. Remove it in Apply agent settings, or tick it here"
        elif checkbox:
            value, why = True, ""
        else:
            # A Yes/No question that asks for agreement: a statement stored as ticked is the form's one "Yes".
            yes = [option for option in entry.options if _norm(option) == "yes"]
            value, why = (yes[0], "") if len(yes) == 1 else (None, "This question has no single Yes option")
        # The words are the same, but a notice is its document: a form that links to other documents than the ones the
        # student agreed to, or to none, is not agreed to. No links on either side is a value too.
        if value is not None and set(stored_links) != set(entry.links):
            value, why = None, "The document this statement links to is not the one you agreed to. Read it again, then add it again"
    else:
        value, why = _choice_value(entry.control, answer, entry.options)
    if value is None:
        entry.problem_kind, entry.problem = "sensitive_mismatch", f"Your stored answer doesn't fit this form. {why}"
        return entry
    if category in STATEMENT_CATEGORIES:
        noun = "acknowledgment" if category == "acknowledgment" else "consent"
        label = f"Your {noun} (any company)" if not stored.get("company_key") else f"Your {noun} for {ctx.company}"
    else:
        label = "Sensitive answer you added" + (f" {stored['added']}" if stored.get("added") else "")
    entry.source = Source("sensitive", str(stored.get("id") or ""), label=label, links=entry.links)
    entry.value = value
    return entry


def _plan_value(item: SchemaField, entry: PlanField, text: str, ctx: _Context) -> PlanField:
    facts, key = ctx.sources.facts, question_key(item.label)
    control, name = entry.control, item.name
    profile_value, profile_ref = "", ""
    if name in ("first_name", "last_name"):
        first, last, _preferred = name_parts(facts)
        profile_value = first if name == "first_name" else last
        profile_ref = f"name_parts.{'first' if name == 'first_name' else 'last'}"
        if not profile_value:
            entry.problem_kind, entry.problem = "name", _NAME_PROBLEM
            return entry
    elif name == "preferred_name":
        profile_value, profile_ref = name_parts(facts)[2], "name_parts.preferred"
    elif name == "email":
        profile_value, profile_ref = _contact(facts, "email"), "contact.email"
        if not profile_value:
            entry.problem_kind, entry.problem = "profile_fact", "Add your email to your profile"
            return entry
    elif name == "phone":
        profile_value, profile_ref = _contact(facts, "phone"), "contact.phone"
        if not profile_value:
            entry.problem_kind, entry.problem = "profile_fact", "Add your phone number to your profile"
            return entry
    elif item.section == "custom" and control == "text" and key in _PROFILE_KEYS:
        fact = _PROFILE_KEYS[key]
        profile_value, profile_ref = _contact(facts, fact), f"contact.{fact}"
    if profile_value:
        entry.source, entry.value = Source("profile", profile_ref, label="Profile"), profile_value
        return entry
    label_kind = label_field_of(item)
    if label_kind:
        label = ctx.sources.ats_labels.get(label_kind, "")
        if not label:
            entry.problem_kind, entry.label_field = "label_needed", label_kind
            entry.problem = f'Confirm the exact option for "{item.label}" first: the form offers a list of choices only it knows'
            return entry
        entry.source, entry.value = Source("ats_label", label_kind, label="Option you confirmed"), label
        return entry
    if control not in ("text", "textarea", "select", "multiselect", "checkbox"):
        entry.problem_kind, entry.problem = "unsupported", f'The app doesn\'t fill this kind of field ("{item.label}")'
        return entry
    if entry.net_never:
        # The broad net: a question about criminal history, personal details, pay or security, one filed under such a question,
        # or a box that agrees to something in words the classifier has no list for. Nothing in the answer library fills it,
        # at this company or another, and no form offers to save an answer to it.
        words = ", ".join(NET_WORDS[topic] for topic in entry.net_never if topic in NET_WORDS)
        entry.problem_kind = "sensitive_never"
        entry.problem = (
            f"This looks like a question about {words}, so the app never saves an answer to it or fills one in from your saved answers. Finish in browser leaves it for you"
            if words else
            "The app never ticks a box for you from your saved answers, so this is left for you. Finish in browser leaves it for you"
        )
        return entry
    row, kind, problem = _saved_answer(item, text, ctx)
    if row is None:
        entry.problem_kind, entry.problem = kind, problem
        return entry
    value, why = _choice_value(control, str(row["answer"]), entry.options)
    if value is None:
        entry.problem_kind = "answer_mismatch"
        entry.problem = f'{why}. Save an answer that is one of the options' if control in ("select", "multiselect") else f"{why}"
        return entry
    entry.source = Source("answer", str(row["id"]), company=str(row["company"] or ""), label=f"Saved answer for {row['company']}")
    entry.value = value
    return entry


# Fields the app leaves blank on purpose, so an optional one is not a problem for the student: the reason is a note.
_BLANK_KINDS = frozenset({"missing_answer", "sensitive_never", "sensitive_not_allowed", "sensitive_missing", "label_needed", "unsupported", "profile_fact"})


def _settle(entry: PlanField, ctx: _Context) -> PlanField:
    """The disposition (6.6): fill, deferred (a rehearsal's sensitive fields), left_for_you (handoff) or blank."""
    if entry.source.kind != "none" and not entry.problem:
        entry.disposition = "deferred" if entry.defer or (ctx.mode == "rehearse" and entry.sensitive) else "fill"
        if entry.value is not None and entry.control != "file":
            entry.value_mac = value_mac(ctx.sources.mac_key, entry.value)
        return entry
    entry.value = None
    if not entry.required and entry.problem_kind in _BLANK_KINDS:
        entry.note, entry.problem, entry.problem_kind = entry.problem, "", ""
    entry.disposition = "left_for_you" if entry.required and entry.problem and ctx.mode == "handoff" else "blank"
    if not entry.problem and not entry.note:
        entry.note = "Left blank: nothing saved answers it"
    return entry


def build_plan(
    schema: Iterable[SchemaField], scan: Iterable[Any] | None, sources: Sources, company: str, mode: str, *,
    canonical_url: str = "", adapter_version: str = "", uploads_on_attach: bool = False,
) -> Plan:
    """Apply section 7 to every field of the form.

    ``mode`` is rehearse, submit, handoff, or check (the read-only check, judged as a submit would be). What a
    field that cannot be filled does depends on it: a required one is a problem in a submit or a rehearsal and
    is "left for you" in a handoff; an optional one is left blank. ``scan``, when the page has been read, adds
    the problems of joining the listing to the page (apply_checks.join). Nothing here changes a row.
    """
    mode = "submit" if mode == "check" else mode
    if mode not in ("rehearse", "submit", "handoff"):
        raise ValueError(f"Unknown plan mode: {mode!r}")
    fields = [item for item in schema]
    repeated: dict[str, list[str]] = {}
    for item in fields:
        if item.section == "custom":
            repeated.setdefault(question_key(item.label), []).append(item.name)
    texts = {item.name: _answer_key(item, repeated) for item in fields}
    filed: dict[str, int] = {}
    for item in fields:
        if item.section == "custom":
            filed[question_key(texts[item.name][0])] = filed.get(question_key(texts[item.name][0]), 0) + 1
    ctx = _Context(sources, company, mode, repeated, uploads_on_attach)
    entries: list[PlanField] = []
    problems: list[Problem] = []
    # What each question is, by label, for the follow-ups filed under it. A follow-up's own entry is what it
    # inherited, so a follow-up of a follow-up keeps the first question's category.
    own: dict[str, str | None] = {}
    # The same, for the broad net's topics (NET_TOPICS): what each question's own words hit, and what a follow-up chain carries.
    net_own: dict[str, frozenset[str]] = {}
    net_chain: dict[str, frozenset[str]] = {}
    for item in fields:
        control = control_of(item)
        if control == "hidden" or item.name in ALTERNATE_TEXT_FIELDS:
            continue
        text, dependent = texts[item.name]
        category = None
        follows = False
        net: frozenset[str] = frozenset()
        marks: tuple[str, ...] = ()
        if control != "file":
            category = classify_item(item, control)
            # A follow-up takes its meaning from the question above it, so it is as sensitive as that one. Only
            # words that continue another question count: a short question that stands alone ("GPA") does not.
            label_key = question_key(item.label)
            custom_child = item.section == "custom" and bool(item.parent) and label_key not in _PROFILE_KEYS
            parent_category = most_restrictive((own.get(item.parent), classify_sensitive(item.parent))) if custom_child else None
            under_strict = custom_child and parent_category in INHERITING_PARENTS
            # Under a question the app never answers, or one only the student may (a felony, a visa, salary, export control), a
            # question filed under it is that question's continuation whatever its own words: "Year" or "Type" says nothing about
            # what it is the year or the type of. So is a short phrase that asks about no one ("Nature of charge"). Under any
            # other parent only words that continue another question count.
            follows = custom_child and (
                (text != item.label and (under_strict or follow_up_wording(label_key)))
                or (under_strict and len(without_enumeration(label_key).split()) < 5 and not SECOND_PERSON.search(label_key))
            )
            if follows:
                inherited = classify_item(item, control, own.get(item.parent), True)
                follows = inherited is not None and inherited != category
                category = inherited
            own[item.label] = most_restrictive(found for found in (own.get(item.label), category) if found)
            # The broad net, whatever the precise classifier said. A question is read on its own words, and takes the topics of the
            # question right above it (its parent: the group it is filed under) when it follows that one by the rules above, when
            # that one is precisely sensitive, or when the net finds a topic in it: then whatever the child says ("Please tell us
            # what happened", "Sentence received and date of release") is about the parent's subject. Only a profile field (a
            # LinkedIn or portfolio link) is exempt.
            own_net, marks = field_net(item, control)
            topics = set(own_net)
            continues = custom_child and (text != item.label or follow_up_wording(label_key))
            parent_net = net_own.get(item.parent, frozenset()) | set(net_topics(item.parent)) if custom_child else frozenset()
            if custom_child and (continues or parent_category is not None or parent_net):
                topics |= parent_net
                if continues:
                    # A follow-up of a follow-up keeps the first question's topics; a question that only comes after a sensitive
                    # one takes that one's own topics and does not pass them on to what follows it.
                    topics |= net_chain.get(item.parent, frozenset())
                if parent_category is not None:
                    topics.add(CATEGORY_TOPIC[parent_category])
            net = frozenset(topics)
            net_own[item.label] = frozenset(net_own.get(item.label, frozenset()) | own_net)
            net_chain[item.label] = frozenset(net_chain.get(item.label, frozenset()) | (net if continues else own_net))
        # The net tightens only an ordinary question: a sensitive one already goes through the store and never the library.
        ordinary = control != "file" and category is None
        net_company = ordinary and (bool(net) or bool(marks))
        net_never = (tuple(sorted(net & NEVER_TOPICS)) + marks) if ordinary else ()
        entry = PlanField(
            key=item.name, question=item.label, control=control, required=item.required, options=item.options, section=item.section,
            sensitive=category, answer_key=text, context_dependent=dependent or net_company,
            net=tuple(sorted(net)), net_company=net_company and not dependent, net_never=net_never,
        )
        if control == "file":
            _plan_file(item, entry, ctx)
        elif category:
            _plan_sensitive(item, entry, category, ctx, follows=follows)
        elif item.section == "custom" and filed.get(question_key(text), 0) > 1:
            entry.problem_kind = "ambiguous_question"
            entry.problem = f'The form asks "{item.label}" more than once, so the app cannot tell the answers apart'
        else:
            _plan_value(item, entry, text, ctx)
        entries.append(_settle(entry, ctx))
    joined: set[str] = set()
    if scan is not None:
        fills = [entry.key for entry in entries if entry.disposition in ("fill", "deferred")]
        by_name = {entry.key: entry for entry in entries}
        for issue in join(fields, scan, fills):
            problems.append(issue)
            held = by_name.get(issue.key)
            if held is not None and held.disposition in ("fill", "deferred"):
                held.value, held.value_mac = None, ""
                held.disposition = "left_for_you" if mode == "handoff" else "blank"
                held.problem_kind, held.problem = issue.kind, issue.message
                joined.add(held.key)   # its problem is the join's, already listed
    for entry in entries:
        if entry.problem and entry.key not in joined:
            problems.append(Problem(entry.problem_kind, entry.key, entry.problem, entry.question, entry.required))
    plan = Plan(entries, problems)
    plan.plan_hash = plan_hash(plan, canonical_url, adapter_version)
    return plan


def plan_hash(plan: Plan, canonical_url: str = "", adapter_version: str = "") -> str:
    """The fingerprint of everything the student approves (6.6).

    It covers the page's address and the adapter's version, then for each field, by key: the question, the
    control, whether it is required, the option labels, the source's kind and ref, and the value's MAC (or the
    file's hash). A field's disposition is not in it, so a rehearsal that deferred a sensitive field and the
    submit that fills it compare like with like.
    """
    body = {
        "url": canonical_url, "adapter": adapter_version,
        "fields": [
            {"key": item.key, "question": item.question, "control": item.control, "required": bool(item.required),
             "options": list(item.options), "source_kind": item.source.kind, "source_ref": item.source.ref,
             "value_mac": item.value_mac, "file_sha256": item.file_sha256}
            for item in sorted(plan.fields, key=lambda entry: entry.key)
        ],
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def plan_entries(plan: Plan) -> list[dict[str, Any]]:
    """The value-free entries a run stores as ``plan_json`` (5.3): no value, only its MAC and where it came from."""
    return [
        {"key": item.key, "question": item.question, "control": item.control, "required": bool(item.required),
         "options": list(item.options), "sensitive": item.sensitive, "disposition": item.disposition,
         "source": {"kind": item.source.kind, "ref": item.source.ref, "company": item.source.company, "reusable": item.source.reusable,
                    "links": list(item.source.links)},
         "value_mac": item.value_mac, "file_sha256": item.file_sha256, "problem": item.problem}
        for item in plan.fields
    ]
