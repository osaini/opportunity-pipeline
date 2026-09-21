"""Evidence-grounded document, answer-library, and mock-interview workflows."""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agent_providers import AgentProvider
from .profile import is_answered
from .schema import utc_now


class PreparationNotFoundError(LookupError):
    pass


MAX_MOCK_AUDIO_BYTES = 15 * 1024 * 1024
DEFAULT_MOCK_AUDIO_STORAGE = Path(__file__).resolve().parent.parent / "data" / "private" / "mock-interviews"
MOCK_AUDIO_EXTENSIONS = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


def confirmed_facts(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT field_path, value_json, source FROM profile_facts WHERE user_id=? AND confirmed=1",
        (user_id,),
    ).fetchall()
    facts = {str(row["field_path"]): json.loads(row["value_json"]) for row in rows}
    return {field: value for field, value in facts.items() if is_answered(value)}


def _opportunity(conn: sqlite3.Connection, opportunity_id: str) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    if not row:
        raise PreparationNotFoundError(opportunity_id)
    return row


def _fact_line(field: str, value: Any, evidence: list[dict[str, Any]]) -> str:
    evidence.append({"profile_field": field, "value": value, "source": "confirmed_profile"})
    return str(value)


def _render_resume(facts: dict[str, Any], job: sqlite3.Row) -> tuple[str, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    lines: list[str] = []
    if facts.get("name"):
        lines.append(f"# {_fact_line('name', facts['name'], evidence)}")
    education = " | ".join(
        _fact_line(field, facts[field], evidence)
        for field in ("school", "degree", "graduation_year")
        if facts.get(field)
    )
    if education:
        lines.extend(("", education))
    summary = facts.get("summary")
    if summary:
        lines.extend(("", "## Summary", _fact_line("summary", summary, evidence)))
    skills = [str(skill) for skill in facts.get("skills", []) if str(skill).strip()]
    posting_text = f"{job['title']}\n{job['description']}".lower()
    matching = [skill for skill in skills if skill.lower() in posting_text]
    ordered_skills = [*matching, *[skill for skill in skills if skill not in matching]]
    if ordered_skills:
        lines.extend(("", "## Skills", _fact_line("skills", ", ".join(ordered_skills), evidence)))
    for field, heading in (("experience", "Experience"), ("projects", "Projects"), ("education", "Education"), ("awards", "Awards"), ("activities", "Activities")):
        entries = facts.get(field)
        if not entries:
            continue
        lines.extend(("", f"## {heading}"))
        values = entries if isinstance(entries, list) else [entries]
        for entry in values:
            if isinstance(entry, dict):
                text = " — ".join(
                    "; ".join(str(item) for item in value) if isinstance(value, list) else str(value)
                    for key, value in entry.items()
                    if value and key != "outreach"
                )
            else:
                text = str(entry)
            if text.strip():
                lines.append(f"- {_fact_line(field, text.strip(), evidence)}")
    lines.extend(("", f"<!-- Target: {job['title']} at {job['company']}; source posting is context, not a profile claim. -->"))
    return "\n".join(lines).strip() + "\n", evidence


def _render_cover_letter(facts: dict[str, Any], job: sqlite3.Row) -> tuple[str, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    name = _fact_line("name", facts["name"], evidence) if facts.get("name") else "Applicant"
    school = _fact_line("school", facts["school"], evidence) if facts.get("school") else "my school"
    degree = _fact_line("degree", facts["degree"], evidence) if facts.get("degree") else "my degree program"
    skills = [str(skill) for skill in facts.get("skills", []) if str(skill).lower() in f"{job['title']} {job['description']}".lower()]
    skill_sentence = ""
    if skills:
        _fact_line("skills", skills[:4], evidence)
        skill_sentence = f" My confirmed experience includes {', '.join(skills[:4])}, which I would be glad to discuss in relation to this role."
    content = (
        f"# Cover letter — {job['title']} at {job['company']}\n\n"
        f"Dear Hiring Team,\n\n"
        f"I am applying for the {job['title']} opportunity at {job['company']}. "
        f"I am pursuing {degree} at {school}.{skill_sentence}\n\n"
        "I would welcome the chance to learn more about the team and explain how my confirmed experience relates to the responsibilities in the original posting.\n\n"
        f"Sincerely,\n{name}\n"
    )
    return content, evidence


def create_document(
    conn: sqlite3.Connection,
    opportunity_id: str,
    document_type: str,
    *,
    user_id: str,
    provider: AgentProvider | None = None,
) -> dict[str, Any]:
    if document_type not in {"resume", "cover_letter"}:
        raise ValueError("document_type must be resume or cover_letter")
    facts = confirmed_facts(conn, user_id)
    if not facts:
        raise ValueError("Confirm profile facts before generating a document")
    job = _opportunity(conn, opportunity_id)
    content, evidence = (_render_resume if document_type == "resume" else _render_cover_letter)(facts, job)
    if provider is not None:
        grounded_facts = json.dumps(facts, ensure_ascii=False, sort_keys=True)
        job_context = json.dumps(
            {
                "company": job["company"],
                "title": job["title"],
                "location": job["location"],
                "description_excerpt": str(job["description"] or "")[:8_000],
            },
            ensure_ascii=False,
        )
        reply = provider.create(
            instructions=(
                f"Write one concise {document_type.replace('_', ' ')} in Markdown. "
                "Profile facts are the only source for claims about the applicant. "
                "The posting is context, not evidence about the applicant. Never invent experience, "
                "metrics, dates, credentials, or skills. Omit unsupported claims and do not include "
                "commentary, a preface, or a code fence."
            ),
            messages=[{
                "role": "user",
                "content": f"CONFIRMED PROFILE FACTS:\n{grounded_facts}\n\nJOB CONTEXT:\n{job_context}",
            }],
            tools=[],
            max_output_tokens=2_000,
        )
        if reply.tool_calls or not reply.text.strip():
            raise ValueError("The configured model did not return a usable document draft")
        content = reply.text.strip() + "\n"
        evidence = [
            {"profile_field": field, "value": value, "source": "confirmed_profile"}
            for field, value in facts.items()
        ]
    previous = conn.execute(
        """
        SELECT id, version FROM generated_documents
        WHERE user_id=? AND opportunity_id=? AND document_type=?
        ORDER BY version DESC LIMIT 1
        """,
        (user_id, opportunity_id, document_type),
    ).fetchone()
    version = int(previous["version"] + 1) if previous else 1
    document_id = f"document-{uuid4().hex}"
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO generated_documents(
                id, user_id, opportunity_id, document_type, version, parent_id,
                content, evidence_json, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (document_id, user_id, opportunity_id, document_type, version, previous["id"] if previous else None, content, json.dumps(evidence), timestamp, timestamp),
        )
    return document_record(conn, document_id, user_id=user_id)


def document_record(conn: sqlite3.Connection, document_id: str, *, user_id: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT d.*, o.company, o.title FROM generated_documents d
        LEFT JOIN opportunities o ON o.id=d.opportunity_id
        WHERE d.id=? AND d.user_id=?
        """,
        (document_id, user_id),
    ).fetchone()
    if not row:
        raise PreparationNotFoundError(document_id)
    result = dict(row)
    result["evidence"] = json.loads(row["evidence_json"] or "[]")
    result.pop("evidence_json", None)
    parent_content = ""
    if row["parent_id"]:
        parent = conn.execute("SELECT content FROM generated_documents WHERE id=?", (row["parent_id"],)).fetchone()
        parent_content = str(parent[0]) if parent else ""
    result["diff"] = "".join(
        difflib.unified_diff(
            parent_content.splitlines(keepends=True),
            str(row["content"]).splitlines(keepends=True),
            fromfile="previous",
            tofile=f"version-{row['version']}",
        )
    )
    return result


def list_documents(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    ids = [row[0] for row in conn.execute("SELECT id FROM generated_documents WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()]
    return [document_record(conn, str(document_id), user_id=user_id) for document_id in ids]


def edit_document(
    conn: sqlite3.Connection,
    document_id: str,
    content: str,
    evidence_fields: list[str],
    *,
    user_id: str,
) -> dict[str, Any]:
    existing = document_record(conn, document_id, user_id=user_id)
    facts = confirmed_facts(conn, user_id)
    unknown = sorted(set(evidence_fields) - set(facts))
    if unknown:
        raise ValueError(f"Evidence fields are not confirmed: {', '.join(unknown)}")
    if not content.strip() or not evidence_fields:
        raise ValueError("Edited content requires at least one confirmed evidence field")
    timestamp = utc_now()
    evidence = [{"profile_field": field, "value": facts[field], "source": "confirmed_profile"} for field in evidence_fields]
    with conn:
        conn.execute(
            "UPDATE generated_documents SET content=?, evidence_json=?, status='draft', approved_at=NULL, updated_at=? WHERE id=? AND user_id=?",
            (content, json.dumps(evidence), timestamp, document_id, user_id),
        )
    return document_record(conn, document_id, user_id=user_id)


def approve_document(conn: sqlite3.Connection, document_id: str, *, user_id: str) -> dict[str, Any]:
    existing = document_record(conn, document_id, user_id=user_id)
    if not existing["evidence"]:
        raise ValueError("A document without confirmed evidence cannot be approved")
    timestamp = utc_now()
    with conn:
        conn.execute(
            "UPDATE generated_documents SET status='approved', approved_at=?, updated_at=? WHERE id=? AND user_id=?",
            (timestamp, timestamp, document_id, user_id),
        )
    return document_record(conn, document_id, user_id=user_id)


def list_answers(conn: sqlite3.Connection, query: str = "", *, user_id: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    params: list[Any] = [user_id]
    where = "WHERE user_id=?"
    if query.strip():
        where += " AND (question LIKE ? OR answer LIKE ? OR company LIKE ? OR tags_json LIKE ?)"
        term = f"%{query.strip()}%"
        params.extend([term, term, term, term])
    rows = conn.execute(f"SELECT * FROM answer_library {where} ORDER BY updated_at DESC", params).fetchall()
    return [{**dict(row), "tags": json.loads(row["tags_json"] or "[]")} for row in rows]


def save_answer(
    conn: sqlite3.Connection,
    question: str,
    answer: str,
    company: str = "",
    tags: list[str] | None = None,
    *,
    answer_id: str | None = None,
    user_id: str,
) -> dict[str, Any]:
    if not question.strip() or not answer.strip():
        raise ValueError("Question and answer are required")
    timestamp = utc_now()
    resolved_id = answer_id or f"answer-{uuid4().hex}"
    with conn:
        if answer_id:
            cursor = conn.execute(
                """
                UPDATE answer_library SET question=?, answer=?, company=?, tags_json=?, updated_at=?
                WHERE id=? AND user_id=?
                """,
                (question.strip(), answer.strip(), company.strip(), json.dumps(tags or []), timestamp, resolved_id, user_id),
            )
            if not cursor.rowcount:
                raise PreparationNotFoundError(resolved_id)
        else:
            conn.execute(
                """
                INSERT INTO answer_library(id, user_id, question, answer, company, tags_json, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (resolved_id, user_id, question.strip(), answer.strip(), company.strip(), json.dumps(tags or []), timestamp, timestamp),
            )
    row = conn.execute("SELECT * FROM answer_library WHERE id=? AND user_id=?", (resolved_id, user_id)).fetchone()
    return {**dict(row), "tags": json.loads(row["tags_json"] or "[]")}


def delete_answer(conn: sqlite3.Connection, answer_id: str | None = None, *, user_id: str) -> int:
    with conn:
        if answer_id:
            cursor = conn.execute("DELETE FROM answer_library WHERE id=? AND user_id=?", (answer_id, user_id))
        else:
            cursor = conn.execute("DELETE FROM answer_library WHERE user_id=?", (user_id,))
    return int(cursor.rowcount)


def create_mock_interview(conn: sqlite3.Connection, opportunity_id: str, *, user_id: str) -> dict[str, Any]:
    job = _opportunity(conn, opportunity_id)
    interview_id = f"interview-{uuid4().hex}"
    timestamp = utc_now()
    prompts = [
        f"Tell me about a confirmed experience that is relevant to the {job['title']} role.",
        f"Describe a time you solved a difficult technical problem and what you learned.",
        f"Why are you interested in {job['company']} and this role? Separate what you know from what you still need to verify.",
    ]
    rubric = {"dimensions": ["specificity", "structure", "relevance", "reflection"], "max_score": 100}
    with conn:
        conn.execute(
            "INSERT INTO mock_interviews(id, user_id, opportunity_id, status, created_at) VALUES(?, ?, ?, 'active', ?)",
            (interview_id, user_id, opportunity_id, timestamp),
        )
        for position, prompt in enumerate(prompts, start=1):
            conn.execute(
                "INSERT INTO mock_questions(id, interview_id, prompt, rubric_json, position) VALUES(?, ?, ?, ?, ?)",
                (f"question-{uuid4().hex}", interview_id, prompt, json.dumps(rubric), position),
            )
    return interview_record(conn, interview_id, user_id=user_id)


def list_interviews(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    """Every mock interview, newest first, with how much was answered and recorded."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT i.id, i.created_at, o.company, o.title,
               COUNT(a.id) AS answers,
               COALESCE(SUM(CASE WHEN a.audio_path IS NOT NULL AND a.audio_path <> '' THEN 1 ELSE 0 END), 0) AS recordings,
               MAX(a.created_at) AS last_answered_at
        FROM mock_interviews i
        JOIN opportunities o ON o.id=i.opportunity_id
        LEFT JOIN mock_questions q ON q.interview_id=i.id
        LEFT JOIN mock_answers a ON a.question_id=q.id AND a.user_id=i.user_id
        WHERE i.user_id=?
        GROUP BY i.id, i.created_at, o.company, o.title
        ORDER BY i.created_at DESC
        """,
        (user_id,),
    ).fetchall()
    return [{**dict(row), "answers": int(row["answers"]), "recordings": int(row["recordings"])} for row in rows]


def interview_record(conn: sqlite3.Connection, interview_id: str, *, user_id: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    interview = conn.execute(
        """
        SELECT i.*, o.company, o.title FROM mock_interviews i
        JOIN opportunities o ON o.id=i.opportunity_id WHERE i.id=? AND i.user_id=?
        """,
        (interview_id, user_id),
    ).fetchone()
    if not interview:
        raise PreparationNotFoundError(interview_id)
    questions = conn.execute(
        "SELECT * FROM mock_questions WHERE interview_id=? ORDER BY position",
        (interview_id,),
    ).fetchall()
    result = dict(interview)
    result["questions"] = []
    for question in questions:
        answers = conn.execute(
            "SELECT * FROM mock_answers WHERE question_id=? AND user_id=? ORDER BY created_at DESC",
            (question["id"], user_id),
        ).fetchall()
        result["questions"].append({
            **dict(question),
            "rubric": json.loads(question["rubric_json"]),
            "answers": [
                {
                    **{key: value for key, value in dict(answer).items() if key != "audio_path"},
                    "has_audio": bool(answer["audio_path"]),
                    "feedback": json.loads(answer["feedback_json"]),
                }
                for answer in answers
            ],
        })
        result["questions"][-1].pop("rubric_json", None)
    return result


def answer_mock_question(
    conn: sqlite3.Connection,
    question_id: str,
    answer_text: str,
    transcript: str = "",
    audio_path: str = "",
    *,
    user_id: str,
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    question = conn.execute(
        """
        SELECT q.*, i.user_id, o.title, o.description FROM mock_questions q
        JOIN mock_interviews i ON i.id=q.interview_id
        JOIN opportunities o ON o.id=i.opportunity_id
        WHERE q.id=? AND i.user_id=?
        """,
        (question_id, user_id),
    ).fetchone()
    if not question:
        raise PreparationNotFoundError(question_id)
    content = (transcript or answer_text).strip()
    if not content:
        raise ValueError("Type an answer or provide a reviewed transcript")
    words = re.findall(r"[A-Za-z0-9']+", content)
    lower = content.lower()
    structure_hits = sum(token in lower for token in ("situation", "task", "action", "result", "learned"))
    job_terms = {term.lower() for term in re.findall(r"\b[A-Za-z][A-Za-z+#.-]{3,}\b", f"{question['title']} {question['description']}")}
    relevance_hits = len({word.lower() for word in words} & job_terms)
    score = min(100, 25 + min(len(words), 120) // 3 + structure_hits * 7 + min(relevance_hits, 8) * 3)
    feedback: list[str] = []
    if len(words) < 60:
        feedback.append("Add a concrete example with enough context to understand your contribution.")
    if structure_hits < 2:
        feedback.append("Make the situation, your action, and the result easier to identify.")
    if relevance_hits < 2:
        feedback.append("Connect the example more directly to the role while staying within confirmed facts.")
    if not feedback:
        feedback.append("The answer is specific and structured; tighten any repetition before using it live.")
    answer_id = f"mock-answer-{uuid4().hex}"
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO mock_answers(
                id, question_id, user_id, answer_text, transcript, audio_path,
                score, feedback_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (answer_id, question_id, user_id, answer_text, transcript, audio_path, score, json.dumps(feedback), timestamp),
        )
    row = conn.execute("SELECT * FROM mock_answers WHERE id=?", (answer_id,)).fetchone()
    result = {**dict(row), "feedback": feedback, "has_audio": bool(row["audio_path"])}
    result.pop("audio_path", None)
    return result


def store_recorded_mock_answer(
    conn: sqlite3.Connection,
    question_id: str,
    answer_text: str,
    transcript: str,
    audio_data: bytes,
    media_type: str,
    storage_root: Path,
    *,
    user_id: str,
) -> dict[str, Any]:
    """Persist a reviewed recording privately, then score its reviewed text."""

    if not audio_data:
        raise ValueError("The recording is empty")
    if len(audio_data) > MAX_MOCK_AUDIO_BYTES:
        raise ValueError("Mock-interview recordings are limited to 15 MB")
    normalized_type = media_type.split(";", 1)[0].strip().lower()
    extension = MOCK_AUDIO_EXTENSIONS.get(normalized_type)
    if not extension:
        raise ValueError("Unsupported recording type")
    signatures_ok = {
        "audio/webm": audio_data.startswith(b"\x1aE\xdf\xa3"),
        "audio/ogg": audio_data.startswith(b"OggS"),
        "audio/mp4": len(audio_data) >= 12 and audio_data[4:8] == b"ftyp",
        "audio/wav": audio_data.startswith(b"RIFF") and audio_data[8:12] == b"WAVE",
        "audio/x-wav": audio_data.startswith(b"RIFF") and audio_data[8:12] == b"WAVE",
    }
    if not signatures_ok[normalized_type]:
        raise ValueError("Recording content does not match its media type")
    storage_root = storage_root.expanduser().resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    stored_name = f"mock-audio-{uuid4().hex}{extension}"
    stored_path = (storage_root / stored_name).resolve()
    if stored_path.parent != storage_root:
        raise ValueError("Invalid recording storage path")
    stored_path.write_bytes(audio_data)
    try:
        return answer_mock_question(
            conn,
            question_id,
            answer_text,
            transcript,
            stored_name,
            user_id=user_id,
        )
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise


def recorded_mock_answer_path(
    conn: sqlite3.Connection,
    answer_id: str,
    storage_root: Path,
    *,
    user_id: str,
) -> tuple[Path, str]:
    row = conn.execute(
        "SELECT audio_path FROM mock_answers WHERE id=? AND user_id=?",
        (answer_id, user_id),
    ).fetchone()
    if not row or not row["audio_path"]:
        raise PreparationNotFoundError(answer_id)
    root = storage_root.expanduser().resolve()
    path = (root / str(row["audio_path"])).resolve()
    if path.parent != root or not path.is_file():
        raise PreparationNotFoundError(answer_id)
    media_type = {
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
    }.get(path.suffix.lower(), "application/octet-stream")
    return path, media_type
