"""Résumé variants: the student's own résumés for different kinds of roles, and which one each saved role gets.

A student keeps two or three résumés they designed themselves, one per kind of
role they apply to. Each is uploaded, then confirmed with "Use as a variant"
and given a label (resumes.confirm_variant). Their profile lists, per label,
the words that mark that kind of posting:

    "resume_variants": [{"label": "Hardware", "keywords": ["CAD", "PCB", "embedded"]}, ...],
    "default_variant": "Hardware"

When the resume_variant_pick switch is on and a role is saved (by the student
or by auto_save), pick_variant scores each variant by whole-word,
case-insensitive keyword hits: a keyword in the title counts 3, in the
description 1. The top variant wins only with at least 2 points and at least
1.5 times the runner-up; otherwise the pick is unsure and the default variant
is used, with that reason recorded. There is no model call.

The pick goes through the automation ledger (automation.ResumePick), so it
can be undone, and a pick the student makes (set_student_pick) always sticks.
With no variants set up, nothing is picked and every consumer falls back to
the confirmed résumé, as before.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any

from pipeline_core.visibility import capture_visible_sql

from . import automation
from .actions import OpportunityNotFoundError, intent_state
from .database import rollback_quietly
from .profile_store import read_stored_profile
from .timestamps import utc_now

LOGGER = logging.getLogger(__name__)

FEATURE = "resume_variant_pick"
TITLE_POINTS = 3
DESCRIPTION_POINTS = 1
MIN_POINTS = 2
MIN_MARGIN = 1.5


class ResumePickError(ValueError):
    """A pick the student asked for cannot be made; the message says why."""


def label_key(label: Any) -> str:
    """How labels are compared: trimmed and case-insensitive."""
    return " ".join(str(label or "").split()).casefold()


def configured_variants(profile: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """The variants the profile lists ({label, keywords}), cleaned, and the default variant's label.

    An entry without a label is ignored, keywords that are not text are
    dropped, and a label listed twice keeps its first entry.
    """
    variants: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw = profile.get("resume_variants")
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        label = " ".join(str(entry.get("label") or "").split())
        if not label or label_key(label) in seen:
            continue
        seen.add(label_key(label))
        keywords = entry.get("keywords")
        words = [" ".join(word.split()) for word in keywords if isinstance(word, str) and word.strip()] if isinstance(keywords, list) else []
        variants.append({"label": label, "keywords": words})
    default = profile.get("default_variant")
    return variants, " ".join(default.split()) if isinstance(default, str) else ""


def _pattern(keyword: str) -> re.Pattern[str]:
    # Whole words, but keywords such as "C++" or "Node.js" end in punctuation,
    # where \b would not match; a letter or digit on either side is what fails
    # a match, and so do "+" and "#", so "C" is not found in "C++" or "C#".
    body = r"\s+".join(re.escape(part) for part in keyword.split())
    return re.compile(rf"(?<![A-Za-z0-9+#]){body}(?![A-Za-z0-9+#])", re.IGNORECASE)


def mentions(text: str, term: str) -> bool:
    """Whether ``text`` names ``term`` as a whole word, ignoring case."""
    return bool(term.strip()) and bool(_pattern(term).search(text or ""))


def choose_variant(
    title: str, description: str, variants: list[dict[str, Any]], default_label: str = "",
) -> dict[str, Any]:
    """Pick a variant for a posting. Pure: the same posting and variants always give the same answer.

    Returns ``status`` ('picked', 'unsure', or 'no_variants'), ``label`` (the
    variant to use, or '' when there is none), ``matched`` (the winning
    variant's keywords the posting names), ``points`` per variant, and a plain
    ``reason``.
    """
    if not variants:
        return {"status": "no_variants", "label": "", "matched": [], "points": {}, "reason": "No résumé variants are set up"}
    scored = []
    for variant in variants:
        points, matched = 0, []
        for keyword in variant["keywords"]:
            hit = 0
            if mentions(title, keyword):
                hit += TITLE_POINTS
            if mentions(description, keyword):
                hit += DESCRIPTION_POINTS
            if hit:
                points += hit
                matched.append(keyword)
        scored.append((points, variant["label"], matched))
    points_by_label = {label: points for points, label, _matched in scored}
    ranked = sorted(scored, key=lambda item: -item[0])
    best_points, best_label, best_matched = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0
    if best_points >= MIN_POINTS and best_points >= MIN_MARGIN * runner_up:
        return {
            "status": "picked", "label": best_label, "matched": best_matched, "points": points_by_label,
            "reason": f"Matched {', '.join(best_matched)}",
        }
    if best_points < MIN_POINTS:
        why = "the posting names too few of any variant's words"
    else:
        why = f"{best_label} did not lead the next variant by enough"
    default = next((variant["label"] for variant in variants if label_key(variant["label"]) == label_key(default_label)), "")
    if default:
        fallback = ""
    elif default_label.strip():
        fallback = f"; your default variant ({default_label.strip()}) has no confirmed résumé"
    else:
        fallback = "; no default variant is set"
    return {
        "status": "unsure", "label": default, "matched": [], "points": points_by_label,
        "reason": f"Couldn't tell which variant fits: {why}{fallback}",
    }


def variant_files(conn: sqlite3.Connection, user_id: str) -> dict[str, dict[str, Any]]:
    """The confirmed résumés that carry a variant label, by label_key. The newest wins a shared label."""
    rows = conn.execute(
        """
        SELECT rf.id AS file_id, rv.id AS version_id, rf.variant_label, rf.original_name
        FROM resume_files rf JOIN resume_versions rv ON rv.resume_file_id=rf.id
        WHERE rf.user_id=? AND rv.status='confirmed' AND rf.variant_label<>''
        ORDER BY rf.created_at, rf.id
        """,
        (user_id,),
    ).fetchall()
    return {
        label_key(row["variant_label"]): {
            "resume_file_id": row["file_id"], "version_id": row["version_id"],
            "label": row["variant_label"], "original_name": row["original_name"],
        }
        for row in rows
    }


def variant_setup(conn: sqlite3.Connection, user_id: str) -> dict[str, list[str]]:
    """How far the student's variants are set up, for the Profile page and the switch.

    ``configured``: the labels the profile lists under resume_variants.
    ``usable``: those a confirmed résumé carries, which are the only ones ever picked.
    ``unlisted``: labels on confirmed résumés that the profile does not list, so they are never picked.
    """
    variants, _default = configured_variants(read_stored_profile(conn, user_id))
    files = variant_files(conn, user_id)
    listed = {label_key(variant["label"]) for variant in variants}
    return {
        "configured": [variant["label"] for variant in variants],
        "usable": [variant["label"] for variant in variants if label_key(variant["label"]) in files],
        "unlisted": sorted((entry["label"] for key, entry in files.items() if key not in listed), key=str.casefold),
    }


def setup_requirement(conn: sqlite3.Connection, user_id: str) -> str:
    """What resume_variant_pick needs before it can pick anything, or "" (automation.REQUIREMENTS)."""
    setup = variant_setup(conn, user_id)
    if setup["usable"]:
        return ""
    if not setup["configured"]:
        return "List your résumé variants and the words that mark each under resume_variants in your profile file first"
    return "None of the variants your profile lists has a confirmed résumé with its label yet. Label one with Use as a variant first"


def _posting(conn: sqlite3.Connection, opportunity_id: str, *, visible_to: str | None = None) -> Any:
    """The role's title and description. ``visible_to`` also requires the role be one that student may see."""
    if visible_to is None:
        row = conn.execute("SELECT id, title, description FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    else:
        row = conn.execute(
            f"SELECT o.id, o.title, o.description FROM opportunities o WHERE o.id=? AND {capture_visible_sql('o')}",
            (opportunity_id, visible_to),
        ).fetchone()
    if row is None:
        raise OpportunityNotFoundError(opportunity_id)
    return row


def pick_variant(conn: sqlite3.Connection, user_id: str, opportunity_id: str, *, visible_to: str | None = None) -> dict[str, Any]:
    """Which variant this role should get, from the profile's variants and the confirmed résumés that carry them.

    Only a variant with a confirmed résumé carrying its label is considered.
    Adds ``resume_file_id`` (None when there is nothing to use) to choose_variant's answer.
    ``visible_to`` reads the posting only when that student may see the role
    (OpportunityNotFoundError otherwise), as _posting does.
    """
    posting = _posting(conn, opportunity_id, visible_to=visible_to)
    variants, default = configured_variants(read_stored_profile(conn, user_id))
    files = variant_files(conn, user_id)
    usable = [variant for variant in variants if label_key(variant["label"]) in files]
    choice = choose_variant(posting["title"] or "", posting["description"] or "", usable, default)
    if variants and not usable:
        choice["reason"] = "None of your résumé variants has a confirmed résumé with its label yet"
    chosen = files.get(label_key(choice["label"])) if choice["label"] else None
    return {**choice, "resume_file_id": chosen["resume_file_id"] if chosen else None, "label": chosen["label"] if chosen else ""}


def _summary(posting: Any, choice: dict[str, Any]) -> str:
    title = posting["title"] or "this role"
    if choice["status"] == "picked":
        return f"Picked your {choice['label']} résumé for {title} (matched {', '.join(choice['matched'])})"
    return f"Using your default {choice['label']} résumé for {title}: couldn't tell which variant fits"


def pick_after_save(conn: sqlite3.Connection, user_id: str, opportunity_id: str) -> dict[str, Any] | None:
    """Pick the variant for a role that was just saved, through the ledger. None when nothing was picked.

    Runs after the save has committed, in its own transaction, so it can never
    undo or hold up the save. The switch must be on (and automation not
    paused); a pick the student made stays as it is.
    """
    if not automation.is_enabled(conn, user_id, FEATURE):
        return None
    try:
        # Only a role this student may see: another student's private capture is never read into their ledger.
        posting = _posting(conn, opportunity_id, visible_to=user_id)
        choice = pick_variant(conn, user_id, opportunity_id, visible_to=user_id)
    except OpportunityNotFoundError:
        return None
    if not choice["resume_file_id"]:
        return None
    latest = conn.execute(
        "SELECT id FROM opportunity_interactions WHERE opportunity_id=? AND user_id=? AND action='saved' ORDER BY id DESC LIMIT 1",
        (opportunity_id, user_id),
    ).fetchone()
    save_ref = str(latest["id"]) if latest else "none"
    matched = {"status": choice["status"], "matched": choice["matched"], "points": choice["points"], "reason": choice["reason"]}
    try:
        return automation.perform(
            conn, user_id=user_id, feature=FEATURE, action_type="resume.pick", subject_kind="opportunity",
            subject_id=opportunity_id,
            after={"resume_file_id": choice["resume_file_id"], "matched": {**matched, "label": choice["label"]}},
            evidence={"excerpt": choice["reason"], "title": posting["title"] or "", "label": choice["label"], **matched},
            summary=_summary(posting, choice),
            basis="keywords" if choice["status"] == "picked" else "default:unsure",
            confidence=None,
            # One pick per save: a role saved again later is picked again.
            idempotency_key=f"resume-pick:{opportunity_id}:{save_ref}",
            auto=True,
        )
    except automation.NotApplicable:
        return None


def safe_pick_after_save(conn: sqlite3.Connection, user_id: str, opportunity_id: str) -> None:
    """pick_after_save for the save path: a failure is logged and never reaches the save."""
    try:
        pick_after_save(conn, user_id, opportunity_id)
    except Exception:  # noqa: BLE001 - the save already committed; the pick is a convenience
        LOGGER.exception("The résumé variant for a saved role was not picked")
        rollback_quietly(conn, LOGGER, "a résumé pick failed")


def stored_pick(conn: sqlite3.Connection, user_id: str, opportunity_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT p.resume_file_id, p.picked_by, p.matched_json, p.updated_at, rf.variant_label, rf.original_name,
               (SELECT rv.id FROM resume_versions rv WHERE rv.resume_file_id=rf.id ORDER BY rv.created_at DESC LIMIT 1) AS version_id
        FROM opportunity_resume_picks p JOIN resume_files rf ON rf.id=p.resume_file_id
        WHERE p.user_id=? AND p.opportunity_id=?
        """,
        (user_id, opportunity_id),
    ).fetchone()
    return None if row is None else _pick_record(row)


def _pick_record(row: Any) -> dict[str, Any]:
    try:
        matched = json.loads(row["matched_json"] or "{}")
    except (TypeError, ValueError):
        matched = {}
    return {
        "resume_file_id": row["resume_file_id"],
        "version_id": row["version_id"],
        "label": row["variant_label"] or row["original_name"],
        "picked_by": row["picked_by"],
        "status": matched.get("status", "picked") if row["picked_by"] == "automatic" else "student",
        "matched": [str(word) for word in matched.get("matched") or []] if isinstance(matched, dict) else [],
        "reason": str(matched.get("reason") or "") if isinstance(matched, dict) else "",
        "updated_at": row["updated_at"],
    }


def picks_for(conn: sqlite3.Connection, opportunity_ids: list[str], *, user_id: str) -> dict[str, dict[str, Any]]:
    """The stored picks for one page of roles, in one query."""
    ids = [str(value) for value in opportunity_ids if value]
    if not ids:
        return {}
    rows = conn.execute(
        f"""
        SELECT p.opportunity_id, p.resume_file_id, p.picked_by, p.matched_json, p.updated_at, rf.variant_label, rf.original_name,
               NULL AS version_id
        FROM opportunity_resume_picks p JOIN resume_files rf ON rf.id=p.resume_file_id
        WHERE p.user_id=? AND p.opportunity_id IN ({', '.join('?' for _ in ids)})
        """,
        (user_id, *ids),
    ).fetchall()
    return {str(row["opportunity_id"]): _pick_record(row) for row in rows}


def resume_options(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    """Every confirmed résumé the student can pick for a role, variants first."""
    rows = conn.execute(
        """
        SELECT rf.id AS file_id, rv.id AS version_id, rf.variant_label, rf.original_name, rv.confirmed_at
        FROM resume_files rf JOIN resume_versions rv ON rv.resume_file_id=rf.id
        WHERE rf.user_id=? AND rv.status='confirmed'
        ORDER BY CASE WHEN rf.variant_label<>'' THEN 0 ELSE 1 END, rf.variant_label COLLATE NOCASE, rv.confirmed_at DESC, rf.id
        """,
        (user_id,),
    ).fetchall()
    return [
        {"resume_file_id": row["file_id"], "version_id": row["version_id"], "label": row["variant_label"] or "",
         "original_name": row["original_name"]}
        for row in rows
    ]


def pick_view(conn: sqlite3.Connection, user_id: str, opportunity_id: str) -> dict[str, Any]:
    """Everything the role's page shows: the pick in force, what the words suggest, and what can be picked."""
    _posting(conn, opportunity_id, visible_to=user_id)
    variants, default = configured_variants(read_stored_profile(conn, user_id))
    return {
        "opportunity_id": opportunity_id,
        "configured": bool(variants),
        "default_variant": default,
        "enabled": automation.mode(conn, user_id, FEATURE) == "on",
        # A pick is made only when a save changes the role, and never while paused (pick_after_save).
        "paused": automation.paused(conn, user_id),
        "saved": intent_state(conn, opportunity_id, user_id) == "saved",
        "pick": stored_pick(conn, user_id, opportunity_id),
        "suggestion": pick_variant(conn, user_id, opportunity_id, visible_to=user_id),
        "options": resume_options(conn, user_id),
    }


def set_student_pick(conn: sqlite3.Connection, user_id: str, opportunity_id: str, resume_file_id: str) -> dict[str, Any]:
    """The student's own pick for a role. It wins, and automation never overwrites it."""
    _posting(conn, opportunity_id, visible_to=user_id)
    owned = conn.execute(
        """
        SELECT 1 FROM resume_files rf JOIN resume_versions rv ON rv.resume_file_id=rf.id
        WHERE rf.id=? AND rf.user_id=? AND rv.status='confirmed'
        """,
        (resume_file_id, user_id),
    ).fetchone()
    if owned is None:
        raise ResumePickError("Pick one of your confirmed résumés")
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO opportunity_resume_picks(user_id, opportunity_id, resume_file_id, picked_by, matched_json, created_at, updated_at)
            VALUES(?, ?, ?, 'student', '{}', ?, ?)
            ON CONFLICT(user_id, opportunity_id) DO UPDATE SET
                resume_file_id=excluded.resume_file_id, picked_by='student', matched_json='{}', updated_at=excluded.updated_at
            """,
            (user_id, opportunity_id, resume_file_id, timestamp, timestamp),
        )
    return pick_view(conn, user_id, opportunity_id)


def preferred_resume_file(conn: sqlite3.Connection, user_id: str, opportunity_id: str | None) -> str | None:
    """The résumé file to put first for a role: the pick in force, or None (the confirmed résumé, as before)."""
    if not opportunity_id:
        return None
    pick = stored_pick(conn, user_id, opportunity_id)
    return pick["resume_file_id"] if pick else None


def resume_check(conn: sqlite3.Connection, user_id: str, opportunity_id: str) -> dict[str, Any]:
    """Skills the posting asks for that the student's confirmed profile lists but the picked résumé never mentions.

    Terms come only from the confirmed profile skills, so this can never
    suggest a skill the student has not confirmed. Informational only: nothing
    is edited. The résumé is the pick in force, else what the words suggest,
    else the most recently confirmed résumé.
    """
    from .preparation import confirmed_facts

    posting = _posting(conn, opportunity_id, visible_to=user_id)
    text = f"{posting['title'] or ''}\n{posting['description'] or ''}"
    skills = confirmed_facts(conn, user_id).get("skills")
    skills = [" ".join(skill.split()) for skill in skills if isinstance(skill, str) and skill.strip()] if isinstance(skills, list) else []
    file_id = preferred_resume_file(conn, user_id, opportunity_id) or pick_variant(conn, user_id, opportunity_id)["resume_file_id"]
    params: tuple[Any, ...]
    if file_id:
        where, params = "rf.id=? AND rf.user_id=?", (file_id, user_id)
    else:
        where, params = "rf.user_id=?", (user_id,)
    resume = conn.execute(
        f"""
        SELECT rf.id AS file_id, rf.variant_label, rf.original_name, rv.extracted_text
        FROM resume_files rf JOIN resume_versions rv ON rv.resume_file_id=rf.id
        WHERE {where} AND rv.status='confirmed'
        ORDER BY rv.confirmed_at DESC, rv.created_at DESC LIMIT 1
        """,
        params,
    ).fetchone()
    base = {"opportunity_id": opportunity_id, "confirmed_skills": len(skills), "terms": []}
    if resume is None:
        return {**base, "resume_file_id": None, "label": "", "note": "No confirmed résumé to check yet"}
    label = resume["variant_label"] or resume["original_name"]
    seen: set[str] = set()
    terms = []
    for skill in skills:
        if label_key(skill) in seen:
            continue
        seen.add(label_key(skill))
        if mentions(text, skill) and not mentions(resume["extracted_text"] or "", skill):
            terms.append(skill)
    note = "" if skills else "Your profile has no confirmed skills yet, so there is nothing to check"
    return {**base, "resume_file_id": resume["file_id"], "label": label, "terms": terms, "note": note}
