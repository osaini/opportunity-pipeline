"""Find contacts for an outreach target.

The company's own website is crawled first. An address counts as confirmed only
when it is published there, on the company's domain, and the candidate keeps
the page that shows it. Guessed addresses (first@, first.last@, ...) are made
only for people the site names, only when the domain accepts mail, and are
always labeled unverified, whatever backs them: the company's own address
format (pattern_observed) or a mail server's answer (verification, see
outreach_smtp.py) makes a guess stronger, never confirmed. Addresses printed on
other sites come from outreach_email_search.py and stay unverified too.
robots.txt is honored and the crawl is small.

choose_contact() is the one place that decides what an unattended run may use.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import sqlite3
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from uuid import uuid4

import httpx

from .outreach import _log, get_target, update_target, website_domain
from .schema import utc_now

USER_AGENT = "Mozilla/5.0 (compatible; internship-pipeline-outreach/1.0; one student's research)"
MAX_PAGES = 12
MAX_PAGE_BYTES = 2 * 1024 * 1024
# Pages rendered in a browser when the plain crawl found no one: a team page
# built by scripts reads as empty HTML.
MAX_RENDERED_PAGES = 4
PAGE_KEYWORDS = (
    ("team", 1), ("people", 1), ("leadership", 1), ("founders", 1), ("about", 2), ("company", 2),
    ("who-we-are", 2), ("our-story", 3), ("contact", 3), ("careers", 4), ("jobs", 4), ("press", 4),
    ("news", 5),
)
GENERIC_LOCAL_PARTS = {
    "info", "hello", "hi", "contact", "careers", "jobs", "team", "hr", "admin", "support", "sales",
    "press", "media", "inquiries", "enquiries", "office", "general", "recruiting", "talent",
    "internships", "people", "founders", "partners", "business",
}
ROLE_PATTERN = re.compile(
    r"\b(co-?founder|founder|ceo|cto|coo|chief|president|vp|vice president|head of|director|"
    r"lead|manager|principal|engineer|recruit\w*|talent|hiring|people operations|hr)\b",
    re.IGNORECASE,
)
# Roles most worth writing to about an internship, best first.
PREFERRED_ROLES = (
    r"hiring|recruit|talent|people operations",
    r"cto|head of engineering|vp engineering|engineering (lead|manager|director)",
    r"co-?founder|founder|ceo",
)
NAME_PATTERN = re.compile(r"^[A-Z][a-zA-Z'’-]+(?: [A-Z]\.)?(?: [A-Z][a-zA-Z'’-]+){1,2}$")
INLINE_PERSON = re.compile(r"^([A-Z][a-zA-Z'’-]+(?: [A-Z]\.)?(?: [A-Z][a-zA-Z'’-]+){1,2})\s*[,|–—-]\s*(.{2,70})$")
# The lookbehind keeps a masked address ("j***doe@acme.com", "j…doe@") from
# yielding its tail ("doe@acme.com") as if that were printed on the page.
EMAIL_PATTERN = re.compile(r"(?<![A-Za-z0-9._%+*\u2022\u2026-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
# "jane [at] acme [dot] com", "jane(at)acme.com", "jane at acme dot com".
_BRACKETED_AT = re.compile(r"\s*[\[({<]\s*at\s*[\])}>]\s*", re.IGNORECASE)
_BRACKETED_DOT = re.compile(r"\s*[\[({<]\s*dot\s*[\])}>]\s*", re.IGNORECASE)
_SPELLED_OUT = re.compile(
    r"\b([A-Za-z0-9._%+-]+)\s+at\s+([A-Za-z0-9-]+(?:\s+dot\s+[A-Za-z0-9-]+)+)\b", re.IGNORECASE,
)
_SPELLED_DOT = re.compile(r"\s+dot\s+", re.IGNORECASE)
CLOUDFLARE_PROTECTED = "/cdn-cgi/l/email-protection"
# Guess formats in the order an unattended run tries them when nothing says
# which one the company uses. Small companies most often give people first@.
PATTERN_PREFERENCE = ("first", "first.last", "flast", "firstlast", "first_last")

Resolver = Callable[[str], list[str]]


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    text: str
    error: str | None = None
    content_type: str = ""


def public_web_url_error(url: str) -> str | None:
    """Validate the URL shape before DNS is consulted."""
    try:
        parsed = urlsplit(str(url or "").strip())
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return "invalid"
        lowered = host.rstrip(".").lower()
        if lowered == "localhost" or lowered.endswith(".localhost"):
            return "private"
        try:
            ipaddress.ip_address(lowered)
        except ValueError:
            return None
        return "private"
    except ValueError:
        return "invalid"


def decode_cloudflare_email(encoded: str) -> str:
    """The address behind Cloudflare's email obfuscation (a hex XOR string), or ""."""
    try:
        data = bytes.fromhex(encoded.strip())
    except ValueError:
        return ""
    if len(data) < 2:
        return ""
    address = bytes(byte ^ data[0] for byte in data[1:]).decode("utf-8", "replace")
    return address if EMAIL_PATTERN.fullmatch(address) else ""


def deobfuscate(text: str) -> str:
    """Rewrite spelled-out addresses ("jane [at] acme [dot] com") as plain ones."""
    text = _BRACKETED_DOT.sub(".", _BRACKETED_AT.sub("@", text))

    def spelled(match: re.Match[str]) -> str:
        return f"{match.group(1)}@{_SPELLED_DOT.sub('.', match.group(2))}"

    return _SPELLED_OUT.sub(spelled, text)


def _resolve_host(host: str) -> list[str]:
    return sorted({item[4][0] for item in socket.getaddrinfo(host, None)})


class SafeFetcher:
    """Fetch public web pages with bounded bodies and checked redirect hops."""

    def __init__(self, client: httpx.Client, resolve: Resolver = _resolve_host) -> None:
        self.client = client
        self.resolve = resolve

    def __enter__(self) -> "SafeFetcher":
        self.client.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self.client.__exit__(*args)

    def fetch(
        self, url: str, *, same_host_only: bool, hop_check: Callable[[str], str | None] | None = None,
    ) -> FetchResult:
        """hop_check, when given, is asked about every URL before it is requested, redirects included."""
        start_host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        current = url
        for redirects in range(6):
            policy = public_web_url_error(current)
            if policy:
                return FetchResult(current, 0, "", policy)
            refused = hop_check(current) if hop_check is not None else None
            if refused:
                return FetchResult(current, 0, "", refused)
            host = (urlsplit(current).hostname or "").lower()
            if same_host_only and host.removeprefix("www.") != start_host:
                return FetchResult(current, 0, "", "left site")
            try:
                addresses = self.resolve(host)
                if not addresses:
                    return FetchResult(current, 0, "", "dns")
                if any(not ipaddress.ip_address(address).is_global for address in addresses):
                    return FetchResult(current, 0, "", "private")
            except Exception:
                return FetchResult(current, 0, "", "dns")
            try:
                with self.client.stream("GET", current) as response:
                    status = response.status_code
                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return FetchResult(str(response.url), status, "", "redirect")
                        if redirects == 5:
                            return FetchResult(str(response.url), status, "", "redirect")
                        current = urljoin(str(response.url), location)
                        continue
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_PAGE_BYTES:
                            return FetchResult(str(response.url), status, "", "too_large")
                        chunks.append(chunk)
                    encoding = response.encoding or "utf-8"
                    return FetchResult(
                        str(response.url), status, b"".join(chunks).decode(encoding, errors="replace"),
                        content_type=response.headers.get("content-type", ""),
                    )
            except Exception:
                return FetchResult(current, 0, "", "network")
        return FetchResult(current, 0, "", "redirect")


class _PageParser(HTMLParser):
    """Collect links, visible text lines, and JSON-LD blocks from one page."""

    BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "td", "section", "article", "span", "a"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.lines: list[str] = []
        self.json_ld: list[str] = []
        self._buffer: list[str] = []
        self._href: str | None = None
        self._anchor: list[str] = []
        self._skip = 0
        self._json_ld = False
        self._suppress: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            if tag == "script" and (attributes.get("type") or "").lower() == "application/ld+json":
                self._json_ld = True
            else:
                self._skip += 1
            return
        if tag in self.BLOCK_TAGS:
            self._flush()
        if tag == "a":
            href = attributes.get("href") or ""
            # Cloudflare hides a mailto link's address in the fragment.
            if CLOUDFLARE_PROTECTED in href and "#" in href:
                decoded = decode_cloudflare_email(href.rsplit("#", 1)[1])
                href = f"mailto:{decoded}" if decoded else href
            self._href = href
            self._anchor = []
        encoded = attributes.get("data-cfemail")
        if encoded and self._suppress is None:
            decoded = decode_cloudflare_email(encoded)
            if decoded:
                # The element's own text is the placeholder "[email protected]".
                self._buffer.append(decoded)
                if self._href is not None:
                    self._anchor.append(decoded)
                self._suppress = tag

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            if self._json_ld:
                self._json_ld = False
            elif self._skip:
                self._skip -= 1
            return
        if tag == self._suppress:
            self._suppress = None
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._anchor).split())))
            self._href = None
        if tag in self.BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._json_ld:
            self.json_ld.append(data)
            return
        if self._skip or self._suppress is not None:
            return
        self._buffer.append(data)
        if self._href is not None:
            self._anchor.append(data)

    def _flush(self) -> None:
        text = deobfuscate(" ".join("".join(self._buffer).split()))
        if text:
            self.lines.append(text)
        self._buffer = []

    def close(self) -> None:
        super().close()
        self._flush()


def default_client() -> httpx.Client:
    return httpx.Client(
        timeout=10.0,
        follow_redirects=False,
        verify=True,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )


def default_fetcher() -> SafeFetcher:
    return SafeFetcher(default_client())


def _same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == domain or host.endswith(f".{domain}")


def _email_on_domain(email: str, domain: str) -> bool:
    email_domain = email.rsplit("@", 1)[-1].lower()
    return email_domain == domain or email_domain.endswith(f".{domain}")


def mail_domain_accepts(client: httpx.Client, domain: str) -> bool | None:
    """MX lookup over DNS-over-HTTPS. None when the lookup itself failed."""
    try:
        response = client.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": domain, "type": "MX"},
            headers={"Accept": "application/dns-json"},
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if payload.get("Status") != 0:
        return False
    return any(answer.get("type") == 15 for answer in payload.get("Answer") or [])


def _page_priority(url: str, text: str, keywords: tuple[tuple[str, int], ...] = PAGE_KEYWORDS) -> int | None:
    haystack = f"{urlsplit(url).path} {text}".lower()
    ranks = [rank for keyword, rank in keywords if keyword in haystack]
    return min(ranks) if ranks else None


def _role_rank(role: str) -> int:
    for index, pattern in enumerate(PREFERRED_ROLES):
        if re.search(pattern, role, re.IGNORECASE):
            return index
    return len(PREFERRED_ROLES)


def _people_from_page(parser: _PageParser) -> list[dict[str, str]]:
    people: list[dict[str, str]] = []
    for block in parser.json_ld:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("@type") == "Person" and node.get("name"):
                    people.append({
                        "name": html.unescape(str(node["name"]).strip()),
                        "role": html.unescape(str(node.get("jobTitle") or "").strip()),
                        "email": str(node.get("email") or "").removeprefix("mailto:").strip(),
                    })
                stack.extend(value for value in node.values() if isinstance(value, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)
    lines = parser.lines
    for index, line in enumerate(lines):
        inline = INLINE_PERSON.match(line)
        if inline and ROLE_PATTERN.search(inline.group(2)):
            people.append({"name": inline.group(1), "role": inline.group(2).strip(), "email": ""})
            continue
        if NAME_PATTERN.match(line) and index + 1 < len(lines):
            following = lines[index + 1]
            if len(following) <= 80 and ROLE_PATTERN.search(following) and not NAME_PATTERN.match(following):
                people.append({"name": line, "role": following, "email": ""})
    by_name = {person["name"].casefold(): person for person in people}
    for href, anchor in parser.links:
        address = href[7:].split("?", 1)[0].strip().lower() if href.lower().startswith("mailto:") else ""
        if address and EMAIL_PATTERN.fullmatch(address) and NAME_PATTERN.fullmatch(anchor):
            person = by_name.get(anchor.casefold()) or {"name": anchor, "role": "", "email": ""}
            person["email"] = address
            if anchor.casefold() not in by_name:
                people.append(person)
                by_name[anchor.casefold()] = person
    for index, line in enumerate(lines):
        name = ""
        email_index = index + 1
        inline = INLINE_PERSON.match(line)
        if inline and ROLE_PATTERN.search(inline.group(2)):
            name = inline.group(1)
        elif NAME_PATTERN.fullmatch(line):
            name = line
            if index + 1 < len(lines) and ROLE_PATTERN.search(lines[index + 1]):
                email_index += 1
        if not name or email_index >= len(lines):
            continue
        addresses = EMAIL_PATTERN.findall(lines[email_index])
        for href, anchor in parser.links:
            if anchor == lines[email_index] and href.lower().startswith("mailto:"):
                addresses.append(href[7:].split("?", 1)[0].strip())
        if addresses:
            person = by_name.get(name.casefold())
            if person is not None:
                person["email"] = addresses[0].strip(".").lower()
    return people


def _emails_from_page(parser: _PageParser) -> set[str]:
    found = set()
    for href, _text in parser.links:
        if href.lower().startswith("mailto:"):
            address = href[7:].split("?", 1)[0].strip()
            if EMAIL_PATTERN.fullmatch(address):
                found.add(address)
    for line in parser.lines:
        found.update(EMAIL_PATTERN.findall(html.unescape(line)))
    for person in _people_from_page(parser):
        if person["email"]:
            found.add(person["email"])
    return {email.strip(".").lower() for email in found if not email.lower().endswith(IMAGE_SUFFIXES)}


def _name_parts(name: str) -> tuple[str, str]:
    parts = [re.sub(r"[^a-z]", "", part.lower()) for part in name.split() if not part.endswith(".")]
    parts = [part for part in parts if part]
    return (parts[0], parts[-1]) if len(parts) >= 2 else ("", "")


PATTERNS = {
    "first": lambda first, last: first,
    "first.last": lambda first, last: f"{first}.{last}",
    "flast": lambda first, last: f"{first[0]}{last}",
    "firstlast": lambda first, last: f"{first}{last}",
    "first_last": lambda first, last: f"{first}_{last}",
}


def _pattern_of(email: str, name: str) -> str | None:
    first, last = _name_parts(name)
    if not first:
        return None
    local = email.split("@", 1)[0].lower()
    for pattern, build in PATTERNS.items():
        if build(first, last) == local:
            return pattern
    return None


def site_robots(start: str, fetcher: SafeFetcher) -> RobotFileParser:
    """The site's robots.txt rules; a missing or unreadable file allows everything."""
    robots = RobotFileParser()
    response = fetcher.fetch(urljoin(start, "/robots.txt"), same_host_only=True)
    robots.parse(response.text.splitlines() if not response.error and response.status == 200 else [])
    return robots


def crawl_site(
    website: str,
    *,
    fetcher: SafeFetcher,
    delay: float = 1.0,
    keywords: tuple[tuple[str, int], ...] = PAGE_KEYWORDS,
    max_pages: int = MAX_PAGES,
) -> dict[str, Any]:
    """Read up to max_pages same-site pages, following links that match keywords (people, by default)."""
    domain = website_domain(website)
    if not domain:
        raise ValueError("Add the company website before finding contacts")
    parsed = urlsplit(website if "//" in website else f"https://{website}")
    start = f"{parsed.scheme}://{parsed.netloc}/"
    robots = site_robots(start, fetcher)

    queue: list[tuple[int, str]] = [(0, start)]
    if parsed.path and parsed.path != "/":
        queue.append((0, urljoin(start, parsed.path)))
    seen: set[str] = set()
    pages: list[dict[str, Any]] = []
    blocked: list[str] = []
    errors: list[str] = []
    while queue and len(pages) < max_pages:
        queue.sort()
        _rank, url = queue.pop(0)
        normalized = url.split("#", 1)[0].rstrip("/") or url
        if normalized in seen:
            continue
        seen.add(normalized)
        if not robots.can_fetch(USER_AGENT, url):
            blocked.append(url)
            continue
        if pages and delay:
            time.sleep(delay)
        response = fetcher.fetch(url, same_host_only=True)
        if response.error:
            errors.append(f"{url}: {response.error}")
            continue
        final_url = response.url
        if response.status >= 400 or not _same_site(final_url, domain):
            errors.append(f"{url}: HTTP {response.status}" if response.status >= 400 else f"{url}: left the company site")
            continue
        if response.content_type and "html" not in response.content_type.lower():
            continue
        raw = response.text
        parser = _PageParser()
        parser.feed(raw)
        parser.close()
        pages.append({"url": final_url, "parser": parser, "raw": raw})
        for href, text in parser.links:
            absolute = urljoin(final_url, href)
            if not absolute.startswith(("http://", "https://")) or not _same_site(absolute, domain):
                continue
            rank = _page_priority(absolute, text, keywords)
            if rank is not None and absolute.split("#", 1)[0].rstrip("/") not in seen:
                queue.append((rank, absolute))
    return {"domain": domain, "pages": pages, "blocked": blocked, "errors": errors}




def _collect(pages: list[dict[str, Any]], domain: str) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """The people each page names and every on-domain address, with the page that shows it."""
    people: dict[str, dict[str, Any]] = {}
    emails: dict[str, str] = {}
    for page in pages:
        for person in _people_from_page(page["parser"]):
            key = person["name"].casefold()
            existing = people.get(key)
            if existing is None or (not existing["role"] and person["role"]):
                people[key] = {**person, "email": existing["email"] if existing else person["email"], "evidence_url": page["url"]}
            if person["email"] and _email_on_domain(person["email"], domain):
                people[key]["email"] = person["email"].lower()
                emails.setdefault(person["email"].lower(), page["url"])
        for address in _emails_from_page(page["parser"]):
            if _email_on_domain(address, domain):
                emails.setdefault(address, page["url"])
    return people, emails


def _is_generic(address: str) -> bool:
    return address.split("@", 1)[0].lower() in GENERIC_LOCAL_PARTS


def _guess_note(pattern: str, observed_example: str) -> str:
    if observed_example:
        return f"Their site publishes a personal address in the {pattern}@ format"
    return f"A common format ({pattern}@); their site shows no personal address to learn theirs from"


def candidates_from_pages(pages: list[dict[str, Any]], domain: str, *, mail_ok: bool | None) -> list[dict[str, Any]]:
    """Turn what the pages publish into contact candidates, unsorted and unverified by any mail server."""
    people, emails = _collect(pages, domain)
    candidates: list[dict[str, Any]] = []
    matched: set[str] = set()
    observed: list[tuple[str, str]] = []
    for address, evidence in emails.items():
        owner = next((person for person in people.values() if person.get("email") == address), None)
        if _is_generic(address):
            local = address.split("@", 1)[0]
            candidates.append({"name": "", "role": f"{local}@ inbox", "email": address, "method": "site_generic",
                               "confidence": "confirmed", "evidence_url": evidence})
            continue
        if owner:
            matched.add(owner["name"].casefold())
            pattern = _pattern_of(address, owner["name"])
            if pattern:
                observed.append((pattern, address))
        candidates.append({
            "name": owner["name"] if owner else "",
            "role": owner["role"] if owner else "",
            "email": address,
            "method": "site_published",
            "confidence": "confirmed",
            "evidence_url": evidence,
        })

    ranked_people = sorted(
        (person for key, person in people.items() if key not in matched),
        key=lambda person: _role_rank(person["role"]),
    )
    if mail_ok:
        pattern, example = observed[0] if observed else ("", "")
        patterns = [pattern] if pattern else list(PATTERN_PREFERENCE[:3])
        for person in ranked_people[:3]:
            first, last = _name_parts(person["name"])
            if not first:
                continue
            for each in patterns:
                candidates.append({
                    "name": person["name"],
                    "role": person["role"],
                    "email": f"{PATTERNS[each](first, last)}@{domain}",
                    "method": "pattern_guess",
                    "confidence": "unverified",
                    "evidence_url": person["evidence_url"],
                    "pattern_observed": bool(pattern),
                    "note": _guess_note(each, example),
                })
    for person in ranked_people:
        if not any(candidate["name"] == person["name"] for candidate in candidates):
            candidates.append({
                "name": person["name"], "role": person["role"], "email": "", "method": "site_person",
                "confidence": "unknown", "evidence_url": person["evidence_url"],
            })
    for candidate in candidates:
        candidate.setdefault("verification", "")
        candidate.setdefault("pattern_observed", False)
        candidate.setdefault("note", "")
    return candidates


METHOD_ORDER = {"site_published": 0, "site_generic": 1, "published_elsewhere": 2, "pattern_guess": 3}
VERIFICATION_ORDER = {"smtp_accepted": 0, "": 1, "catch_all": 1, "smtp_unknown": 1, "smtp_rejected": 2}


def _has_personal_address(people: dict[str, dict[str, Any]], emails: dict[str, str]) -> bool:
    return bool(people) or any(not _is_generic(address) for address in emails)


def discover_candidates(
    website: str,
    *,
    fetcher: SafeFetcher,
    delay: float = 1.0,
    renderer: Any = None,
    verifier: Any = None,
) -> dict[str, Any]:
    """Crawl the site and turn what it publishes into ranked contact candidates.

    When the plain pages name no one and a renderer is given, the people pages
    are read again in a browser. When a verifier is given, the guessed
    addresses are put to the domain's mail server (see outreach_smtp.py).
    """
    crawl = crawl_site(website, fetcher=fetcher, delay=delay)
    domain = crawl["domain"]
    pages = list(crawl["pages"])
    rendered = False
    if renderer is not None and not _has_personal_address(*_collect(pages, domain)):
        # Imported here: outreach_profile imports this module.
        from .outreach_profile import rendered_pages

        extra = rendered_pages(website, renderer=renderer, fetcher=fetcher, keywords=PAGE_KEYWORDS, max_pages=MAX_RENDERED_PAGES)
        if extra:
            pages.extend(extra)
            rendered = True

    mail_ok = mail_domain_accepts(fetcher.client, domain)
    candidates = candidates_from_pages(pages, domain, mail_ok=mail_ok)
    guesses = [candidate for candidate in candidates if candidate["method"] == "pattern_guess"]
    if verifier is not None and guesses:
        answers = verifier.check(domain, [candidate["email"] for candidate in guesses])
        for candidate in guesses:
            candidate["verification"] = answers.get(candidate["email"], "")

    candidates.sort(key=lambda item: (
        item["confidence"] == "unknown", METHOD_ORDER.get(item["method"], 4),
        VERIFICATION_ORDER.get(item["verification"], 1), _role_rank(item["role"]) if item["name"] else 9,
    ))
    return {
        "domain": domain,
        "candidates": candidates[:25],
        "pages_checked": [page["url"] for page in pages],
        "blocked_by_robots": crawl["blocked"],
        "errors": crawl["errors"],
        "mail_domain_ok": mail_ok,
        "rendered": rendered,
        "pages": pages,
    }


CANDIDATE_COLUMNS = "id, name, role, email, method, confidence, evidence_url, verification, pattern_observed, note, created_at"


def _candidate(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["pattern_observed"] = bool(item.get("pattern_observed"))
    return item


def list_candidates(conn: sqlite3.Connection, target_id: str, *, user_id: str) -> list[dict[str, Any]]:
    get_target(conn, target_id, user_id=user_id)
    conn.row_factory = sqlite3.Row
    return [
        _candidate(row)
        for row in conn.execute(
            f"""
            SELECT {CANDIDATE_COLUMNS}
            FROM outreach_contact_candidates WHERE target_id=? AND user_id=?
            ORDER BY CASE confidence WHEN 'confirmed' THEN 0 WHEN 'unverified' THEN 1 ELSE 2 END,
                     CASE method WHEN 'site_published' THEN 0 WHEN 'site_generic' THEN 1
                                 WHEN 'published_elsewhere' THEN 2 WHEN 'pattern_guess' THEN 3 ELSE 4 END,
                     CASE verification WHEN 'smtp_accepted' THEN 0 WHEN 'smtp_rejected' THEN 2 ELSE 1 END,
                     created_at
            """,
            (target_id, user_id),
        ).fetchall()
    ]


def store_candidate(conn: sqlite3.Connection, target_id: str, user_id: str, candidate: dict[str, Any], timestamp: str) -> None:
    """Insert one candidate. Stronger evidence replaces a weaker row for the same person and address.

    A confirmed address replaces anything; an address printed on another page
    replaces a guess of the same address. Nothing replaces a stronger row.
    """
    conn.execute(
        """
        INSERT INTO outreach_contact_candidates(
            id, target_id, user_id, name, role, email, method, confidence, evidence_url,
            verification, pattern_observed, note, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_id, email, name) DO UPDATE SET
            method=excluded.method, confidence=excluded.confidence, evidence_url=excluded.evidence_url,
            verification=excluded.verification, pattern_observed=excluded.pattern_observed, note=excluded.note
        WHERE excluded.confidence='confirmed'
           OR (excluded.method='published_elsewhere' AND outreach_contact_candidates.method='pattern_guess')
           OR (excluded.method=outreach_contact_candidates.method AND outreach_contact_candidates.confidence<>'confirmed')
        """,
        (f"candidate-{uuid4().hex}", target_id, user_id, candidate["name"][:200], candidate["role"][:200],
         candidate["email"][:320], candidate["method"], candidate["confidence"], candidate["evidence_url"][:500],
         candidate.get("verification", ""), int(bool(candidate.get("pattern_observed"))),
         str(candidate.get("note", ""))[:500], timestamp),
    )
    if candidate.get("verification") and candidate["email"]:
        # What a mail server said is about the address, whoever it is filed under,
        # so the latest answer replaces an older one on every row for it.
        conn.execute(
            "UPDATE outreach_contact_candidates SET verification=? WHERE target_id=? AND user_id=? AND email=?",
            (candidate["verification"], target_id, user_id, candidate["email"][:320]),
        )


# Candidates a re-run of the site crawl does not produce, so it must not delete.
KEPT_ACROSS_CRAWLS = ("ai_research", "published_elsewhere")


def find_contacts(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    fetcher: SafeFetcher,
    delay: float = 1.0,
    renderer: Any = None,
    verifier: Any = None,
) -> dict[str, Any]:
    target = get_target(conn, target_id, user_id=user_id)
    if not target["website"]:
        raise ValueError("Add the company website before finding contacts")
    result = discover_candidates(target["website"], fetcher=fetcher, delay=delay, renderer=renderer, verifier=verifier)
    timestamp = utc_now()
    with conn:
        # A re-run replaces what the site said last time; research from elsewhere stays.
        conn.execute(
            f"DELETE FROM outreach_contact_candidates WHERE target_id=? AND user_id=? "
            f"AND method NOT IN ({', '.join('?' for _ in KEPT_ACROSS_CRAWLS)})",
            (target_id, user_id, *KEPT_ACROSS_CRAWLS),
        )
        for candidate in result["candidates"]:
            store_candidate(conn, target_id, user_id, candidate, timestamp)
        conn.execute(
            "UPDATE outreach_targets SET mail_domain_ok=?, updated_at=? WHERE id=? AND user_id=?",
            (None if result["mail_domain_ok"] is None else int(result["mail_domain_ok"]), timestamp, target_id, user_id),
        )
        _log(conn, target_id, user_id, "contacts_searched",
             detail=f"{len(result['candidates'])} candidates from {len(result['pages_checked'])} pages"
             + (" (rendered in a browser)" if result["rendered"] else ""))
    from .outreach_profile import record_site_location

    # The pages already read for people often state where the company is.
    location = record_site_location(conn, target_id, user_id=user_id, pages=result["pages"])
    if result["rendered"]:
        location = {**location, "rendered": True}
    return {
        "candidates": list_candidates(conn, target_id, user_id=user_id),
        "pages_checked": result["pages_checked"],
        "blocked_by_robots": result["blocked_by_robots"],
        "errors": result["errors"],
        "mail_domain_ok": result["mail_domain_ok"],
        "rendered": result["rendered"],
        "location": location,
    }


VERIFICATION_WORDS = {
    "smtp_accepted": "their mail server accepted it",
    "smtp_rejected": "their mail server said it does not exist",
    "catch_all": "their mail server accepts any address, which proves nothing",
    "smtp_unknown": "their mail server gave no usable answer",
}


def contact_route(row: dict[str, Any], *, cc: str = "") -> str:
    """How the address was found, in words, for the contact_route field."""
    evidence = row["evidence_url"]
    if row["method"] == "pattern_guess":
        details = [part for part in (row.get("note", ""), VERIFICATION_WORDS.get(row.get("verification", ""), "")) if part]
        route = f"Guessed address from the name on {evidence}"
        route += (f"; {'; '.join(details)}" if details else "") + "; not confirmed"
    elif row["method"] == "published_elsewhere":
        route = f"Address printed on {evidence}, not on the company's own site; not confirmed"
        if row.get("verification") in VERIFICATION_WORDS:
            route += f"; {VERIFICATION_WORDS[row['verification']]}"
    elif row["method"] == "site_generic":
        route = f"Shared inbox published on {evidence}"
    else:
        route = f"Address published on {evidence}"
    if cc:
        route += f". Cc {cc} so a wrong guess still reaches the company"
    return route


def apply_candidate(
    conn: sqlite3.Connection,
    target_id: str,
    candidate_id: str,
    *,
    user_id: str,
    cc_candidate_id: str | None = None,
) -> dict[str, Any]:
    """Make one candidate the contact. A Cc is set only when named; applying by hand clears it."""
    target = get_target(conn, target_id, user_id=user_id)
    conn.row_factory = sqlite3.Row

    def load(candidate_id: str) -> dict[str, Any]:
        row = conn.execute(
            f"SELECT {CANDIDATE_COLUMNS} FROM outreach_contact_candidates WHERE id=? AND target_id=? AND user_id=?",
            (candidate_id, target_id, user_id),
        ).fetchone()
        if not row:
            raise LookupError(candidate_id)
        return _candidate(row)

    row = load(candidate_id)
    if not row["email"]:
        raise ValueError("This person has no published or guessable address; reach them another way")
    cc = ""
    if cc_candidate_id:
        cc_row = load(cc_candidate_id)
        if cc_row["email"] and cc_row["email"] != row["email"]:
            cc = cc_row["email"]
    changes: dict[str, Any] = {
        "contact_email": row["email"],
        "contact_cc": cc,
        "contact_confidence": row["confidence"],
        "contact_evidence_url": row["evidence_url"],
    }
    # The name, role, and confidence always describe the same address. A shared
    # inbox has no person behind it, so a previously named person is cleared
    # rather than shown as the confirmed owner of info@ or careers@.
    changes["contact_name"] = row["name"]
    changes["contact_role"] = row["role"] if row["name"] else ""
    route = contact_route(row, cc=cc)
    if row["method"] == "site_generic" and target["contact_name"] and not row["name"]:
        person = f"{target['contact_name']} ({target['contact_role']})" if target["contact_role"] else target["contact_name"]
        route += f". {person} is named on the site but has no published address"
    changes["contact_route"] = route
    if row["evidence_url"] and row["evidence_url"] not in target["source_urls"]:
        changes["source_urls"] = [*target["source_urls"], row["evidence_url"]]
    update_target(conn, target_id, changes, user_id=user_id)
    with conn:
        _log(conn, target_id, user_id, "contact_applied",
             detail=f"{row['email']} ({row['confidence']}, {row['method'].replace('_', ' ')})" + (f", cc {cc}" if cc else ""))
    return get_target(conn, target_id, user_id=user_id)


# The shared inbox worth writing to about an internship, best first. Any other
# generic inbox (press@, sales@, support@) comes after these.
INBOX_PREFERENCE = (
    "careers", "jobs", "internships", "recruiting", "talent", "hr", "people",
    "hello", "hi", "info", "contact", "team", "founders", "general", "office", "inquiries", "enquiries",
)


def guess_strength(candidate: dict[str, Any]) -> int | None:
    """How far an unverified address can be trusted: 0 strong, 1 good, 2 weak, None never unattended.

    A guess the mail server refused is never used. An address printed on
    another page, or one the server accepted, is strong. A guess in the format
    the company's own addresses use is good. Any other guess is weak.
    """
    if not candidate.get("email") or candidate.get("confidence") == "confirmed":
        return None
    if candidate.get("verification") == "smtp_rejected":
        return None
    method = candidate.get("method")
    if method == "published_elsewhere":
        return 0
    if method != "pattern_guess":
        return None
    if candidate.get("verification") == "smtp_accepted":
        return 0
    if candidate.get("pattern_observed"):
        return 1
    return 2


def _pattern_rank(candidate: dict[str, Any]) -> int:
    pattern = _pattern_of(candidate["email"], candidate.get("name", ""))
    return PATTERN_PREFERENCE.index(pattern) if pattern in PATTERN_PREFERENCE else len(PATTERN_PREFERENCE)


def _inbox_rank(candidate: dict[str, Any]) -> int:
    local = candidate["email"].split("@", 1)[0].lower()
    return INBOX_PREFERENCE.index(local) if local in INBOX_PREFERENCE else len(INBOX_PREFERENCE)


def choose_contact(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What an unattended run may address a draft to, or None.

    In order:
    1. A confirmed personal address from the company's site. No Cc.
    2. The strongest unverified address (see guess_strength), one per target,
       with the best shared inbox in Cc so a wrong guess still arrives once.
    3. A weak guess, only when there is a shared inbox to Cc.
    4. The best shared inbox alone.
    A guess is never relabeled: the target's contact stays unverified.
    """
    usable = [candidate for candidate in candidates if candidate.get("email")]
    personal = [
        candidate for candidate in usable
        if candidate["method"] == "site_published" and candidate["confidence"] == "confirmed" and not _is_generic(candidate["email"])
    ]
    if personal:
        best = min(personal, key=lambda item: _role_rank(item["role"]) if item["name"] else len(PREFERRED_ROLES) + 1)
        return {"to": best, "cc": None, "basis": "confirmed"}
    inboxes = sorted(
        (candidate for candidate in usable if candidate["method"] == "site_generic" and candidate["confidence"] == "confirmed"),
        key=_inbox_rank,
    )
    inbox = inboxes[0] if inboxes else None
    scored = [(strength, candidate) for candidate in usable if (strength := guess_strength(candidate)) is not None]
    strong = [(strength, candidate) for strength, candidate in scored if strength <= 1]
    if strong:
        _, best = min(strong, key=lambda pair: (pair[0], _role_rank(pair[1]["role"]), _pattern_rank(pair[1])))
        return {"to": best, "cc": inbox, "basis": "strong_guess"}
    weak = [candidate for strength, candidate in scored if strength == 2]
    if weak and inbox is not None:
        best = min(weak, key=lambda item: (_role_rank(item["role"]), _pattern_rank(item)))
        return {"to": best, "cc": inbox, "basis": "weak_guess"}
    if inbox is not None:
        return {"to": inbox, "cc": None, "basis": "shared_inbox"}
    return None


def apply_choice(conn: sqlite3.Connection, target_id: str, choice: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    return apply_candidate(
        conn, target_id, choice["to"]["id"], user_id=user_id,
        cc_candidate_id=choice["cc"]["id"] if choice.get("cc") else None,
    )
