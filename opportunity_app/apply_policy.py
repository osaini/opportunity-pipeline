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
the student saved (only when the question is word for word the same and it was saved for this company or
marked reusable), an exact option label the student confirmed, and the résumé or cover letter for the role.
Sensitive answers come from one function, ``stored_sensitive_answer``, which returns None until the store
that holds them is built (M4s). Nothing is ever guessed: no fuzzy match, no first option, no label regex.

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

from pipeline import identity_tokens

from . import preparation, resume_variants
from .apply_checks import ALTERNATE_TEXT_FIELDS, BOARD_HOSTS, Problem, join, question_key
from .extension_apply import SENSITIVE_FIELD, ExtensionApplyError, confirmed_resume_file

__all__ = [
    "ALLOWED_ATS_LABEL_FIELDS", "ATS_GREENHOUSE", "CATEGORY_WORDS", "Plan", "PlanField", "SchemaField", "Source", "Sources",
    "build_plan", "canonical_url", "classify_sensitive", "company_matches", "context_dependent", "control_of", "cover_letter_for",
    "identify", "mac_key", "match_options", "name_parts", "needs_label_key", "parse_schema", "plan_entries", "plan_hash",
    "question_key", "resume_for", "schema_url", "sources_for", "stored_sensitive_answer", "value_mac", "with_page_labels",
    "without_enumeration",
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
_EEOC_NAMES = {
    "gender": "eeo_gender", "hispanic_ethnicity": "eeo_hispanic", "race": "eeo_race",
    "veteran_status": "eeo_veteran", "disability_status": "eeo_disability",
}
_SELECTS = frozenset({"multi_value_single_select", "multi_value_multi_select"})


@dataclass(frozen=True)
class SchemaField:
    """One field of the application form, as Greenhouse's listing describes it.

    ``section`` is standard, custom, location, education, compliance, demographic or data_compliance.
    ``parent`` is the label of the question above it in the listing, which gives a follow-up its meaning.
    ``derived_name`` marks a name the live listing does not carry (a demographic question has only an id,
    a data_compliance entry only its consent flags): it is derived, and the page's own controls decide.
    ``label_from_page`` marks a label the listing does not carry: a consent statement is on the form only.
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
        description = str(block.get("description") or "")[:2000]
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
                section=_section_of(name, section), parent=previous, description=description, compliance_type=compliance_type,
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
            description=str(question.get("description") or "")[:2000], derived_name=True,
        ))
    for entry in listing.get("data_compliance") or []:
        if not isinstance(entry, Mapping):
            continue
        kind = re.sub(r"[^a-z0-9]+", "_", str(entry.get("type") or "").lower()).strip("_")
        # Consent is required when Greenhouse says so. Its statement is only on the form, and so is its control's name.
        if kind and any(entry.get(flag) for flag in ("requires_consent", "requires_processing_consent", "requires_retention_consent")):
            found.append(SchemaField(
                name=f"{kind}_consent_given", label=f"{kind.upper()} data consent (the statement is on the form)", required=True,
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


# --- Which questions never take a stored answer, and which need a label (7.1) -------------------------

# Text that takes its meaning from the question above it, not from the company. These mirror the shared
# engine's rules (apps/extension/apply-engine.js: needsLabelKey, withoutEnumeration, CONTEXT_WORDING);
# tests/fixtures/apply/context_keys.json is run by both suites, so they cannot drift apart. The Python side
# must never be looser than the engine.
_CONTEXT_OPENER = re.compile(r"^(?:if yes|if so|if no|if other|please specify|please explain|please describe|other|explain)\b")
_CONTEXT_IF = re.compile(r"^if\b")
_CONTEXT_IF_ANY = re.compile(r"\bif (?:yes|so|no|not|other|applicable|any)\b")
_CONTEXT_PLEASE = re.compile(r"\bplease (?:explain|specify|describe|elaborate)\b")
_CONTEXT_DETAILS = re.compile(r"\b(?:provide|give|share|include|add|list)\s+(?:[a-z']+\s+){0,3}?(?:details?|information|info|context|explanations?)\b")
_CONTEXT_PRONOUN = re.compile(r"\b(?:list|name|give|provide|share|describe|explain|specify|identify) (?:them|it|those|these|each)\b")
_CONTEXT_VERB = re.compile(r"\b(?:explain|explanation|specify|elaborate|clarify|expand)\b")
_CONTEXT_DESCRIBE = re.compile(r"\bdescribe\b")
_CONTEXT_PHRASE = re.compile(
    r"\btell us (?:more|why)\b|\bwhy or why not\b|\bif applicable\b|\byour (?:answer|response)s? (?:above|to the previous)\b|\bprevious question\b|\bthe above\b"
)
_CONTEXT_WH = re.compile(r"^(?:which|what|when|where|who|whom|whose|how|why)\b")
# Questions whose truth depends on the employer. Found from this wording list and nothing smarter.
_CONTEXT_WORDING = re.compile(
    r"previously (?:worked|been employed|applied)|worked (?:here|for us|for this company|at)|applied (?:here|before|previously)|referr|who referred"
    r"|know (?:anyone|someone)|how did you hear|where did you (?:hear|find)|current(?:ly)? (?:an )?employee"
    r"|worked (?:for|with|at) (?:us|this|our|the company)|employed (?:by|at|with)|interviewed (?:with|at|here)|relatives?\b|family members?\b"
    r"|this (?:organi[sz]ation|firm|company|employer)"
)


def without_enumeration(key: str) -> str:
    """The key with a leading enumeration or bullet ("b.", "1a)", "(ii)", "Question 3:", "Follow-up:") taken off."""
    text = re.sub(r"'+(?=\s|$)", "", re.sub(r"(^|\s)'+", r"\1", key)).strip()
    text = re.sub(r"^follow ?up(?: question)?\s+(?=\S)", "", text)
    for _ in range(4):
        following = re.sub(r"^(?:question|part|step|section|item|no|number)\s+(?:\d{1,3}[a-z]?|[a-z]|[ivx]{1,4})\s+(?=\S)", "", text)
        following = re.sub(r"^(?:[a-z]|[ivx]{1,4}|\d{1,3}[a-z]?|[a-z]\d{1,3}[a-z]?)\s+(?=\S)", "", following)
        if following == text:
            break
        text = following
    return text


def needs_label_key(key: str) -> bool:
    """Whether a question key is a follow-up, an opener or too short to stand on its own words."""
    text = without_enumeration(key)
    words = len([word for word in text.split(" ") if word])
    return (
        len([word for word in key.split(" ") if word]) < 3 or words < 3
        or any(_CONTEXT_OPENER.search(item) or _CONTEXT_PLEASE.search(item) for item in (key, text))
        or bool(_CONTEXT_IF.search(text)) or bool(_CONTEXT_IF_ANY.search(text)) or bool(_CONTEXT_DETAILS.search(text))
        or bool(_CONTEXT_PRONOUN.search(text)) or bool(_CONTEXT_PHRASE.search(text))
        or (words < 8 and bool(_CONTEXT_VERB.search(text))) or (words < 6 and bool(_CONTEXT_DESCRIBE.search(text)))
        or (words < 6 and bool(_CONTEXT_WH.search(text)))
    )


def context_dependent(key: str) -> bool:
    """A key whose saved answer is never reused for another company, even when the row is tagged reusable."""
    return needs_label_key(key) or bool(_CONTEXT_WORDING.search(key))


# --- What counts as sensitive (7.3) ----------------------------------------------------------------

# On the normalized question: lower case, every run of anything but a-z and 0-9 one space.
_OPT_NOT_MARKETING = r"\bopt\b(?! (?:in|out)\b(?! (?:the )?(?:us|u s|usa|united states|20\d\d)(?!\w)))"
_AGE_TAIL = r"(?: years?)?(?: (?:of age|old|or older|or over|and older|and over))*"
_AGE_18 = re.compile(
    rf"\b(?:(?:at least|over|above|older than) (?:the age of )?18{_AGE_TAIL}|(?:the )?age of 18{_AGE_TAIL}"
    rf"|18(?: years?)?(?: (?:of age|old|or older|or over|and older|and over))+)"
)
_PATTERNS: dict[str, re.Pattern[str]] = {
    "work_authorization": re.compile(
        r"authori[sz]ed to work|authori[sz]ation to work|work authori[sz]ation|legally (?:eligible|authori[sz]ed)|right to work|eligible to work"
    ),
    "sponsorship": re.compile(
        r"sponsor|immigration|petition|employment based|visa (?:sponsor|status|support|type|holder|transfer)"
        rf"|(?:require|need|hold)\w* (?:a )?visa|work visa|student visa|\b(?:f ?1|j ?1|h ?1 ?b|tn|e ?3)\b|\bstem opt\b|{_OPT_NOT_MARKETING}|\bcpt\b|practical training"
    ),
    "age_18": _AGE_18,
    "export_control": re.compile(
        r"u s person|us person|\bitar\b|export administration regulations|export control|citizen|permanent resident|green card|clearance"
    ),
    "eeo_gender": re.compile(r"\bgender\b|\bsex\b"),
    "eeo_hispanic": re.compile(r"hispanic|latin[oax]"),
    "eeo_race": re.compile(r"\brace\b|ethnic"),
    "eeo_veteran": re.compile(r"veteran|military service"),
    "eeo_disability": re.compile(r"disab"),
    "acknowledgment": re.compile(r"i (?:certify|attest|acknowledge|confirm|understand|agree)|accura|truthful|have read|privacy (?:notice|policy)"),
    "consent": re.compile(r"consent|retain|retention|process(?:ing)? (?:of )?(?:my|your) (?:personal )?(?:data|information)|gdpr"),
    "salary": re.compile(r"salary|compensation|pay (?:expectation|range)|desired pay|expected pay|hourly rate"),
}
_NEVER_STORABLE = re.compile(
    r"\bage\b|birth|pronoun|marital|religio|genetic|pregnan|criminal|convict|felony|misdemeanor|arrest|background check|sexual orientation|transgender|non ?compete"
)
# Most restrictive first (7.3 step 3).
_RESTRICTION = (
    "uncategorized", "export_control", "salary", "sponsorship", "work_authorization", "age_18",
    "eeo_gender", "eeo_hispanic", "eeo_race", "eeo_veteran", "eeo_disability", "acknowledgment", "consent",
)
_OPTION_FLAGS = (
    ("export_control", re.compile(r"citizen|clearance|green card|permanent resident")),
    ("sponsorship", re.compile(rf"visa|h ?1 ?b|{_OPT_NOT_MARKETING}|sponsor")),
)
_DECLINE = re.compile(
    r"decline to (?:self identify|answer|state|identify|disclose)|do(?: not|n t) wish to (?:answer|disclose|identify|say)"
    r"|do not want to answer|prefer not to (?:say|answer|disclose|identify)"
)
CATEGORY_WORDS = {
    "work_authorization": "work authorization", "sponsorship": "visa sponsorship or immigration status", "age_18": "18 or older",
    "export_control": "export control, citizenship or security clearance", "salary": "salary",
    "eeo_gender": "voluntary self-identification", "eeo_hispanic": "voluntary self-identification",
    "eeo_race": "voluntary self-identification", "eeo_veteran": "voluntary self-identification",
    "eeo_disability": "voluntary self-identification", "acknowledgment": "a legal acknowledgment",
    "consent": "a data-processing consent", "uncategorized": "a personal question",
}


def _words(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text if text is not None else "").lower()).strip()


def _most_restrictive(categories: Iterable[str]) -> str | None:
    found = set(categories)
    return next((category for category in _RESTRICTION if category in found), None)


def classify_sensitive(question: str, options: Iterable[str] = (), section: str = "", field_name: str = "") -> str | None:
    """A sensitive category, ``"uncategorized"`` (sensitive, never storable), or None (an ordinary question).

    The rules run in the order of spec 7.3. The result can only be stricter than the extension's own
    ``SENSITIVE`` rule: anything that rule flags and nothing here places is ``"uncategorized"``.
    """
    text = _words(question)
    # 1. Never storable, for every section. An 18-or-older phrase goes first, or "years of age" would trip \bage\b.
    if _NEVER_STORABLE.search(_AGE_18.sub(" ", text)):
        return "uncategorized"
    # 2. Section rules. EEO fields are mapped by their schema field names only.
    if section in ("compliance", "demographic", "demographic_questions"):
        return _EEOC_NAMES.get(field_name, "uncategorized")
    if section == "data_compliance":
        return "consent"
    # 3. The question's own words.
    found = {category for category, pattern in _PATTERNS.items() if pattern.search(text)}
    # 4. Options fail closed: a vague question with visa, citizenship or clearance choices is sensitive too.
    choices = [_words(option) for option in options]
    for category, pattern in _OPTION_FLAGS:
        if any(pattern.search(choice) for choice in choices):
            found.add(category)
    if any(_DECLINE.search(choice) for choice in choices):
        return "uncategorized"
    result = _most_restrictive(found)
    # 5. Anything the extension flags and no row places can never be answered, but is never ordinary either.
    if result is None and SENSITIVE_FIELD.search(str(question or "")):
        return "uncategorized"
    return result


def stored_sensitive_answer(
    conn: sqlite3.Connection, user_id: str, *, category: str, question_key: str, company_key: str, mode: str,
) -> dict[str, Any] | None:
    """The stored answer the student deliberately added for this sensitive question, or None.

    The one place the plan asks. The store (apply_sensitive_answers, spec 5.4) is built in M4s; until then
    nothing is stored, so nothing sensitive is ever answered. The entry it will return holds ``id``,
    ``answer_kind`` (option, options, text or checkbox), ``answer`` and the statement's ``links``, and only
    when the exact key, the category, the company and the consent scope for ``mode`` all match.
    """
    return None


# --- Where a value may come from (7.1) --------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """kind is profile, ats_label, answer, sensitive, resume, cover_letter or none. ``ref`` names the row, never a value."""

    kind: str = "none"
    ref: str = ""
    company: str = ""
    reusable: bool = False
    label: str = ""


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


def _loads(text: Any, default: Any) -> Any:
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _setting(conn: sqlite3.Connection, user_id: str, key: str) -> str:
    row = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, key)).fetchone()
    return str(row[0]) if row and row[0] is not None else ""


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
        {**dict(row), "tags": [str(tag) for tag in _loads(row["tags_json"], [])]}
        for row in conn.execute(
            "SELECT id, question, answer, company, tags_json, updated_at FROM answer_library WHERE user_id=? ORDER BY updated_at DESC, id", (user_id,),
        ).fetchall()
    ]
    labels = {
        str(row["field"]): str(row["label"])
        for row in conn.execute("SELECT field, label FROM apply_ats_labels WHERE user_id=? AND ats=?", (user_id, ATS_GREENHOUSE)).fetchall()
    }
    allowed = frozenset(part.strip() for part in _setting(conn, user_id, "apply_sensitive_categories").split(",") if part.strip())

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
    key = question_key(item.label)
    dependent = context_dependent(key)
    if needs_label_key(key) or len(repeated.get(key, [])) > 1:
        return (f"{item.parent} / {item.label}" if item.parent else item.label), True
    return item.label, dependent


def company_matches(row_company: str, company: str) -> bool:
    mine, theirs = identity_tokens(company), identity_tokens(row_company)
    return bool(mine) and mine == theirs


def _saved_answer(item: SchemaField, text: str, dependent: bool, ctx: _Context) -> tuple[dict[str, Any] | None, str, str]:
    """(the usable saved answer, problem_kind, problem) under the company rule and the one-answer rule (7.1)."""
    key = question_key(text)
    rows = [row for row in ctx.sources.answers if question_key(row["question"]) == key and str(row["answer"]).strip()]
    usable, elsewhere = [], []
    for row in rows:
        same = company_matches(str(row["company"] or ""), ctx.company)
        reusable = any(str(tag).lower() == "reusable" for tag in row["tags"])
        if same or (reusable and not dependent and not context_dependent(key)):
            usable.append(row)
        else:
            elsewhere.append(row)
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
    if item.name.startswith("cover_letter"):
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


def _plan_sensitive(item: SchemaField, entry: PlanField, category: str, ctx: _Context) -> PlanField:
    words = CATEGORY_WORDS.get(category, "a personal question")
    sources = ctx.sources
    key = question_key(item.label)
    if category == "uncategorized" or category not in sources.sensitive_allowed:
        entry.problem_kind = "sensitive_never" if category == "uncategorized" else "sensitive_not_allowed"
        entry.problem = f"The app doesn't answer this kind of question for you ({words}). Finish in browser leaves it for you"
        return entry
    stored = sources.sensitive_lookup(category=category, question_key=key, company_key=" ".join(sorted(identity_tokens(ctx.company))), mode=ctx.mode)
    if not stored:
        entry.problem_kind = "sensitive_missing"
        entry.problem = f"You haven't added an answer for this ({words}) in Apply agent settings"
        return entry
    kind = str(stored.get("answer_kind") or "")
    answer = str(stored.get("answer") or "")
    if entry.control == "checkbox":
        value, why = (True, "") if kind == "checkbox" and _norm(answer) == "checked" else (None, "The stored statement is not the one on this form")
    else:
        value, why = _choice_value(entry.control, answer, entry.options)
    if value is None:
        entry.problem_kind, entry.problem = "sensitive_mismatch", f"Your stored answer doesn't fit this form's options. {why}"
        return entry
    entry.source = Source("sensitive", str(stored.get("id") or ""), label="Sensitive answer you added")
    entry.value = value
    return entry


def _plan_value(item: SchemaField, entry: PlanField, text: str, dependent: bool, ctx: _Context) -> PlanField:
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
    row, kind, problem = _saved_answer(item, text, dependent, ctx)
    if row is None:
        entry.problem_kind, entry.problem = kind, problem
        return entry
    value, why = _choice_value(control, str(row["answer"]), entry.options)
    if value is None:
        entry.problem_kind = "answer_mismatch"
        entry.problem = f'{why}. Save an answer that is one of the options' if control in ("select", "multiselect") else f"{why}"
        return entry
    same = company_matches(str(row["company"] or ""), ctx.company)
    reusable = any(str(tag).lower() == "reusable" for tag in row["tags"])
    entry.source = Source(
        "answer", str(row["id"]), company=str(row["company"] or ""), reusable=reusable and not same,
        label=f"Saved answer for {row['company']}" if same else "Saved answer, reusable" + (f" (first saved for {row['company']})" if row["company"] else ""),
    )
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
    for item in fields:
        control = control_of(item)
        if control == "hidden" or item.name in ALTERNATE_TEXT_FIELDS:
            continue
        text, dependent = texts[item.name]
        category = None if control == "file" else classify_sensitive(item.label, item.options, item.section, item.name)
        entry = PlanField(
            key=item.name, question=item.label, control=control, required=item.required, options=item.options, section=item.section,
            sensitive=category, answer_key=text, context_dependent=dependent,
        )
        if control == "file":
            _plan_file(item, entry, ctx)
        elif category:
            _plan_sensitive(item, entry, category, ctx)
        elif item.section == "custom" and filed.get(question_key(text), 0) > 1:
            entry.problem_kind = "ambiguous_question"
            entry.problem = f'The form asks "{item.label}" more than once, so the app cannot tell the answers apart'
        else:
            _plan_value(item, entry, text, dependent, ctx)
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
         "source": {"kind": item.source.kind, "ref": item.source.ref, "company": item.source.company, "reusable": item.source.reusable},
         "value_mac": item.value_mac, "file_sha256": item.file_sha256, "problem": item.problem}
        for item in plan.fields
    ]
