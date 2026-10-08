"""Render approved preparation documents into attachable local PDF artifacts."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

from .preparation import document_record
from ..core.timestamps import utc_now


PDF_MEDIA_TYPE = "application/pdf"


def content_digest(content: str) -> str:
    """The SHA-256 of a document's text: what ``content_sha256`` records and what Apply for me puts in its plan."""
    return hashlib.sha256(str(content).encode("utf-8")).hexdigest()


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


def document_file_name(document: dict[str, Any]) -> str:
    """The name the employer sees for a document's PDF (company, role, kind and version), never a storage name."""
    return _safe_name(f"{document.get('company', '')}-{document.get('title', '')}-{document['document_type']}-v{document['version']}")


def _stored_file(storage_root: Path, artifact: Any) -> Path | None:
    """The artifact's file inside the generated folder, or None when it is missing or outside it."""
    root = (storage_root.resolve() / "generated").resolve()
    path = (root / str(artifact["storage_path"])).resolve()
    return path if path.parent == root and path.is_file() else None


def _is_current(artifact: Any, content: str, storage_root: Path) -> bool:
    """The stored PDF was rendered from this exact text, and the file on disk is still the one that was stored.

    An artifact made before ``content_sha256`` existed has none recorded, so it cannot be shown to match and is made again.
    """
    if str(artifact["content_sha256"] or "") != content_digest(content):
        return False
    path = _stored_file(storage_root, artifact)
    return path is not None and hashlib.sha256(path.read_bytes()).hexdigest() == str(artifact["sha256"])


def ensure_document_artifact(
    conn: sqlite3.Connection,
    document_id: str,
    storage_root: Path,
    *,
    user_id: str,
) -> dict[str, Any]:
    """The PDF of an approved document, rendered from the text as it is now.

    A stored PDF is returned only when it was rendered from this exact text (its recorded ``content_sha256``) and the file
    still matches its own hash. Otherwise it is rendered again and the old file removed, so a PDF of an older draft is never
    returned, whatever happened to the delete that was meant to remove it (the edit route commits first and deletes after).
    """
    document = document_record(conn, document_id, user_id=user_id)
    if document["status"] != "approved":
        raise ValueError("Only approved documents can become attachable artifacts")
    existing = conn.execute(
        "SELECT * FROM generated_document_artifacts WHERE document_id=? AND user_id=?",
        (document_id, user_id),
    ).fetchone()
    if existing and _is_current(existing, str(document["content"]), storage_root):
        return dict(existing)

    artifact_root = (storage_root.resolve() / "generated").resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_id = f"document-artifact-{uuid4().hex}"
    filename = document_file_name(document)
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
                    byte_size, sha256, storage_path, created_at, content_sha256
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    content_digest(str(document["content"])),
                ),
            )
        if existing:
            stale = _stored_file(storage_root, existing)
            if stale is not None and stale != target:
                try:
                    stale.unlink(missing_ok=True)
                except OSError:
                    pass   # the new artifact is committed; an old file left behind is only clutter, and never returned
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


class AttachableFile(NamedTuple):
    """An approved document's PDF read for attaching: the name the employer sees, its bytes, and the hashes that identify it."""

    name: str
    media_type: str
    data: bytes
    sha256: str            # of the bytes
    content_sha256: str    # of the approved text the bytes were rendered from


def attachable_file(conn: sqlite3.Connection, document_id: str, storage_root: Path, *, user_id: str) -> AttachableFile:
    """The PDF of an approved document, current and read now. ValueError when the document is not approved or its file cannot be read.

    Renders the PDF when there is none that matches the text (``ensure_document_artifact``), so the bytes returned always come from the
    text the document has at this moment.
    """
    artifact = ensure_document_artifact(conn, document_id, storage_root, user_id=user_id)
    path = _stored_file(storage_root, artifact)
    if path is None:
        raise ValueError("The approved document's file is unavailable")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != str(artifact["sha256"]):
        raise ValueError("The approved document's file no longer matches what was rendered")
    return AttachableFile(str(artifact["filename"]), str(artifact["media_type"]), data, str(artifact["sha256"]), str(artifact["content_sha256"]))


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


def delete_document(conn: sqlite3.Connection, document_id: str, storage_root: Path, *, user_id: str) -> None:
    """Delete a generated document and its stored artifact. Raises PreparationNotFoundError when it is not the caller's."""
    document_record(conn, document_id, user_id=user_id)
    delete_document_artifact(conn, document_id, storage_root, user_id=user_id)
    with conn:
        conn.execute(
            "DELETE FROM generated_documents WHERE id=? AND user_id=?",
            (document_id, user_id),
        )


def backfill_approved_artifacts(conn: sqlite3.Connection, storage_root: Path, *, user_id: str) -> list[str]:
    """File the artifact of every approved document that has none yet.

    Returns the messages of the documents whose artifact could not be filed (a RuntimeError or ValueError from
    ensure_document_artifact), in the order they were tried and not de-duplicated.
    """
    warnings: list[str] = []
    approved = conn.execute(
        """
            SELECT d.id FROM generated_documents d
            LEFT JOIN generated_document_artifacts a ON a.document_id=d.id
            WHERE d.user_id=? AND d.status='approved' AND a.id IS NULL
            """,
        (user_id,),
    ).fetchall()
    for row in approved:
        try:
            ensure_document_artifact(conn, str(row["id"]), storage_root, user_id=user_id)
        except (RuntimeError, ValueError) as exc:
            warnings.append(str(exc))
    return warnings
