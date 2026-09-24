"""Private manual opportunity capture with explicit confirmation."""

from __future__ import annotations

import hashlib
import html
import io
import ipaddress
import json
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from . import ROOT
from .company_tags import regenerate_company_tags
from .opportunity_metadata import extract_opportunity_metadata
from .resumes import ResumeValidationError, detect_media_type, extract_pdf, scan_resume_file
from .schema import sort_key, RULESET_VERSION, utc_now


DEFAULT_CAPTURE_STORAGE = ROOT / "data" / "captures"
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000


class CaptureValidationError(ValueError):
    pass


class CaptureNotFoundError(LookupError):
    pass


def _safe_path(root: Path, name: str) -> Path:
    resolved_root = root.expanduser().resolve()
    candidate = (resolved_root / name).resolve()
    if candidate.parent != resolved_root:
        raise CaptureValidationError("Invalid capture storage path")
    return candidate


def _validate_source_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise CaptureValidationError("Enter a public HTTP or HTTPS job URL")
    return urllib.parse.urlunsplit(parsed)


def _validate_public_url(value: str) -> str:
    safe_value = _validate_source_url(value)
    parsed = urllib.parse.urlsplit(safe_value)
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
        }
    except socket.gaierror as exc:
        raise CaptureValidationError("The job URL hostname could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise CaptureValidationError("Private, local, and reserved job URLs are not allowed")
    return safe_value


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        safe_url = _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


def parse_html_draft(document: str, source_url: str) -> dict[str, Any]:
    def first(pattern: str) -> str:
        match = re.search(pattern, document, re.IGNORECASE | re.DOTALL)
        return html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))).strip() if match else ""

    title = first(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)")
    if not title:
        title = first(r"<h1[^>]*>(.*?)</h1>") or first(r"<title[^>]*>(.*?)</title>")
    company = first(r"<meta[^>]+property=[\"']og:site_name[\"'][^>]+content=[\"']([^\"']+)")
    text = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", document, flags=re.IGNORECASE | re.DOTALL)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", text))
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())[:100_000]
    return {
        "company": company,
        "title": title[:500],
        "location": "",
        "url": source_url,
        "description": text,
    }


def capture_url(conn: sqlite3.Connection, url: str, *, user_id: str) -> dict[str, Any]:
    safe_url = _validate_public_url(url)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    request = urllib.request.Request(safe_url, headers={"User-Agent": "OpportunityPipeline/1.0"})
    try:
        with opener.open(request, timeout=12) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "text/plain"}:
                raise CaptureValidationError("The URL did not return an HTML job page")
            data = response.read(MAX_HTML_BYTES + 1)
            if len(data) > MAX_HTML_BYTES:
                raise CaptureValidationError("The job page exceeds the 2 MB capture limit")
            charset = response.headers.get_content_charset() or "utf-8"
            final_url = _validate_public_url(response.geturl())
    except (OSError, urllib.error.URLError) as exc:
        raise CaptureValidationError("The job page could not be fetched") from exc
    document = data.decode(charset, errors="replace")
    parsed = parse_html_draft(document, final_url)
    return _insert_capture(conn, "url", final_url, "", "text/html", "", parsed["description"], parsed, user_id)


def ocr_image(data: bytes) -> str:
    """Best-effort local OCR. Returns '' when no OCR engine is available.

    Tesseract is the only supported engine; it is optional and never a
    hard dependency. Failures degrade to manual confirmation, exactly like
    the no-engine case.
    """
    executable = shutil.which("tesseract")
    if not executable:
        return ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(data)
            temp_path = Path(handle.name)
        try:
            completed = subprocess.run(
                [executable, temp_path.as_posix(), "stdout", "--psm", "6"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            temp_path.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _scan_image(data: bytes) -> str:
    if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in data.upper():
        raise CaptureValidationError("The upload failed the malware scan")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise CaptureValidationError("Only PNG and JPEG screenshots are supported")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise CaptureValidationError("The screenshot dimensions exceed the safety limit")
            image.verify()
            return "image/png" if image.format == "PNG" else "image/jpeg"
    except (UnidentifiedImageError, OSError) as exc:
        raise CaptureValidationError("The screenshot could not be read") from exc


def capture_file(
    conn: sqlite3.Connection,
    data: bytes,
    original_name: str,
    storage_root: Path = DEFAULT_CAPTURE_STORAGE,
    *,
    user_id: str,
) -> dict[str, Any]:
    if not data or len(data) > MAX_CAPTURE_BYTES:
        raise CaptureValidationError("Capture files must be between 1 byte and 8 MB")
    extracted_text = ""
    source_type = "screenshot"
    suffix = ""
    media_type = ""
    try:
        media_type = detect_media_type(data)
    except ResumeValidationError:
        media_type = _scan_image(data)
        suffix = ".png" if media_type == "image/png" else ".jpg"
        extracted_text = ocr_image(data)
    else:
        if media_type != "application/pdf":
            raise CaptureValidationError("Manual document capture supports PDF, PNG, or JPEG")
        try:
            scan_resume_file(data, media_type)
            extracted_text = extract_pdf(data)
        except ResumeValidationError as exc:
            raise CaptureValidationError(str(exc)) from exc
        source_type = "pdf"
        suffix = ".pdf"
    storage_root = storage_root.expanduser().resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    stored_name = f"capture-file-{uuid4().hex}{suffix}"
    _safe_path(storage_root, stored_name).write_bytes(data)
    parsed = {
        "company": "",
        "title": "",
        "location": "",
        "url": "",
        "description": extracted_text,
    }
    if source_type == "screenshot":
        parsed["extraction_note"] = (
            "Screenshot text extracted with local OCR; confirm fields manually."
            if extracted_text
            else "No local OCR was available; confirm screenshot fields manually."
        )
    else:
        parsed["extraction_note"] = "PDF text extracted locally."
    try:
        return _insert_capture(
            conn, source_type, "", Path(original_name).name, media_type,
            stored_name, extracted_text, parsed, user_id,
        )
    except Exception:
        _safe_path(storage_root, stored_name).unlink(missing_ok=True)
        raise


def _insert_capture(
    conn: sqlite3.Connection,
    source_type: str,
    source_url: str,
    original_name: str,
    media_type: str,
    storage_path: str,
    extracted_text: str,
    parsed: dict[str, Any],
    user_id: str,
) -> dict[str, Any]:
    capture_id = f"capture-{uuid4().hex}"
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO opportunity_captures(
                id, user_id, source_type, source_url, original_name, media_type,
                storage_path, extracted_text, parsed_json, status, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)
            """,
            (capture_id, user_id, source_type, source_url, original_name, media_type, storage_path, extracted_text, json.dumps(parsed), timestamp),
        )
    return get_capture(conn, capture_id, user_id=user_id)


def get_capture(conn: sqlite3.Connection, capture_id: str, *, user_id: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM opportunity_captures WHERE id=? AND user_id=?",
        (capture_id, user_id),
    ).fetchone()
    if not row:
        raise CaptureNotFoundError(capture_id)
    result = dict(row)
    result["parsed"] = json.loads(row["parsed_json"] or "{}")
    result.pop("parsed_json", None)
    result.pop("storage_path", None)
    return result


def confirm_capture(
    conn: sqlite3.Connection,
    capture_id: str,
    fields: dict[str, Any],
    *,
    user_id: str,
) -> dict[str, Any]:
    capture = get_capture(conn, capture_id, user_id=user_id)
    if capture["status"] == "confirmed":
        return capture
    company = str(fields.get("company", "")).strip()
    title = str(fields.get("title", "")).strip()
    url = _validate_source_url(str(fields.get("url") or capture["source_url"]))
    if not company or not title:
        raise CaptureValidationError("Company and title are required before confirmation")
    location = str(fields.get("location", "")).strip()
    description = str(fields.get("description", capture["extracted_text"] or "")).strip()
    role_type = str(fields.get("role_type", "other")).strip() or "other"
    timestamp = utc_now()
    digest = hashlib.sha256(f"{user_id}|{url}|{company}|{title}".encode()).hexdigest()[:24]
    opportunity_id = f"manual-{digest}"
    application_id = f"app-{opportunity_id}"
    metadata = extract_opportunity_metadata(title, location, description)
    with conn:
        conn.execute(
            """
            INSERT INTO opportunities(
                id, company, title, company_sort_key, title_sort_key, location, region,
                role_type, url, description,
                posted_at, posted_at_utc, deadline_at, first_seen_at, last_seen_at, active,
                fingerprint, content_fingerprint, duplicate_of, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'Unknown', ?, ?, ?, NULL, NULL, ?, ?, ?, 1, ?, ?, NULL, ?, ?)
            ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (opportunity_id, company, title, sort_key(company), sort_key(title), location, role_type, url, description, metadata["deadline_at"], timestamp, timestamp, digest, digest, timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO opportunity_sources(
                opportunity_id, source_key, source_name, external_id, source_url, first_seen_at, last_seen_at
            ) VALUES(?, 'manual:capture', 'Manual capture', ?, ?, ?, ?)
            """,
            (opportunity_id, capture_id, url, timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO opportunity_attributes(
                opportunity_id, remote_mode, terms_json, graduation_years_json,
                pay_min, pay_max, pay_period, currency, extracted_json, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (opportunity_id, metadata["remote_mode"], json.dumps(metadata["terms"]), json.dumps(metadata["graduation_years"]), metadata["pay_min"], metadata["pay_max"], metadata["pay_period"], metadata["currency"], json.dumps(metadata), timestamp),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO fit_scores(
                opportunity_id, user_id, ruleset_version, score, explanation_json, created_at
            ) VALUES(?, ?, ?, 0, '["Manual capture; rescore after confirmation"]', ?)
            """,
            (opportunity_id, user_id, RULESET_VERSION, timestamp),
        )
        conn.execute(
            """
            INSERT INTO applications(
                id, opportunity_id, user_id, stage, notes, applied_at,
                follow_up_at, created_at, updated_at
            ) VALUES(?, ?, ?, 'applying', '', NULL, NULL, ?, ?)
            ON CONFLICT(opportunity_id, user_id) DO NOTHING
            """,
            (application_id, opportunity_id, user_id, timestamp, timestamp),
        )
        actual_application = conn.execute(
            "SELECT id FROM applications WHERE opportunity_id=? AND user_id=?",
            (opportunity_id, user_id),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO application_events(
                application_id, event_type, from_stage, to_stage, detail_json, created_at
            ) VALUES(?, 'capture_confirmed', NULL, 'applying', ?, ?)
            """,
            (actual_application, json.dumps({"capture_id": capture_id, "source_type": capture["source_type"]}), timestamp),
        )
        conn.execute(
            """
            UPDATE opportunity_captures
            SET status='confirmed', application_id=?, confirmed_at=?
            WHERE id=? AND user_id=?
            """,
            (actual_application, timestamp, capture_id, user_id),
        )
        regenerate_company_tags(conn, [sort_key(company)])
    return get_capture(conn, capture_id, user_id=user_id)
