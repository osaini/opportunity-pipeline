"""Application artifacts: the tailored resume and cover letter, and their PDF export."""

from __future__ import annotations

import html
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import paths
from .config import load_json, load_profile
from .identity import normalized
from .paths import display_path


# ---------------------------------------------------------------------------
# Application artifacts: resume and cover letter
#
# `config/resume.json` is the only source of factual claims. Matching against a
# posting reorders and emphasises what is already there -- it never adds a
# skill, a metric, or an experience. Anything the tool cannot know is emitted as
# a visibly-marked TODO rather than invented.
# ---------------------------------------------------------------------------


def load_resume(path: Path = paths.RESUME_PATH) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"Missing {display_path(path)}.\n"
            f"Copy {display_path(paths.RESUME_EXAMPLE_PATH)} to {display_path(path)} and fill it in.\n"
            "It is the only source of factual claims about you -- nothing is invented from it."
        )
    return load_json(path)


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _job_keywords(description: str, title: str) -> set[str]:
    """Normalised word set of a posting, for matching against your own terms."""
    return set(normalized(f"{title} {description}").split())


def term_matches_job(term: str, job_words: set[str]) -> bool:
    """True when every word of a term appears in the posting.

    Whole words only. A substring test reports "CAD" as present in "cadence"
    and "R" in everything, which would put a bogus emphasis on the resume.
    """
    words = normalized(term).split()
    return bool(words) and all(word in job_words for word in words)


def _slugify(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-") or "untitled"


def _bullets_html(bullets: Iterable[str]) -> str:
    items = [f"    <li>{_esc(bullet)}</li>" for bullet in bullets if str(bullet).strip()]
    return f"  <ul>\n{chr(10).join(items)}\n  </ul>" if items else ""


def _entry_html(title: str, subtitle: str, dates: str, bullets: Iterable[str]) -> str:
    parts = [
        '<div class="entry">',
        '  <div class="entry-head">',
        f'    <span class="entry-title">{_esc(title)}</span>',
        f'    <span class="entry-dates">{_esc(dates)}</span>' if dates else "",
        "  </div>",
        f'  <div class="entry-sub">{_esc(subtitle)}</div>' if subtitle else "",
        _bullets_html(bullets),
        "</div>",
    ]
    return "\n".join(part for part in parts if part)


def _date_range(item: dict[str, Any]) -> str:
    start, end = str(item.get("start") or "").strip(), str(item.get("end") or "").strip()
    if start and end:
        return f"{start} - {end}"
    return start or end


def build_resume_html(resume: dict[str, Any], job: sqlite3.Row | None = None) -> str:
    job_words = _job_keywords(job["description"], job["title"]) if job else set()
    sections: list[str] = []

    summary = str(resume.get("summary") or "").strip()
    if summary:
        sections.append(f'<section>\n  <h2>Summary</h2>\n  <p class="summary">{_esc(summary)}</p>\n</section>')

    education = [item for item in resume.get("education") or [] if item.get("school")]
    if education:
        entries = []
        for item in education:
            subtitle = " | ".join(
                part
                for part in (str(item.get("degree") or ""), str(item.get("location") or ""))
                if part
            )
            details = []
            if str(item.get("gpa") or "").strip():
                details.append(f"GPA: {item['gpa']}")
            coursework = [str(course) for course in item.get("coursework") or [] if str(course).strip()]
            if coursework:
                # Coursework is where a first-year has the most relevant
                # evidence, so matched courses lead.
                ordered = sorted(coursework, key=lambda c: not term_matches_job(c, job_words))
                rendered = ", ".join(
                    f'<span class="match">{_esc(course)}</span>'
                    if term_matches_job(course, job_words)
                    else _esc(course)
                    for course in ordered
                )
                details.append(f"Relevant coursework: {rendered}")
            body = (
                f'  <ul>\n' + "\n".join(f"    <li>{detail}</li>" for detail in details) + "\n  </ul>"
                if details
                else ""
            )
            entry = "\n".join(
                part
                for part in [
                    '<div class="entry">',
                    '  <div class="entry-head">',
                    f'    <span class="entry-title">{_esc(item["school"])}</span>',
                    f'    <span class="entry-dates">{_esc(item.get("graduation", ""))}</span>',
                    "  </div>",
                    f'  <div class="entry-sub">{_esc(subtitle)}</div>' if subtitle else "",
                    body,
                    "</div>",
                ]
                if part
            )
            entries.append(entry)
        sections.append("<section>\n  <h2>Education</h2>\n" + "\n".join(entries) + "\n</section>")

    experience = [item for item in resume.get("experience") or [] if item.get("organization")]
    if experience:
        entries = [
            _entry_html(
                item.get("role") or item["organization"],
                " | ".join(
                    part
                    for part in (
                        str(item["organization"]) if item.get("role") else "",
                        str(item.get("location") or ""),
                    )
                    if part
                ),
                _date_range(item),
                item.get("bullets") or [],
            )
            for item in experience
        ]
        sections.append("<section>\n  <h2>Experience</h2>\n" + "\n".join(entries) + "\n</section>")

    projects = [item for item in resume.get("projects") or [] if item.get("name")]
    if projects:
        # A posting-relevant project is worth more than a chronological one.
        projects.sort(
            key=lambda item: not any(
                term_matches_job(word, job_words)
                for word in [item.get("name", ""), item.get("context", "")]
                + list(item.get("bullets") or [])
            )
        )
        entries = [
            _entry_html(
                item["name"],
                str(item.get("context") or ""),
                _date_range(item),
                item.get("bullets") or [],
            )
            for item in projects
        ]
        sections.append("<section>\n  <h2>Projects</h2>\n" + "\n".join(entries) + "\n</section>")

    skills = resume.get("skills") or {}
    if skills:
        rows = []
        for group, items in skills.items():
            listed = [str(item) for item in items if str(item).strip()]
            if not listed:
                continue
            # Matched skills first, then bolded, so a recruiter skimming for the
            # posting's terms finds them without the wording being altered.
            listed.sort(key=lambda item: not term_matches_job(item, job_words))
            rendered = ", ".join(
                f'<span class="match">{_esc(item)}</span>'
                if term_matches_job(item, job_words)
                else _esc(item)
                for item in listed
            )
            rows.append(
                f'  <div class="skills-row"><span class="skills-label">{_esc(group)}:</span> {rendered}</div>'
            )
        if rows:
            sections.append("<section>\n  <h2>Skills</h2>\n" + "\n".join(rows) + "\n</section>")

    for key, heading in (("awards", "Awards"), ("activities", "Activities")):
        items = [str(item) for item in resume.get(key) or [] if str(item).strip()]
        if items:
            sections.append(
                f"<section>\n  <h2>{heading}</h2>\n" + _bullets_html(items) + "\n</section>"
            )

    contact = resume.get("contact") or {}
    contact_parts = [
        f"<span>{_esc(value)}</span>"
        for value in (
            contact.get("location"),
            contact.get("email"),
            contact.get("phone"),
            contact.get("linkedin"),
            contact.get("github"),
            contact.get("portfolio"),
        )
        if str(value or "").strip()
    ]

    template = (paths.TEMPLATE_DIR / "resume.html").read_text(encoding="utf-8")
    title = f"{resume.get('name', 'Resume')} - Resume"
    if job:
        title += f" - {job['company']}"
    return (
        template.replace("TITLE_PLACEHOLDER", _esc(title))
        .replace("NAME_PLACEHOLDER", _esc(resume.get("name", "")))
        .replace("CONTACT_PLACEHOLDER", "".join(contact_parts))
        .replace("BODY_PLACEHOLDER", "\n\n".join(sections))
    )


# Requirement-shaped lines in a posting, used to seed the cover-letter draft.
_REQUIREMENT_HINTS = (
    "experience",
    "familiar",
    "proficien",
    "knowledge",
    "coursework",
    "pursuing",
    "ability to",
    "skills",
)


def job_requirement_lines(description: str, limit: int = 6) -> list[str]:
    """Sentences from a posting that read like requirements."""
    sentences = re.split(r"(?<=[.;])\s+|\s{2,}", description or "")
    picked: list[str] = []
    for sentence in sentences:
        cleaned = sentence.strip(" -*•\t")
        if not 25 <= len(cleaned) <= 220:
            continue
        lowered = cleaned.lower()
        if any(hint in lowered for hint in _REQUIREMENT_HINTS) and cleaned not in picked:
            picked.append(cleaned)
        if len(picked) >= limit:
            break
    return picked


# A leading degree abbreviation ("B.S.", "BSE", "B.Eng.", "M.S.") before the
# field of study. "B.S. Chemistry" reads as "Chemistry"; "B.S. in Biology"
# as "Biology".
_DEGREE_ABBREVIATION_RE = re.compile(
    r"^\s*(?:B\.?\s?S\.?\s?E\.?|B\.?\s?S\.?|B\.?\s?A\.?|B\.?\s?Eng\.?|B\.?\s?Sc\.?|"
    r"M\.?\s?S\.?|M\.?\s?A\.?|M\.?\s?Eng\.?|M\.?\s?Sc\.?|Ph\.?\s?D\.?)"
    r"(?=[\s,]|$)[\s,]*(?:in\s+|of\s+)?",
    re.IGNORECASE,
)


def build_cover_letter_html(
    resume: dict[str, Any], job: sqlite3.Row, profile: dict[str, Any] | None = None
) -> str:
    job_words = _job_keywords(job["description"], job["title"])
    matched: list[str] = []
    for group_items in (resume.get("skills") or {}).values():
        for item in group_items:
            if term_matches_job(str(item), job_words) and str(item) not in matched:
                matched.append(str(item))
    for project in resume.get("projects") or []:
        name = str(project.get("name") or "")
        if name and term_matches_job(name, job_words) and name not in matched:
            matched.append(name)

    profile = profile or {}
    education = next(
        (item for item in resume.get("education") or [] if isinstance(item, dict)), {}
    )
    # Described from the degree already on file rather than asserting a year of
    # study, which nothing here reliably knows. Nothing is defaulted: a missing
    # degree or school becomes a visible TODO, never invented text.
    degree = str(education.get("degree") or profile.get("degree") or "").strip()
    school = str(education.get("school") or profile.get("school") or "").strip()
    standing = _DEGREE_ABBREVIATION_RE.sub("", degree).strip(" ,-")
    todo = '<span class="todo">{}</span>'
    article = "an" if standing[:1].lower() in "aeiou" and standing else "a"
    if standing and school:
        identity = f"I am {article} {_esc(standing)} student at {_esc(school)}, and "
    elif standing:
        identity = f"I am {article} {_esc(standing)} student, and "
    elif school:
        identity = f"I am a student at {_esc(school)}, and "
    else:
        identity = (
            "I am a "
            + todo.format("[your degree and school -- add education to config/resume.json]")
            + " student, and "
        )
    paragraphs = [
        f"<p>Dear {todo.format('[hiring manager name, or &ldquo;Hiring Team&rdquo;]')},</p>",
        (
            f"<p>I am applying for the <strong>{_esc(job['title'])}</strong> position at "
            f"{_esc(job['company'])}. "
            + identity
            + todo.format("[one sentence on why this company specifically -- name something real "
                          "you know about their work]")
            + "</p>"
        ),
    ]

    if matched:
        listed = ", ".join(_esc(item) for item in matched[:6])
        paragraphs.append(
            f"<p>The posting asks for {listed}, which I have worked with directly. "
            + todo.format("[pick ONE of these and give a concrete example: what you built, what "
                          "went wrong, what you measured]")
            + "</p>"
        )
    else:
        paragraphs.append(
            "<p>"
            + todo.format(
                "[No skill in your resume.json matched this posting's text. Write the connection "
                "yourself, or reconsider whether this role fits.]"
            )
            + "</p>"
        )

    requirements = job_requirement_lines(job["description"])
    if requirements:
        items = "\n".join(f"    <li>{_esc(line)}</li>" for line in requirements)
        paragraphs.append(
            "<p>What the posting asks for, to answer point by point (delete this block before "
            "sending -- it is scaffolding, not letter text):</p>\n  <ul>\n" + items + "\n  </ul>"
        )

    paragraphs.append(
        f"<p>I would welcome the chance to talk about the role. Thank you for your time.</p>"
    )

    contact = resume.get("contact") or {}
    contact_line = " · ".join(
        str(value)
        for value in (contact.get("email"), contact.get("phone"), contact.get("location"))
        if str(value or "").strip()
    )
    recipient = f"{_esc(job['company'])}<br>Re: {_esc(job['title'])}"
    if job["location"]:
        # Boards pack several sites into one field ("Austin, Texas, United
        # States; South San Francisco, ..."); an address block wants one.
        recipient += f"<br>{_esc(job['location'].split(';')[0].strip())}"

    template = (paths.TEMPLATE_DIR / "cover-letter.html").read_text(encoding="utf-8")
    _today = datetime.now(timezone.utc)
    return (
        template.replace("TITLE_PLACEHOLDER", _esc(f"Cover letter - {job['company']}"))
        .replace("NAME_PLACEHOLDER", _esc(resume.get("name", "")))
        .replace("CONTACT_PLACEHOLDER", _esc(contact_line))
        # Built by hand: "%-d" is a glibc extension that Windows' strftime does
        # not accept, and "%d" would render "August 05, 2026".
        .replace("DATE_PLACEHOLDER", f"{_today.strftime('%B')} {_today.day}, {_today.year}")
        .replace("RECIPIENT_PLACEHOLDER", recipient)
        .replace("BODY_PLACEHOLDER", "\n".join(paragraphs))
    )


def html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """Render a local HTML file to PDF. False when Playwright is not installed.

    Imported lazily and on purpose: the pipeline itself has no dependencies, and
    discovery must keep working on a machine where this was never set up.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            # Chromium refuses to load file:// subresources from a setContent()
            # page, so navigate to the file instead of injecting its markup.
            page.goto(html_path.as_uri(), wait_until="load")
            page.pdf(path=str(pdf_path), format="Letter", print_background=True)
        finally:
            browser.close()
    return True


def _resolve_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, company, title, location, description, url FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"No posting with id {job_id}. Find ids in output/shortlist.md.")
    return row


def _optional_profile() -> dict[str, Any]:
    """The profile when one is readable; a letter can still be drafted without it."""
    try:
        profile = load_profile()
    except SystemExit:
        return {}
    return profile if isinstance(profile, dict) else {}


def write_artifact(
    conn: sqlite3.Connection,
    kind: str,
    job_id: str | None,
    as_pdf: bool,
) -> Path:
    resume = load_resume()
    job = _resolve_job(conn, job_id) if job_id else None
    if kind == "cover-letter":
        if job is None:
            raise SystemExit("A cover letter needs a posting: pass --job <ID>.")
        markup = build_cover_letter_html(resume, job, _optional_profile())
        stem = f"cover-letter-{_slugify(job['company'])}-{_slugify(job['title'])}"
    else:
        markup = build_resume_html(resume, job)
        # The posting id is part of the name because one company runs several
        # postings, and two tailored resumes for the same employer must not
        # overwrite each other.
        stem = "resume" + (f"-{_slugify(job['company'])}-{job['id'][:8]}" if job else "")

    paths.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    html_path = paths.ARTIFACT_DIR / f"{stem}.html"
    html_path.write_text(markup, encoding="utf-8")
    print(f"Wrote {display_path(html_path)}")

    if as_pdf:
        pdf_path = paths.ARTIFACT_DIR / f"{stem}.pdf"
        if html_to_pdf(html_path, pdf_path):
            print(f"Wrote {display_path(pdf_path)}")
        else:
            print(
                "PDF skipped: Playwright is not installed.\n"
                "  pip install -r requirements-optional.txt && python3 -m playwright install chromium\n"
                "Until then, open the HTML and print to PDF from the browser -- same output.",
                file=sys.stderr,
            )
    if job is not None and kind != "cover-letter":
        print(f"Tailored against: {job['company']} - {job['title']}")
        print("Emphasis only. Nothing was added that is not already in config/resume.json.")
    return html_path
