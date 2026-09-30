"""Apply for me, the sensitive-answers store: what the student allowed the app to answer on a sensitive question.

The store holds the few answers the student chose to let the app type into an application form
(docs/phase5-apply-agent-spec.md 5.4, 7.1, 11): work authorization, visa sponsorship, 18 or older, an EEO
question answered only as a decline, and the exact wording of a legal acknowledgment or a data consent. Each
entry keeps the question exactly as the form showed it, the answer, and the consent it was given under (when,
and for which kind of use). Nothing else is ever stored here:

- export control, citizenship, security clearance and salary are refused, and so is every question the
  classifier places as ``uncategorized`` (age, birth date, pronouns, religion, criminal history, ...);
- an EEO question is stored only as a decline ("Decline To Self Identify", "I don't wish to answer"), so this
  table holds no demographic value, and the service refuses anything else. This is the student's D5 C (i);
- a question is stored only under the kind the wording reads as (a consent may be worded as an acknowledgment), so a
  demographic question cannot be filed as work authorization to get a real value past the decline check;
- a statement that says "I have read" or points to a document is saved for one company, never for any company:
  one employer's notice is not another's. So is any question that depends on its company (7.1).

Who reads it. ``lookup`` is asked by apply_policy (the plan) and by nothing else: the extension's apply context,
``/api/v1/extension/*``, the saved-answer library, employer views and every report never read this table
(tests/test_apply_sensitive.py scans the source for it). Who writes it: ``add_entry`` and ``delete_entry``, from
routes that need the student's own browser session. The plan is read-only, so ``lookup`` writes nothing.

Matching is exact: the question's normalized key, the category, and the company, which is the entry's own or
"any company". For a statement the key is the whole statement text, so one changed word is a miss.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from pipeline import identity_tokens

from .apply_checks import question_key
from .schema import utc_now

__all__ = [
    "CATEGORY_GROUPS", "CONSENT_TEXT", "DECLINE_EXAMPLES", "EEO_CATEGORIES", "LABELS", "STATEMENT_CATEGORIES", "STORABLE", "StoreRefused", "add_entry", "allowed_categories",
    "PLACEHOLDER_NOTE", "TICKABLE", "cites_document", "company_key", "delete_entry", "is_decline", "links_in", "list_entries", "lookup", "set_allowed_categories",
]

SETTING_KEY = "apply_sensitive_categories"
MAX_ENTRIES = 500
MAX_QUESTION_CHARS = 4000
MAX_ANSWER_CHARS = 500
MAX_LINKS = 8
MAX_LINK_CHARS = 500

# What the student ticks before an entry is saved (5.4). The same words are on the form and in the record's meaning.
CONSENT_TEXT = (
    "Use this answer only to fill in application forms when I ask the app to apply, and for nothing else. "
    "I look over each application before it is sent."
)

# The only categories that can ever be stored, in the order the settings list them. export_control and salary are
# in the table's CHECK so a later choice needs no migration, and are refused here; ``uncategorized`` has no row.
STORABLE = (
    "work_authorization", "sponsorship", "age_18",
    "eeo_gender", "eeo_hispanic", "eeo_race", "eeo_veteran", "eeo_disability",
    "acknowledgment", "consent",
)
EEO_CATEGORIES = tuple(category for category in STORABLE if category.startswith("eeo_"))
STATEMENT_CATEGORIES = ("acknowledgment", "consent")
# A single box that states the answer ("I confirm that I am at least 18 years of age") is stored as ticked too.
TICKABLE = ("work_authorization", "sponsorship", "age_18")
# What the app writes in place of a consent statement the listing does not carry. It is never a statement to store.
PLACEHOLDER_NOTE = "(the statement is on the form)"
# What the student switches on, in words. One switch covers the five EEO fields, which are stored as declines only.
CATEGORY_GROUPS = (
    ("work_authorization", "Work authorization", ("work_authorization",)),
    ("sponsorship", "Visa sponsorship and immigration status", ("sponsorship",)),
    ("age_18", "18 or older", ("age_18",)),
    ("eeo", "Voluntary self-identification (EEO), as a decline answer only", EEO_CATEGORIES),
    ("acknowledgment", "Legal acknowledgments, word for word", ("acknowledgment",)),
    ("consent", "Data-processing consents, word for word", ("consent",)),
)
LABELS = {
    "work_authorization": "Work authorization", "sponsorship": "Visa sponsorship or immigration status", "age_18": "18 or older",
    "eeo_gender": "Gender (voluntary self-identification)", "eeo_hispanic": "Hispanic or Latino (voluntary self-identification)",
    "eeo_race": "Race (voluntary self-identification)", "eeo_veteran": "Veteran status (voluntary self-identification)",
    "eeo_disability": "Disability status (voluntary self-identification)", "acknowledgment": "Legal acknowledgment",
    "consent": "Data-processing consent",
}
# Labels live forms use for the one answer an EEO question may have here. The list the service checks is longer.
DECLINE_EXAMPLES = ("Decline To Self Identify", "I don't wish to answer", "I do not want to answer", "Prefer not to say")
_NEVER = {
    "export_control": "The app never answers export control, citizenship or security clearance questions: a wrong answer there can have legal weight",
    "salary": "The app never answers salary questions: it is a negotiating position, not a fact",
    "uncategorized": "The app never stores an answer to this kind of personal question",
}
_WORDS = {
    "work_authorization": "work authorization", "sponsorship": "visa sponsorship or immigration status", "age_18": "18 or older",
    "eeo_gender": "voluntary self-identification", "eeo_hispanic": "voluntary self-identification", "eeo_race": "voluntary self-identification",
    "eeo_veteran": "voluntary self-identification", "eeo_disability": "voluntary self-identification",
    "acknowledgment": "a legal acknowledgment", "consent": "a data-processing consent",
}

_ANSWER_KINDS = ("option", "options", "text", "checkbox")
_CHECKED = "checked"
# The kinds of answer that may be stored under a second name for the wording: a consent is often worded as an acknowledgment.
_PAIRED = frozenset({("acknowledgment", "consent"), ("consent", "acknowledgment")})


class StoreRefused(ValueError):
    """The entry cannot be stored; the message says why, in words for the student."""


def company_key(name: str) -> str:
    """The words that identify an employer, sorted and joined: "Acme Robotics Inc." and "ACME robotics" match. '' means none."""
    return " ".join(sorted(identity_tokens(name)))


def _words(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text if text is not None else "").lower()).strip()


def _now(now: datetime | None) -> str:
    return utc_now() if now is None else now.isoformat(timespec="microseconds")


# --- The one EEO answer that may be stored ---------------------------------------------------------------

# A fixed list, matched on the whole label (not searched for): "Decline To Self Identify" (race, gender) and "I don't
# wish to answer" (veteran status) are what live Greenhouse boards offer. A label that merely contains one of these
# words, such as "Yes, and I do not wish to say more", is not a decline. Anything else is a demographic value.
_DECLINE_LABELS = frozenset(
    f"{lead}{core}"
    for lead in ("", "i ")
    for core in (
        "decline to self identify", "decline to answer", "decline to state", "decline to identify", "decline to disclose",
        "do not wish to answer", "don t wish to answer", "do not want to answer", "don t want to answer",
        "do not wish to self identify", "don t wish to self identify", "do not wish to disclose", "don t wish to disclose",
        "do not wish to say", "don t wish to say", "prefer not to answer", "prefer not to say", "prefer not to disclose",
        "prefer not to identify", "prefer not to self identify", "choose not to answer", "choose not to disclose",
        "choose not to self identify",
    )
)


def is_decline(text: Any) -> bool:
    """Whether an answer is a decline to answer an EEO question, by its whole label. Case and punctuation do not matter."""
    return _words(text) in _DECLINE_LABELS


# --- Links a statement cites -------------------------------------------------------------------------------

_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_BARE_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
# A statement that reads a document ("I have read the privacy notice") depends on which document that is. Leaning
# wide is the safe way: it makes the entry company-specific, and the student is asked once more for a new company.
_DOCUMENT_WORDS = re.compile(
    r"\bhave read\b|\bread and (?:understood?|agree|accept)|acknowledge receipt|\breceipt of\b|\bprivacy\b|\bterms of\b|\bterms and conditions\b"
    r"|\bpolic(?:y|ies)\b|\bnotice\b|\bhandbook\b|\bagreement\b|https?://"
)


def _clean_url(url: str) -> str:
    text = html.unescape(url).strip().rstrip(".,;")
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or len(text) > MAX_LINK_CHARS:
        return ""
    return text


def links_in(*texts: Any) -> tuple[str, ...]:
    """The http and https addresses a statement's own words point to (an anchor's href, or a bare address), once each, in order."""
    found: list[str] = []
    for text in texts:
        raw = str(text or "")
        for match in list(_HREF.finditer(raw)) + list(_BARE_URL.finditer(html.unescape(raw))):
            url = _clean_url(match.group(1) if match.re is _HREF else match.group(0))
            if url and url not in found:
                found.append(url)
    return tuple(found)


def cites_document(statement: str, links: Iterable[str] = ()) -> bool:
    """Whether a statement reads or links a document, so it is only ever saved for one company."""
    return bool(tuple(links)) or bool(_DOCUMENT_WORDS.search(_words(statement))) or bool(links_in(statement))


# --- What the student allowed ---------------------------------------------------------------------------------

def allowed_categories(conn: sqlite3.Connection, user_id: str) -> frozenset[str]:
    """The categories the student switched on (D5). Empty by default, and never more than the storable ones."""
    row = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, SETTING_KEY)).fetchone()
    parts = str(row[0]).split(",") if row and row[0] else []
    return frozenset(part.strip() for part in parts if part.strip() in STORABLE)


def set_allowed_categories(conn: sqlite3.Connection, user_id: str, categories: Iterable[str], *, now: datetime | None = None) -> list[str]:
    """Replace the switched-on categories. Anything outside the storable set is refused, with the reason for the never ones."""
    wanted = {str(category).strip() for category in categories if str(category).strip()}
    for category in sorted(wanted):
        if category not in STORABLE:
            raise StoreRefused(_NEVER.get(category, f"Unknown kind of answer: {category}"))
    ordered = [category for category in STORABLE if category in wanted]
    with conn:
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (user_id, SETTING_KEY, ",".join(ordered), _now(now)),
        )
    return ordered


# --- Writing --------------------------------------------------------------------------------------------------

def _one_line(text: Any, limit: int, what: str) -> str:
    value = " ".join(str(text if text is not None else "").split())
    if not value:
        raise StoreRefused(f"Give the {what} first")
    if len(value) > limit:
        raise StoreRefused(f"The {what} is too long")
    return value


def add_entry(
    conn: sqlite3.Connection, user_id: str, *, category: str, question: str, answer: Any = "", answer_kind: str = "",
    company: str = "", links: Sequence[str] = (), company_only: bool = False, consent: bool = False, now: datetime | None = None,
    from_form: bool = False,
) -> dict[str, Any]:
    """Store one answer, or refuse it. The same question for the same company is replaced, and its consent is given again.

    ``question`` is the exact wording the form showed (for a statement, the whole statement). ``company`` is the
    employer's name: empty means any company, which a statement that cites a document and a follow-up that depends
    on its company (``company_only``, or wording that says so) are refused. ``consent`` must be True: the student ticked the box in
    ``CONSENT_TEXT``. ``from_form`` says the category is the plan's own for this very wording (the Needs-you route), so the
    plan's reading of the options, the question above or the heading is not second-guessed; a demographic value is
    refused either way. The write is one transaction.
    """
    if category in _NEVER or category not in STORABLE:
        raise StoreRefused(_NEVER.get(category, f"Unknown kind of answer: {category}"))
    if consent is not True:
        raise StoreRefused("Tick the box to say the app may use this answer to fill in application forms")
    if category not in allowed_categories(conn, user_id):
        raise StoreRefused(f"Allow answers about {_WORDS[category]} first, in Apply for me settings")
    text = _one_line(question, MAX_QUESTION_CHARS, "question")
    key = question_key(text)
    if not key:
        raise StoreRefused("Give the question exactly as the form shows it")
    # The wording decides too: an answer to "Are you a U.S. citizen or authorized to work in the U.S.?" is never stored
    # under a more permissive name. The classifier is the plan's own, imported here because the plan imports this module.
    from .apply_policy import classify_sensitive

    read_as = classify_sensitive(text)
    if read_as in _NEVER:
        raise StoreRefused(f"This question reads as one the app never answers. {_NEVER[read_as]}")
    # The plan files this wording under its own reading, so a row under a stricter or unrelated kind is never read, and a real
    # demographic value must never get in under, say, work authorization (D5 C (i)). The EEO line is therefore always drawn;
    # among the other kinds a wording that reads as more restrictive than the kind it is filed under is refused, but a
    # plan's category may come from the options, the question above or the heading, which the text alone cannot see.
    if read_as is not None and read_as != category and (category, read_as) not in _PAIRED:
        eeo_read, eeo_filed = read_as in EEO_CATEGORIES, category in EEO_CATEGORIES
        if eeo_read != eeo_filed or (not eeo_read and not from_form and _stricter(read_as, category)):
            raise StoreRefused(f"This question reads as {_WORDS.get(read_as, 'another kind of answer')}. Add it under that kind of answer instead")
    if PLACEHOLDER_NOTE in text:
        raise StoreRefused("That is the app's own placeholder, not the statement. Give the statement as the form shows it, word for word")
    statement = category in STATEMENT_CATEGORIES
    ticked = statement or (category in TICKABLE and answer_kind == "checkbox")
    if ticked:
        kind, stored = "checkbox", _CHECKED
        if str(answer).strip().casefold() not in ("", _CHECKED, "true", "yes", "1"):
            raise StoreRefused("A statement is stored only as ticked")
        if len(_words(text).split()) < 3:
            raise StoreRefused("Give the whole statement the box shows, word for word")
        cited = _clean_links(list(links) + list(links_in(text)))
    else:
        kind = answer_kind or "option"
        if kind not in _ANSWER_KINDS or kind == "checkbox":
            raise StoreRefused("That kind of answer is not stored for this question")
        if kind == "options":
            parts = [" ".join(str(part).split()) for part in (answer if isinstance(answer, list) else str(answer).split("\n"))]
            stored = "\n".join(part for part in parts if part)
            if not stored or len(stored) > MAX_ANSWER_CHARS:
                raise StoreRefused("Give the answer first")
        else:
            stored = _one_line(answer, MAX_ANSWER_CHARS, "answer")
        cited = ()
    if category in EEO_CATEGORIES:
        # D5 C (i): this table never holds a demographic value. Checked here, so no route and no future caller can bypass it.
        if kind != "option" or not is_decline(stored):
            raise StoreRefused("The app stores only a decline answer (such as \"Decline To Self Identify\") for these questions, never a real answer")
    mine = company_key(company) if str(company or "").strip() else ""
    if str(company or "").strip() and not mine:
        raise StoreRefused("This role has no company name the app can match on")
    if not mine and company_only:
        raise StoreRefused("This question depends on the company, so its answer is saved for this company only")
    # Whatever the caller says: an EEO decline holds nothing about a company, anything else that reads as depending on its
    # employer ("this company", a follow-up, a bare heading) is never kept for every company (7.1).
    if not mine and category not in EEO_CATEGORIES and _depends_on_company(key):
        raise StoreRefused("This question depends on the company, so its answer is saved for this company only")
    if not mine and ticked and cites_document(text, cited):
        raise StoreRefused("This statement points to a document, so it is saved for one company only, never for any company")
    stamp = _now(now)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    with conn:
        if conn.execute("SELECT 1 FROM apply_sensitive_answers WHERE user_id=? AND question_hash=? AND company_key=?", (user_id, digest, mine)).fetchone() is None:
            if conn.execute("SELECT COUNT(*) FROM apply_sensitive_answers WHERE user_id=?", (user_id,)).fetchone()[0] >= MAX_ENTRIES:
                raise StoreRefused("There are too many stored answers. Remove some first")
        conn.execute(
            "INSERT INTO apply_sensitive_answers(id, user_id, category, question_text, question_key, question_hash, answer_kind, answer, company_key, "
            "statement_links_json, consent_scope, consented_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?) "
            "ON CONFLICT(user_id, question_hash, company_key) DO UPDATE SET category=excluded.category, question_text=excluded.question_text, "
            "question_key=excluded.question_key, answer_kind=excluded.answer_kind, answer=excluded.answer, "
            "statement_links_json=excluded.statement_links_json, consent_scope='confirmed', consented_at=excluded.consented_at, updated_at=excluded.updated_at",
            (f"sens-{uuid4().hex}", user_id, category, text, key, digest, kind, stored, mine, json.dumps(list(cited)), stamp, stamp, stamp),
        )
        row = conn.execute(
            "SELECT * FROM apply_sensitive_answers WHERE user_id=? AND question_hash=? AND company_key=?", (user_id, digest, mine),
        ).fetchone()
    return _view(row, allowed_categories(conn, user_id))


def _stricter(read_as: str, category: str) -> bool:
    """Whether a wording's reading is a more restrictive kind than the one it is filed under (the plan's own order)."""
    from .apply_policy import _RESTRICTION

    return _RESTRICTION.index(read_as) < _RESTRICTION.index(category)


def _depends_on_company(key: str) -> bool:
    from .apply_policy import context_dependent

    return context_dependent(key)


def _clean_links(urls: Iterable[str]) -> tuple[str, ...]:
    found: list[str] = []
    for url in urls:
        cleaned = _clean_url(str(url))
        if not cleaned:
            raise StoreRefused("A link must be a full http or https address")
        if cleaned not in found:
            found.append(cleaned)
    if len(found) > MAX_LINKS:
        raise StoreRefused("A statement links to too many documents")
    return tuple(found)


def delete_entry(conn: sqlite3.Connection, user_id: str, entry_id: str) -> bool:
    """Remove one entry at once. A plan that used it no longer matches, so a submit built on it is refused (5.4)."""
    with conn:
        cursor = conn.execute("DELETE FROM apply_sensitive_answers WHERE id=? AND user_id=?", (entry_id, user_id))
    return bool(cursor.rowcount)


# --- Reading ----------------------------------------------------------------------------------------------------

def _loads_links(text: Any) -> list[str]:
    try:
        value = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _view(row: Any, allowed: frozenset[str]) -> dict[str, Any]:
    category = str(row["category"])
    mine = str(row["company_key"] or "")
    return {
        "id": str(row["id"]), "category": category, "words": _WORDS.get(category, ""), "question": str(row["question_text"]),
        "answer": str(row["answer"]), "answer_kind": str(row["answer_kind"]), "company": mine.title(), "any_company": not mine,
        "links": _loads_links(row["statement_links_json"]), "consent_scope": str(row["consent_scope"]), "consented_at": str(row["consented_at"]),
        "last_used_at": str(row["last_used_at"] or ""), "switched_on": category in allowed,
    }


def list_entries(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    """The student's own entries for the settings page. Answers are shown to their owner and go nowhere else."""
    allowed = allowed_categories(conn, user_id)
    rows = conn.execute("SELECT * FROM apply_sensitive_answers WHERE user_id=? ORDER BY category, created_at, id", (user_id,)).fetchall()
    order = {category: index for index, category in enumerate(STORABLE)}
    return sorted((_view(row, allowed) for row in rows), key=lambda item: order.get(item["category"], len(order)))


def _covers(scope: str, mode: str) -> bool:
    # A consent given for confirmed use covers every run the student starts and confirms. Only unattended runs need the
    # separate, later consent (M8); an entry made for those covers the confirmed modes too.
    return scope == "unattended" or (scope == "confirmed" and mode != "unattended")


def lookup(
    conn: sqlite3.Connection, user_id: str, *, category: str, question_key: str, company_key: str, mode: str, company_only: bool = False,
) -> dict[str, Any] | None:
    """The stored answer for this exact question and company (or "any company"), or None. Reads only.

    All must hold: a storable category, the same category, the same normalized question key, an entry for this company
    or for any company (this company's wins), a consent timestamp, and a consent scope that covers ``mode``. A statement
    that cites a document must be for this company, whatever the row says. ``company_only`` is the form's own word that
    its question depends on its company or points to a document: a row for any company is then never used, whatever it
    was saved as. The answer is returned to the plan only.
    """
    if category not in STORABLE:
        return None
    digest = hashlib.sha256(str(question_key).encode("utf-8")).hexdigest()
    rows = conn.execute(
        "SELECT id, category, question_text, answer_kind, answer, company_key, statement_links_json, consent_scope, consented_at "
        "FROM apply_sensitive_answers WHERE user_id=? AND question_hash=? AND category=? AND company_key IN ('', ?)",
        (user_id, digest, category, company_key),
    ).fetchall()
    usable = []
    for row in rows:
        mine = str(row["company_key"] or "")
        links = _loads_links(row["statement_links_json"])
        if not row["consented_at"] or not _covers(str(row["consent_scope"]), mode) or str(row["question_text"]) == "":
            continue
        if not mine and company_only:
            continue
        if category in STATEMENT_CATEGORIES and not mine and cites_document(str(row["question_text"]), links):
            continue
        usable.append((bool(mine), row, links))
    if not usable:
        return None
    _specific, row, links = max(usable, key=lambda item: item[0])
    return {
        "id": str(row["id"]), "answer_kind": str(row["answer_kind"]), "answer": str(row["answer"]), "links": links,
        "company_key": str(row["company_key"] or ""), "added": str(row["consented_at"])[:10],
    }
