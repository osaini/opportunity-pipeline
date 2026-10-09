"""A frozen copy of what the Greenhouse ATS seam (LV1a) moved or re-routed, as it was before the seam.

Copied from opportunity_app/apply/greenhouse.py and opportunity_app/apply/policy.py at f77c40e (the head of PR #81, the
last commit before apply/ats.py). It exists only so tests/test_apply_ats_seam.py can run old against new
(AGENTS.md section 8 rule 14). Do not edit it, do not import anything from it but that test, and do not make it import
the code it is the old copy of: its only first-party import is the data class ``SchemaField``, which the seam did not move,
and the two constants the old ``parse_schema`` read are copied here by value.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from opportunity_app.apply.policy import SchemaField

ALTERNATE_TEXT_FIELDS = frozenset({"resume_text", "cover_letter_text"})   # checks.py at f77c40e


class apply_sensitive:  # the old parse_schema spelled the constant apply_sensitive.PLACEHOLDER_NOTE (sensitive.py at f77c40e)
    PLACEHOLDER_NOTE = "(the statement is on the form)"

ATS_GREENHOUSE = "greenhouse"
# The adapter's version (docs/phase5-apply-agent-spec.md 4.4). A rehearsal counts toward the gate only for
# the version the adapter has now, so a change to its selectors or rules means rehearsing again.
ADAPTER_VERSION = "greenhouse-1"
BOARD_HOSTS = frozenset({"job-boards.greenhouse.io", "boards.greenhouse.io"})
SUBMIT_HOST = "boards.greenhouse.io"
GREENHOUSE_DOMAIN = "greenhouse.io"
API_HOST = "boards-api.greenhouse.io"
GREENHOUSE_SENDER_DOMAINS = ("greenhouse.io", "greenhouse-mail.io")


def is_greenhouse_sender(domain: str) -> bool:
    """Whether a sender domain is Greenhouse's or a subdomain of it."""
    domain = (domain or "").lower().rstrip(".")
    return any(domain == known or domain.endswith(f".{known}") for known in GREENHOUSE_SENDER_DOMAINS)


_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_JOB_ID = re.compile(r"^\d+$")
_JOB_PATH = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]{0,79})/jobs/(\d+)/?$")


def canonical_url(board_token: str, job_id: str) -> str:
    return f"https://job-boards.greenhouse.io/{board_token}/jobs/{job_id}"


def schema_url(board_token: str, job_id: str) -> str:
    """Greenhouse's public, keyless listing of what an application form asks (read-only GET)."""
    return f"https://{API_HOST}/v1/boards/{board_token}/jobs/{job_id}?questions=true"


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


_EDUCATION = re.compile(r"^(?:educations?(?:\b|_)|school|degree|discipline|(?:start|end)_date|(?:start|end)_(?:month|year))")
MAX_DESCRIPTION_CHARS = 2000


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
