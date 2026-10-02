"""Render a preparation document (a resume variant or cover letter) to PDF.

The documents are Markdown the student edited and approved. They become a
plain printable page: everything is escaped first, only the handful of
Markdown forms these documents use is turned back into markup, and Chromium
prints it with JavaScript off and every network request refused, so nothing a
draft contains can run or reach out.

Playwright is optional here exactly as it is for ``pipeline.py resume --pdf``:
without it, pdf_renderer() returns None and the app offers Markdown only.
"""

from __future__ import annotations

import html
import re
from typing import Callable

Renderer = Callable[[str], bytes]

PAGE_STYLE = """
@page { size: Letter; margin: 0.7in 0.75in; }
body { font-family: Georgia, "Times New Roman", serif; font-size: 11pt; line-height: 1.4; color: #111; }
h1 { font-size: 20pt; margin: 0 0 6pt; font-weight: 600; }
h2 { font-size: 12pt; margin: 14pt 0 4pt; padding-bottom: 2pt; border-bottom: 1px solid #999;
     text-transform: uppercase; letter-spacing: .04em; }
h3 { font-size: 11pt; margin: 10pt 0 2pt; }
p { margin: 0 0 8pt; }
ul { margin: 0 0 8pt; padding-left: 16pt; }
li { margin: 0 0 3pt; }
a { color: inherit; }
"""

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+|mailto:[^\s)]+)\)")
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=True)
    escaped = _LINK.sub(lambda match: f'<a href="{match.group(2)}">{match.group(1)}</a>', escaped)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    return _ITALIC.sub(r"<em>\1</em>", escaped)


def markdown_to_html(markdown: str, *, title: str = "Document") -> str:
    """A small, escaped Markdown subset: headings, bullets, paragraphs, bold, italics, links."""
    lines = _COMMENT.sub("", markdown).replace("\r\n", "\n").split("\n")
    blocks: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if paragraph:
            # A letter's line breaks ("Sincerely,\nName") are meant, so keep them.
            blocks.append("<p>" + "<br>".join(_inline(line) for line in paragraph) + "</p>")
            paragraph.clear()
        if bullets:
            blocks.append("<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in bullets) + "</ul>")
            bullets.clear()

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            continue
        bullet = _BULLET.match(line)
        if bullet:
            if paragraph:
                flush()
            bullets.append(bullet.group(1).strip())
            continue
        if bullets:
            flush()
        paragraph.append(line.strip())
    flush()
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title><style>{PAGE_STYLE}</style></head>"
        f"<body>{''.join(blocks)}</body></html>"
    )


def pdf_renderer() -> Renderer | None:
    """A function from HTML to PDF bytes when Playwright is installed, else None."""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return None

    def render(page_html: str) -> bytes:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                context = browser.new_context(java_script_enabled=False, service_workers="block")
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                page.set_content(page_html, wait_until="load")
                return page.pdf(format="Letter", print_background=True)
            finally:
                browser.close()

    return render
