"""Private resume upload, extraction, draft parsing, confirmation, and deletion."""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree

from pypdf import PdfReader

from . import ROOT
from .profile import is_answered, update_profile
from .schema import utc_now


DEFAULT_STORAGE = ROOT / "data" / "resumes"
MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 25
MAX_ARCHIVE_MEMBERS = 500
MAX_ARCHIVE_EXPANDED_BYTES = 25 * 1024 * 1024
SUPPORTED_MEDIA = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


class ResumeValidationError(ValueError):
    pass


class ResumeNotFoundError(LookupError):
    pass


def scan_resume_file(data: bytes, media_type: str) -> dict[str, str]:
    """Reject known test malware and active/embedded document payloads.

    This deliberately conservative scanner is deterministic and available in
    local development. Hosted storage can add a provider AV scan before this
    same function's result is accepted.
    """
    if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in data.upper():
        raise ResumeValidationError("The upload failed the malware scan")
    if media_type == "application/pdf":
        lowered = data.lower()
        dangerous_markers = (b"/javascript", b"/launch", b"/embeddedfile", b"/openaction")
        if any(marker in lowered for marker in dangerous_markers):
            raise ResumeValidationError("PDFs with active or embedded content are not supported")
    elif media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise ResumeValidationError("The DOCX contains too many archive entries")
                if sum(member.file_size for member in members) > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise ResumeValidationError("The DOCX expands beyond the safety limit")
                lowered_names = [member.filename.lower() for member in members]
                if any("vbaproject" in name or "/embeddings/" in name for name in lowered_names):
                    raise ResumeValidationError("DOCX macros and embedded files are not supported")
        except zipfile.BadZipFile as exc:
            raise ResumeValidationError("The DOCX could not be scanned") from exc
    return {"status": "passed", "scanner": "local-document-safety-v1"}


def detect_media_type(data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" in names and "word/document.xml" in names:
                    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        except zipfile.BadZipFile:
            pass
    raise ResumeValidationError("Only valid PDF and DOCX resumes are supported")


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    cleaned: list[str] = []
    for line in lines:
        if line or (cleaned and cleaned[-1]):
            cleaned.append(line)
    return "\n".join(cleaned).strip()


def extract_pdf(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # pypdf uses several parser-specific exception types
        raise ResumeValidationError("The PDF could not be read") from exc
    if reader.is_encrypted:
        raise ResumeValidationError("Password-protected PDFs are not supported")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ResumeValidationError(f"PDF resumes are limited to {MAX_PDF_PAGES} pages")
    try:
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise ResumeValidationError("Text extraction failed for this PDF") from exc
    text = _normalize_text(text)
    if len(text) < 20:
        raise ResumeValidationError("No usable text was found; upload a text-based PDF or DOCX")
    return text


def extract_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ResumeValidationError("The DOCX could not be read") from exc
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ResumeValidationError("The DOCX document XML is invalid") from exc
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        parts = [node.text or "" for node in paragraph.iter(f"{namespace}t")]
        value = "".join(parts).strip()
        if value:
            paragraphs.append(value)
    text = _normalize_text("\n".join(paragraphs))
    if len(text) < 20:
        raise ResumeValidationError("No usable text was found in the DOCX")
    return text


def extract_resume_text(data: bytes, media_type: str) -> str:
    if media_type == "application/pdf":
        return extract_pdf(data)
    if media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return extract_docx(data)
    raise ResumeValidationError("Unsupported resume format")


def _link_targets(urls: list[str]) -> list[str]:
    """Web links only, in order, without duplicates (mailto and tel add nothing to contact)."""
    seen: set[str] = set()
    kept: list[str] = []
    for url in urls:
        value = str(url or "").strip().rstrip(".,;)")
        if not re.match(r"(?:https?://|www\.)", value, re.IGNORECASE):
            continue
        if value.lower() not in seen:
            seen.add(value.lower())
            kept.append(value)
    return kept


def extract_pdf_links(data: bytes) -> list[str]:
    """Hyperlink targets from PDF link annotations, top of the first page first.

    Resume headers usually show anchor text ("LinkedIn") whose URL lives only in
    the annotation, so the extracted text never contains it.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return []
        found: list[tuple[int, float, float, str]] = []
        for page_number, page in enumerate(reader.pages[:MAX_PDF_PAGES]):
            for annotation in page.get("/Annots") or []:
                annotation = annotation.get_object()
                if annotation.get("/Subtype") != "/Link" or "/A" not in annotation:
                    continue
                uri = annotation["/A"].get_object().get("/URI")
                if not uri:
                    continue
                rect = [float(value) for value in annotation.get("/Rect") or (0, 0, 0, 0)]
                found.append((page_number, -max(rect[1], rect[3]), min(rect[0], rect[2]), str(uri)))
    except Exception:  # links are a convenience; a malformed annotation must not fail the upload
        return []
    return _link_targets([uri for *_, uri in sorted(found)])


def extract_docx_links(data: bytes) -> list[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            relationships = ElementTree.fromstring(archive.read("word/_rels/document.xml.rels"))
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return []
    return _link_targets(
        [
            relationship.get("Target", "")
            for relationship in relationships
            if relationship.get("Type", "").endswith("/hyperlink") and relationship.get("TargetMode") == "External"
        ]
    )


def extract_resume_links(data: bytes, media_type: str) -> list[str]:
    if media_type == "application/pdf":
        return extract_pdf_links(data)
    if media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return extract_docx_links(data)
    return []


# A heading matches when it is, or ends with, one of these aliases, so
# "Engineering Projects" and "Extracurricular Activities" open their sections.
SECTION_HEADINGS = {
    "education": ("education", "academic background"),
    "experience": ("experience", "employment", "work history"),
    "projects": ("projects",),
    "skills": ("skills", "skills interests", "technologies"),
    "awards": ("awards", "honors", "honours"),
    "activities": ("activities", "leadership", "organizations", "involvement", "extracurriculars"),
}
# Headings that close the current section without being parsed, so their lines
# are not swallowed by whichever section came before.
IGNORED_HEADINGS = ("summary", "objective", "coursework", "certifications", "publications", "interests", "references")
ENTRY_SECTIONS = {"experience": "organization", "projects": "title", "awards": "title", "activities": "organization"}
# PDF extraction renders bullets as U+2022, U+FFFD (a glyph with no Unicode
# mapping), or a private-use Symbol font code point.
BULLET_PATTERN = re.compile(r"^(?:[•�▪●◦‣⁃∙·]\s*|[-*–]\s+)")
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE = rf"(?:(?:{_MONTH}|spring|summer|fall|autumn|winter)\s+\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}})"
_RANGE_DASH = r"(?:-|–|—|�)"
DATE_SUFFIX = re.compile(
    rf"\s+({_DATE}(?:\s*(?:{_RANGE_DASH}|to)\s*(?:{_DATE}|present|current|now))?)$",
    re.IGNORECASE,
)


def _strip_bullet(line: str) -> tuple[str, bool]:
    match = BULLET_PATTERN.match(line)
    return (line[match.end():].strip(), True) if match else (line, False)


def _heading_key(line: str) -> str | None:
    """The section a heading line opens, "" for an ignored heading, or None for content.

    A heading is short and carries no digits, bullet, or "Label: value" text,
    which keeps a skills line or a dated entry line from reading as one.
    """
    if BULLET_PATTERN.match(line) or re.search(r"\d", line) or ":" in line.rstrip(":"):
        return None
    words = [word for word in re.sub(r"[^a-z]+", " ", line.lower()).split() if word != "and"]
    if not words or len(words) > 5:
        return None
    normalized = " ".join(words)

    def ends_with(aliases: tuple[str, ...]) -> bool:
        return any(normalized == alias or normalized.endswith(f" {alias}") for alias in aliases)

    key = next((key for key, aliases in SECTION_HEADINGS.items() if ends_with(aliases)), None)
    if key is None and ends_with(IGNORED_HEADINGS):
        return ""
    return key


def _split_dates(line: str) -> tuple[str, str]:
    """Split "Acme Robotics Nov 2025 - Present" into the name and its date range."""
    match = DATE_SUFFIX.search(line)
    if not match or not line[: match.start()].strip():
        return line.strip(), ""
    return line[: match.start()].strip(), re.sub(rf"\s*{_RANGE_DASH}\s*", " - ", match.group(1))


def _parse_entries(lines: list[str], name_key: str, wrap_width: int) -> list[dict[str, Any]]:
    """Group section lines into entries: a name line with dates, an optional role line, then bullets.

    PDF text wraps long bullets onto plain lines. A plain line that starts
    lowercase, or follows a line near the full text width, continues the line
    before it; otherwise it starts a new entry.
    """
    has_headings = any(not _strip_bullet(line)[1] for line in lines)
    entries: list[dict[str, Any]] = []
    entry: dict[str, Any] | None = None
    last_field = ""
    previous = ""
    for raw in lines:
        line, is_bullet = _strip_bullet(raw)
        if not line:
            continue
        dated = bool(_split_dates(line)[1])
        # A name line that ended in dates is complete, so a long one is not a wrap.
        wrapped = entry is not None and (
            not line[0].isupper()
            or (len(previous) >= wrap_width and not (last_field != "highlights" and entry["dates"]))
        )
        if entry is not None and is_bullet and has_headings:
            entry["highlights"].append(line)
            last_field = "highlights"
        elif entry is not None and not is_bullet and not dated and wrapped:
            if last_field == "highlights":
                entry["highlights"][-1] = f"{entry['highlights'][-1]} {line}"
            else:
                entry[last_field] = f"{entry[last_field]} {line}"
        elif entry is not None and not is_bullet and not dated and not entry["role"] and not entry["highlights"]:
            entry["role"] = line
            last_field = "role"
        else:
            # A section of bullets alone (a list of awards) makes each bullet its own entry.
            name, dates = _split_dates(line)
            entry = {name_key: name, "role": "", "dates": dates, "highlights": []}
            entries.append(entry)
            last_field = name_key
        previous = raw
    # Blank fields are dropped so confirming a suggestion never records a blank
    # as a fact. No "outreach" key: drafting treats an unmarked entry as support.
    return [{key: value for key, value in item.items() if value} for item in entries]


def _parse_skills(lines: list[str]) -> list[str]:
    """Skills from "Label: a, b" lines, joining a wrapped line onto the label line it continues."""
    labeled = any(":" in line for line in lines)
    rows: list[str] = []
    for raw in lines:
        line, is_bullet = _strip_bullet(raw)
        if rows and labeled and not is_bullet and ":" not in line:
            rows[-1] = f"{rows[-1]}{'' if rows[-1].endswith('-') else ' '}{line}"
        elif line:
            rows.append(line)
    skills: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for part in re.split(r"[,;|•]", row.split(":", 1)[-1]):
            skill = part.strip().rstrip(".")
            if skill and skill.lower() not in seen:
                seen.add(skill.lower())
                skills.append(skill)
    return skills


def _contact_links(urls: list[str]) -> dict[str, str]:
    def host(url: str) -> str:
        return re.sub(r"^(?:https?://)?(?:www\.)?", "", url.lower()).split("/", 1)[0].split(":", 1)[0]

    def is_linkedin(url: str) -> bool:
        return host(url) == "linkedin.com" or host(url).endswith(".linkedin.com")

    return {
        "linkedin": next((url for url in urls if is_linkedin(url)), ""),
        "github": next((url for url in urls if host(url) == "github.com"), ""),
        "portfolio": next((url for url in urls if not is_linkedin(url) and host(url) != "github.com"), ""),
    }


def parse_resume_draft(text: str, links: list[str] | None = None) -> dict[str, Any]:
    """Draft profile facts for the student to review; nothing here is confirmed.

    ``links`` are hyperlink targets from the document itself (PDF annotations,
    DOCX relationships), which extracted text only shows as anchor text.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    email_match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.IGNORECASE)
    phone_match = re.search(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}", text)
    url_matches = re.findall(r"(?:https?://|www\.)[^\s|]+", text, re.IGNORECASE)
    name = ""
    if lines and len(lines[0]) <= 80 and not email_match_or_phone(lines[0]):
        name = lines[0]

    sections: dict[str, list[str]] = {key: [] for key in SECTION_HEADINGS}
    current: str | None = None
    for line in lines[1:]:
        key = _heading_key(line)
        if key is not None:
            current = key or None
            continue
        if current:
            sections[current].append(line)

    skills = _parse_skills(sections["skills"])
    # A wrapped line breaks close to the widest line in the document.
    wrap_width = int(max((len(line) for line in lines), default=0) * 0.75)
    entries = {field: _parse_entries(sections[field], name_key, wrap_width) for field, name_key in ENTRY_SECTIONS.items()}

    contact = {
        "email": email_match.group(0) if email_match else "",
        "phone": phone_match.group(0) if phone_match else "",
        **_contact_links(_link_targets([*(links or []), *url_matches])),
    }
    suggestions = {"name": name, "contact": contact, "skills": skills, **entries}
    return {
        "name": name,
        "contact": contact,
        "skills": skills,
        **entries,
        "sections": sections,
        "profile_suggestions": {key: value for key, value in suggestions.items() if is_answered(value)},
    }


def email_match_or_phone(value: str) -> bool:
    return bool("@" in value or re.search(r"\d{3}.*\d{3}.*\d{4}", value))


def _safe_storage_path(storage_root: Path, value: str) -> Path:
    root = storage_root.expanduser().resolve()
    candidate = (root / value).resolve()
    if candidate.parent != root:
        raise ResumeValidationError("Invalid resume storage path")
    return candidate


def store_resume(
    conn: sqlite3.Connection,
    *,
    data: bytes,
    original_name: str,
    storage_root: Path = DEFAULT_STORAGE,
    user_id: str,
) -> dict[str, Any]:
    if not data:
        raise ResumeValidationError("The uploaded resume is empty")
    if len(data) > MAX_RESUME_BYTES:
        raise ResumeValidationError("Resume files are limited to 5 MB")
    media_type = detect_media_type(data)
    scan_result = scan_resume_file(data, media_type)
    text = extract_resume_text(data, media_type)
    digest = hashlib.sha256(data).hexdigest()
    conn.row_factory = sqlite3.Row
    existing = conn.execute(
        """
        SELECT rv.* FROM resume_versions rv
        JOIN resume_files rf ON rf.id=rv.resume_file_id
        WHERE rf.user_id=? AND rf.sha256=?
        """,
        (user_id, digest),
    ).fetchone()
    if existing:
        return resume_record(conn, str(existing["id"]), user_id=user_id)

    storage_root = storage_root.expanduser().resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    file_id = f"resume-file-{uuid4().hex}"
    version_id = f"resume-{uuid4().hex}"
    stored_name = f"{file_id}{SUPPORTED_MEDIA[media_type]}"
    stored_path = _safe_storage_path(storage_root, stored_name)
    stored_path.write_bytes(data)
    timestamp = utc_now()
    try:
        with conn:
            parsed = parse_resume_draft(text, links=extract_resume_links(data, media_type))
            parsed["file_scan"] = scan_result
            conn.execute(
                """
                INSERT INTO resume_files(
                    id, user_id, original_name, media_type, byte_size,
                    sha256, storage_path, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    user_id,
                    Path(original_name).name or f"resume{SUPPORTED_MEDIA[media_type]}",
                    media_type,
                    len(data),
                    digest,
                    stored_name,
                    timestamp,
                ),
            )
            conn.execute(
                """
                INSERT INTO resume_versions(
                    id, resume_file_id, user_id, extracted_text, parsed_json,
                    confirmed_json, status, created_at, confirmed_at
                ) VALUES(?, ?, ?, ?, ?, '{}', 'draft', ?, NULL)
                """,
                (version_id, file_id, user_id, text, json.dumps(parsed), timestamp),
            )
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise
    return resume_record(conn, version_id, user_id=user_id)


def resume_record(conn: sqlite3.Connection, version_id: str, *, user_id: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT rv.*, rf.original_name, rf.media_type, rf.byte_size,
               rf.sha256, rf.storage_path
        FROM resume_versions rv
        JOIN resume_files rf ON rf.id=rv.resume_file_id
        WHERE rv.id=? AND rv.user_id=?
        """,
        (version_id, user_id),
    ).fetchone()
    if not row:
        raise ResumeNotFoundError(version_id)
    return {
        "id": row["id"],
        "file_id": row["resume_file_id"],
        "original_name": row["original_name"],
        "media_type": row["media_type"],
        "byte_size": int(row["byte_size"]),
        "sha256": row["sha256"],
        "status": row["status"],
        "extracted_text": row["extracted_text"],
        "parsed": json.loads(row["parsed_json"] or "{}"),
        "confirmed": json.loads(row["confirmed_json"] or "{}"),
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
    }


def list_resumes(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM resume_versions WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    ]
    return [resume_record(conn, str(version_id), user_id=user_id) for version_id in ids]


def confirm_resume(
    conn: sqlite3.Connection,
    version_id: str,
    confirmed_data: dict[str, Any],
    profile_updates: dict[str, Any],
    confirmed_profile_fields: list[str],
    *,
    user_id: str,
    profile_file: Path | None = None,
) -> dict[str, Any]:
    resume_record(conn, version_id, user_id=user_id)
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            UPDATE resume_versions
            SET confirmed_json=?, status='confirmed', confirmed_at=?
            WHERE id=? AND user_id=?
            """,
            (json.dumps(confirmed_data), timestamp, version_id, user_id),
        )
    if profile_updates:
        update_profile(
            conn,
            profile_updates,
            confirmed_profile_fields,
            source=f"resume:{version_id}",
            user_id=user_id,
            profile_file=profile_file,
        )
    return resume_record(conn, version_id, user_id=user_id)


def resume_file_path(
    conn: sqlite3.Connection,
    version_id: str,
    storage_root: Path = DEFAULT_STORAGE,
    *,
    user_id: str,
) -> tuple[Path, str, str]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT rf.storage_path, rf.original_name, rf.media_type
        FROM resume_versions rv JOIN resume_files rf ON rf.id=rv.resume_file_id
        WHERE rv.id=? AND rv.user_id=?
        """,
        (version_id, user_id),
    ).fetchone()
    if not row:
        raise ResumeNotFoundError(version_id)
    path = _safe_storage_path(storage_root, str(row["storage_path"]))
    if not path.exists():
        raise ResumeNotFoundError(version_id)
    return path, str(row["original_name"]), str(row["media_type"])


def delete_resume(
    conn: sqlite3.Connection,
    version_id: str,
    storage_root: Path = DEFAULT_STORAGE,
    *,
    user_id: str,
) -> None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT rv.resume_file_id, rf.storage_path
        FROM resume_versions rv JOIN resume_files rf ON rf.id=rv.resume_file_id
        WHERE rv.id=? AND rv.user_id=?
        """,
        (version_id, user_id),
    ).fetchone()
    if not row:
        raise ResumeNotFoundError(version_id)
    path = _safe_storage_path(storage_root, str(row["storage_path"]))
    with conn:
        conn.execute("DELETE FROM resume_files WHERE id=? AND user_id=?", (row["resume_file_id"], user_id))
    path.unlink(missing_ok=True)
