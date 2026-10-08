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
- every statement (an acknowledgment or a consent) is saved for one company, never for any company, and so is every tick
  box and every typed answer for work authorization, sponsorship or 18 or older: no list of words can prove a statement
  names no document, so nothing of the kind travels. Only a select's exact option label for those three kinds, and an EEO
  decline, may be kept for any company. So is any question that depends on its company (7.1);
- an entry for work authorization, sponsorship or 18 or older whose statement or answer also claims a demographic, a
  criminal record, pay or a clearance (the broad net's never-storable topics) is refused, so this table never holds a
  ticked veteran, gender, ethnicity or probation claim;

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

from pipeline_core.identity import employer_key, normalized_text
from pipeline_core.visibility import capture_visible_sql

from .checks import question_key
from .classify import (
    NEVER_STORABLE_TOPICS,
    RESTRICTION,
    STATEMENT_CATEGORIES,
    TICKABLE,
    classify_sensitive,
    context_dependent,
    eeo_words,
    net_topics,
)
from ..core.settings_store import get_setting, put_setting
from ..core.timestamps import utc_now

__all__ = [
    "CATEGORY_GROUPS", "CONSENT_TEXT", "DECLINE_EXAMPLES", "EEO_CATEGORIES", "LABELS", "STORABLE", "StoreRefused", "add_entry", "allowed_categories",
    "PLACEHOLDER_NOTE", "cites_document", "delete_entry", "is_decline", "links_in", "list_entries", "lookup", "set_allowed_categories",
]

SETTING_KEY = "apply_sensitive_categories"
MAX_ENTRIES = 500
MAX_QUESTION_CHARS = 4000
MAX_ANSWER_CHARS = 500
MAX_LINKS = 8
MAX_LINK_CHARS = 500
MAX_COMPANY_CHARS = 200

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
        # Lever's veteran question words its decline this way (docs/phase5-lever-handoff-spec.md 6.6). The whole label, as the rest.
        "decline to self identify for protected veteran status",
    )
)


def is_decline(text: Any) -> bool:
    """Whether an answer is a decline to answer an EEO question, by its whole label. Case and punctuation do not matter."""
    return normalized_text(text) in _DECLINE_LABELS


# --- Links a statement cites -------------------------------------------------------------------------------

_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_BARE_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
# A statement that reads or agrees to a document depends on which document that is. Leaning wide is the safe way: it makes
# the entry company-specific, and the student is asked once more for a new company. Any statement that plainly names no
# document ("I certify that the information I have provided is accurate") may be kept for any company.
_DOCUMENT_WORDS = re.compile(
    r"\bhave read\b|\bi ve read\b|\breviewed\b|\bread and (?:understood?|agree|accept)|acknowledge receipt|\breceipt of\b|\bprivacy\b"
    r"|\bterms\b|\bpolic(?:y|ies)\b|\bnotice\b|\bhandbook\b|\bagreement\b|\bstatement\b|\bcode of (?:business )?conduct\b"
    r"|\bprogram\b|\bdisclosures?\b|\bguidelines?\b|\bstandards\b|\baddendum\b|\bcontract\b|\bcharter\b|https?://"
)
# A capitalized name after "the", "our" or "its" ("the Candidate Data Protection Statement") is a document by its own
# name, whatever word it ends in. Read on the text as written, since the normalized text has lost its capitals.
_NAMED_DOCUMENT = re.compile(r"\b(?i:the|our|its|their)\s+(?:[A-Z][\w'’&.-]*\s+)+[A-Z][\w'’&.-]*")


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


# The only words a statement may be made of for the app to be sure it names no document: a certification that what the student
# wrote on this application is true. Every other agreement (arbitration "rules", a "code", a "principles" page, a "poster", a
# retention period at one employer) may be that employer's own text, and a list of document nouns can never be complete.
_ACCURACY_WORDS = frozenset(
    "i we certify certification attest affirm declare declaration confirm acknowledge that the my all of this these those information "
    "answers responses details data provided given submitted made entered in on to application form and best knowledge are is was were "
    "true accurate correct complete truthful not no false misleading or a an above here it they be have has been am will".split()
)
_ACCURACY_CLAIM = re.compile(r"\b(?:true|accurate|correct|complete|truthful)\b")


def _names_no_document(statement: str) -> bool:
    """Whether a statement is provably a certification that the student's own answers are true, and nothing else."""
    words = normalized_text(statement).split()
    return bool(words) and bool(_ACCURACY_CLAIM.search(" ".join(words))) and all(word in _ACCURACY_WORDS for word in words)


def cites_document(statement: str, links: Iterable[str] = (), *, names: bool = True) -> bool:
    """Whether a statement reads, agrees to or links a document, so it is only ever saved for one company.

    No longer what decides scope: every statement and every tick box is kept for one company (``_any_company_choice``), because no
    list can prove a statement names no document. It stays as the finer reading, for callers and tests that ask.

    ``names`` is on for a legal acknowledgment or a data consent, whose whole point is the text it agrees to, and off for a box
    that states a fact about the student ("I am authorized to work in the United States"), which names a place, not a document.
    It reads a capitalized name after "the", "our" or "its" as a document, and fails closed on any agreement the broad net
    finds (apply_classify.NET_TOPICS): such a statement names no document only when the app can prove it, which is a plain
    certification that the student's answers are true. The word lists above catch the common documents; the rule is that a
    list of nouns can never be complete, so anything that agrees to something and is not provably plain is kept for one company.
    """
    if bool(tuple(links)) or bool(_DOCUMENT_WORDS.search(normalized_text(statement))) or bool(links_in(statement)):
        return True
    if not names:
        return False
    if _NAMED_DOCUMENT.search(html.unescape(str(statement or ""))):
        return True
    return "agreement" in net_topics(statement) and not _names_no_document(statement)


# --- What the student allowed ---------------------------------------------------------------------------------

def allowed_categories(conn: sqlite3.Connection, user_id: str) -> frozenset[str]:
    """The categories the student switched on (D5). Empty by default, and never more than the storable ones."""
    stored = get_setting(conn, user_id, SETTING_KEY)
    parts = stored.split(",") if stored else []
    return frozenset(part.strip() for part in parts if part.strip() in STORABLE)


def set_allowed_categories(conn: sqlite3.Connection, user_id: str, categories: Iterable[str], *, now: datetime | None = None) -> list[str]:
    """Replace the switched-on categories. Anything outside the storable set is refused, with the reason for the never ones."""
    wanted = {str(category).strip() for category in categories if str(category).strip()}
    for category in sorted(wanted):
        if category not in STORABLE:
            raise StoreRefused(_NEVER.get(category, f"Unknown kind of answer: {category}"))
    ordered = [category for category in STORABLE if category in wanted]
    with conn:
        put_setting(conn, user_id, SETTING_KEY, ",".join(ordered), _now(now))
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
    employer's name: empty means any company, which every statement, every tick box, every typed answer and a follow-up that
    depends on its company (``company_only``, or wording that says so) are refused. ``consent`` must be True: the student ticked the box in
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
    # under a more permissive name. The classifier is the plan's own (apply_classify).
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
        if len(normalized_text(text).split()) < 3:
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
    if category in TICKABLE:
        # A wording that asks for voluntary self-identification as well as work authorization, sponsorship or age is never filed
        # under the other kind, whichever kind reads it most strictly: a demographic value must not get in under it (D5 C (i)).
        # A ticked statement is no exception: "I am authorized to work in the United States and I am a protected veteran" would
        # tick a demographic claim at every employer. The broad net is read as well as the classifier's own words, so a wording the
        # lists miss ("military spouse", "date of birth") is refused too, and so is a claim about a criminal record, pay or
        # clearance ("...and I am not on probation"): those topics are never storable (D5 C (i)). An 18-or-older wording is not one.
        # An acknowledgment or a consent may name such words ("EEOC Know Your Rights poster"): it is an agreement, saved for one company.
        claimed = set(net_topics(text)) & set(NEVER_STORABLE_TOPICS)
        if eeo_words(text) or "demographic" in claimed:
            raise StoreRefused("This question also asks for voluntary self-identification. The app stores only a decline for those, so it cannot store an answer to it")
        if claimed:
            raise StoreRefused("This question also asks about criminal history, pay or security clearance. The app never stores an answer to those, so it cannot store an answer to it")
        # The answer too, not only the question: an option such as "Yes, and I am a protected veteran" is a demographic value.
        if not ticked and (eeo_words(stored) or set(net_topics(stored)) & set(NEVER_STORABLE_TOPICS)):
            raise StoreRefused("That answer says more than work authorization, sponsorship or age. The app never stores a criminal, demographic, pay or clearance claim")
    if category in EEO_CATEGORIES:
        # D5 C (i): this table never holds a demographic value. Checked here, so no route and no future caller can bypass it.
        if kind != "option" or not is_decline(stored):
            raise StoreRefused("The app stores only a decline answer (such as \"Decline To Self Identify\") for these questions, never a real answer")
    mine = employer_key(company) if str(company or "").strip() else ""
    if str(company or "").strip() and not mine:
        raise StoreRefused("This role has no company name the app can match on")
    if not mine and company_only:
        raise StoreRefused("This question depends on the company, so its answer is saved for this company only")
    # Whatever the caller says: an EEO decline holds nothing about a company, anything else that reads as depending on its
    # employer ("this company", a follow-up, a bare heading) is never kept for every company (7.1).
    if not mine and category not in EEO_CATEGORIES and _depends_on_company(key):
        raise StoreRefused("This question depends on the company, so its answer is saved for this company only")
    if not mine and (statement or ticked or (category in TICKABLE and kind != "option")):
        # No list of words can prove a statement names no document, and a tick box or a typed answer is not an exact option label:
        # every statement and every tick box is kept for the one company it was read at (D9 B; spec 5.4 "As built").
        raise StoreRefused("A statement, a tick box or a typed answer is saved for one company only, never for any company")
    if not mine and not _any_company_choice(category, kind, text, stored):
        # A choice that also agrees to something ("Yes, and I agree to E-Verify") is an agreement, and an agreement is one company's.
        raise StoreRefused("This question or answer also agrees to something, so it is saved for one company only, never for any company")
    # The name is shown back as typed (or as the role names it); the key is only for matching.
    shown = _one_line(company, MAX_COMPANY_CHARS, "company") if mine else ""
    stamp = _now(now)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    with conn:
        if conn.execute("SELECT 1 FROM apply_sensitive_answers WHERE user_id=? AND question_hash=? AND company_key=?", (user_id, digest, mine)).fetchone() is None:
            if conn.execute("SELECT COUNT(*) FROM apply_sensitive_answers WHERE user_id=?", (user_id,)).fetchone()[0] >= MAX_ENTRIES:
                raise StoreRefused("There are too many stored answers. Remove some first")
        conn.execute(
            "INSERT INTO apply_sensitive_answers(id, user_id, category, question_text, question_key, question_hash, answer_kind, answer, company_key, "
            "company_name, statement_links_json, consent_scope, consented_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?) "
            "ON CONFLICT(user_id, question_hash, company_key) DO UPDATE SET category=excluded.category, question_text=excluded.question_text, "
            "question_key=excluded.question_key, answer_kind=excluded.answer_kind, answer=excluded.answer, company_name=excluded.company_name, "
            "statement_links_json=excluded.statement_links_json, consent_scope='confirmed', consented_at=excluded.consented_at, updated_at=excluded.updated_at",
            (f"sens-{uuid4().hex}", user_id, category, text, key, digest, kind, stored, mine, shown, json.dumps(list(cited)), stamp, stamp, stamp),
        )
        row = conn.execute(
            "SELECT * FROM apply_sensitive_answers WHERE user_id=? AND question_hash=? AND company_key=?", (user_id, digest, mine),
        ).fetchone()
    return _view(row, allowed_categories(conn, user_id), _role_counts(conn, user_id) if mine else {})


def _stricter(read_as: str, category: str) -> bool:
    """Whether a wording's reading is a more restrictive kind than the one it is filed under (the plan's own order)."""
    return RESTRICTION.index(read_as) < RESTRICTION.index(category)


def _depends_on_company(key: str) -> bool:
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


def _role_counts(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    """How many of the student's roles each employer key names, so an entry can say whether it matches any."""
    counts: dict[str, int] = {}
    for row in conn.execute(
        f"SELECT o.company, COUNT(*) AS roles FROM opportunities o WHERE {capture_visible_sql('o')} GROUP BY o.company", (user_id,),
    ).fetchall():
        key = employer_key(str(row["company"] or ""))
        if key:
            counts[key] = counts.get(key, 0) + int(row["roles"])
    return counts


def _view(row: Any, allowed: frozenset[str], roles: dict[str, int]) -> dict[str, Any]:
    category = str(row["category"])
    mine = str(row["company_key"] or "")
    return {
        "id": str(row["id"]), "category": category, "words": _WORDS.get(category, ""), "question": str(row["question_text"]),
        # The name as typed, never the key rebuilt into words the student did not write. A row saved before the name was kept shows its key as it is.
        "answer": str(row["answer"]), "answer_kind": str(row["answer_kind"]), "company": str(row["company_name"] or "") or mine, "any_company": not mine,
        # How many of the student's roles the company names: 0 says the entry will not be used on any role in their list.
        "matched_roles": roles.get(mine, 0) if mine else None,
        "links": _loads_links(row["statement_links_json"]), "consent_scope": str(row["consent_scope"]), "consented_at": str(row["consented_at"]),
        "last_used_at": str(row["last_used_at"] or ""), "switched_on": category in allowed,
    }


def list_entries(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    """The student's own entries for the settings page. Answers are shown to their owner and go nowhere else."""
    allowed = allowed_categories(conn, user_id)
    rows = conn.execute("SELECT * FROM apply_sensitive_answers WHERE user_id=? ORDER BY category, created_at, id", (user_id,)).fetchall()
    roles = _role_counts(conn, user_id) if any(row["company_key"] for row in rows) else {}
    order = {category: index for index, category in enumerate(STORABLE)}
    return sorted((_view(row, allowed, roles) for row in rows), key=lambda item: order.get(item["category"], len(order)))


def _covers(scope: str, mode: str) -> bool:
    # A consent given for confirmed use covers every run the student starts and confirms. Only unattended runs need the
    # separate, later consent (M8); an entry made for those covers the confirmed modes too.
    return scope == "unattended" or (scope == "confirmed" and mode != "unattended")


def _any_company_choice(category: str, answer_kind: str, question_text: str, answer: str) -> bool:
    """Whether an entry may be kept, and used, for any company.

    Only an EEO decline and a select's exact option label for work authorization, sponsorship or 18 or older are: a statement, a
    tick box and a typed answer never are (no list of words can prove a statement names no document), and neither is a choice
    whose question or option agrees to something ("Yes, and I agree to complete E-Verify"), which is an agreement whatever
    kind it is filed under.
    """
    if category in EEO_CATEGORIES:
        return True
    if category not in TICKABLE or answer_kind != "option":
        return False
    return "agreement" not in net_topics(f"{question_text} {answer}")


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
        if not mine and not _any_company_choice(category, str(row["answer_kind"]), str(row["question_text"]), str(row["answer"])):
            # A statement, a tick box, a typed answer or a choice that agrees to something is only ever used at the company it was
            # saved for, whatever the row says.
            continue
        usable.append((bool(mine), row, links))
    if not usable:
        return None
    _specific, row, links = max(usable, key=lambda item: item[0])
    return {
        "id": str(row["id"]), "answer_kind": str(row["answer_kind"]), "answer": str(row["answer"]), "links": links,
        "company_key": str(row["company_key"] or ""), "added": str(row["consented_at"])[:10],
    }
