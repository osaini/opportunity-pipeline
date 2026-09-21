"""Render approved preparation documents into attachable local PDF artifacts."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from .preparation import document_record
from .schema import utc_now


PDF_MEDIA_TYPE = "application/pdf"


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    return (cleaned[:80] or "application-document") + ".pdf"


def _plain_lines(markdown: str) -> list[str]:
    lines: list[str] = []
    for raw in markdown.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw.strip()
        if line.startswith("<!--") and line.endswith("-->"):
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^[-*+]\s+", "• ", line)
        line = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        lines.append(line)
    return lines


def _render_pdf(content: str, target: Path) -> None:
    try:
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:  # pragma: no cover - dependency failure is explicit
        raise RuntimeError("PDF export requires the pinned reportlab web dependency") from exc

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "ATSBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=13,
        alignment=TA_LEFT,
        spaceAfter=4,
    )
    heading = ParagraphStyle(
        "ATSHeading",
        parent=body,
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        spaceBefore=7,
        spaceAfter=3,
    )
    story: list[Any] = []
    for index, line in enumerate(_plain_lines(content)):
        if not line:
            story.append(Spacer(1, 5))
            continue
        escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        style = heading if index == 0 or (len(line) <= 60 and line.endswith(":")) else body
        story.append(Paragraph(escaped, style))
    document = SimpleDocTemplate(
        str(target),
        pagesize=letter,
        leftMargin=0.65 * inch,
        rightMargin=0.65 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        title="Approved application document",
        author="Opportunity Pipeline",
        invariant=1,
        pageCompression=1,
    )
    document.build(story)


def ensure_document_artifact(
    conn: sqlite3.Connection,
    document_id: str,
    storage_root: Path,
    *,
    user_id: str,
) -> dict[str, Any]:
    document = document_record(conn, document_id, user_id=user_id)
    if document["status"] != "approved":
        raise ValueError("Only approved documents can become attachable artifacts")
    existing = conn.execute(
        "SELECT * FROM generated_document_artifacts WHERE document_id=? AND user_id=?",
        (document_id, user_id),
    ).fetchone()
    if existing:
        path = (storage_root.resolve() / "generated" / str(existing["storage_path"])).resolve()
        if path.parent == (storage_root.resolve() / "generated").resolve() and path.exists():
            return dict(existing)

    artifact_root = (storage_root.resolve() / "generated").resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_id = f"document-artifact-{uuid4().hex}"
    filename = _safe_name(
        f"{document.get('company', '')}-{document.get('title', '')}-{document['document_type']}-v{document['version']}"
    )
    stored_name = f"{artifact_id}.pdf"
    target = (artifact_root / stored_name).resolve()
    if target.parent != artifact_root:
        raise ValueError("Invalid artifact storage path")
    with tempfile.NamedTemporaryFile(suffix=".pdf", dir=artifact_root, delete=False) as temporary:
        temp_path = Path(temporary.name)
    try:
        _render_pdf(str(document["content"]), temp_path)
        data = temp_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        temp_path.replace(target)
        timestamp = utc_now()
        with conn:
            conn.execute(
                "DELETE FROM generated_document_artifacts WHERE document_id=? AND user_id=?",
                (document_id, user_id),
            )
            conn.execute(
                """
                INSERT INTO generated_document_artifacts(
                    id, document_id, user_id, filename, media_type,
                    byte_size, sha256, storage_path, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    document_id,
                    user_id,
                    filename,
                    PDF_MEDIA_TYPE,
                    len(data),
                    digest,
                    stored_name,
                    timestamp,
                ),
            )
        return dict(
            conn.execute(
                "SELECT * FROM generated_document_artifacts WHERE id=? AND user_id=?",
                (artifact_id, user_id),
            ).fetchone()
        )
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        temp_path.unlink(missing_ok=True)


def delete_document_artifact(
    conn: sqlite3.Connection,
    document_id: str,
    storage_root: Path,
    *,
    user_id: str,
) -> None:
    row = conn.execute(
        "SELECT storage_path FROM generated_document_artifacts WHERE document_id=? AND user_id=?",
        (document_id, user_id),
    ).fetchone()
    if not row:
        return
    root = (storage_root.resolve() / "generated").resolve()
    path = (root / str(row["storage_path"])).resolve()
    with conn:
        conn.execute(
            "DELETE FROM generated_document_artifacts WHERE document_id=? AND user_id=?",
            (document_id, user_id),
        )
    if path.parent == root:
        path.unlink(missing_ok=True)
