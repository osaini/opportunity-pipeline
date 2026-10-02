"""Reach a company through the contact form on its own site when it publishes no email.

Finding: the crawl that looks for addresses (outreach_contacts.find_contacts)
also looks for a contact form: a form with a message box and an email field,
never a search, sign-up, or login form. The best one is kept per target
(outreach_contact_forms), with the page it is on and any CAPTCHA seen.

Submitting: the approved first email goes in, as the student, in a real
Chromium through Playwright. The form is read again on the live page (many are
built by scripts, some inside a frame) and every field is matched to what the
student has confirmed. A field the app cannot answer truthfully stops it before
anything is sent, as does a message longer than the box allows; a hidden field
(a spam trap) is never filled, and an optional phone number, school, or link is
left blank. A checkbox CAPTCHA is clicked like a person would; one that asks
for a picture challenge is left to the student, in a browser window they can
see (``in_browser``). Nothing disguises the browser.

Each company's first message goes out once, through the same claim the Gmail
send uses (outreach_send_claims, action 'form'). The outcome is recorded
honestly: 'submitted' only when the page says it arrived; 'unconfirmed' when
the form was sent but the page did not say so, which is never sent again
without the student saying so; 'needs_you' and 'failed' when nothing was sent.
A submitted form moves the company to Sent exactly as an email would, and its
replies are read from Gmail by the company's domain (outreach/inbox.py).

The worker's submissions are automatic, so the student's pause stops them: in
the claim's own transaction, and once more just before the send button is
pressed. That last check and moving the claim to 'clicking' are one
transaction that starts with automation.pause_guard, so a pause either lands
first and nothing is pressed, or lands after and reports the form as in flight
(automation.in_flight). A claim left 'clicking' (the app stopped mid-press)
may have sent the form, so it is treated like 'unconfirmed'. A form stopped by
a pause stays 'found' and goes when they resume. The student's own Send
through the form is never stopped by a pause.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from .. import ROOT
from ..automation import ledger as automation
from .contact_names import NO_REPLY_SENDER, website_domain
from .targets import UNSENT_STATUSES, DraftChangedError, list_targets, log_event, get_target, update_target, validate_web_url
from .location import missing_location_message
from .config import sender_account
from .gmail import SendNeedsCheckError, attachment_path, attachment_problem
from .send_claims import (
    IN_PROGRESS,
    SendConflictError,
    send_claim_row,
    send_claim_held,
    claimed_send,
    settle_send_claim,
)
from .render import request_allowed
from ..student.preparation import confirmed_facts
from ..core.timestamps import utc_now
from ..integrations.web_fetch import USER_AGENT, Resolver, close_browser, resolve_host, same_site, site_robots

FORM_STATES = ("found", "submitted", "unconfirmed", "needs_you", "failed")
SUBMITTED_EVENT = "form_submitted"
UNCONFIRMED_EVENT = "form_unconfirmed"
NOT_SENT_EVENT = "form_not_sent"
SCREENSHOT_DIR = ROOT / "data" / "private" / "outreach-forms"
# How long a student has to solve a picture challenge in the visible browser.
PERSON_WAIT_SECONDS = 180
NAVIGATION_TIMEOUT_MS = 30_000
# How long after the click the page has to say the message arrived.
CONFIRM_WAIT_SECONDS = 15
CAPTCHA_WAIT_SECONDS = 12
# A field that cannot take text within this is covered or read-only; waiting longer never helps.
FIELD_TIMEOUT_MS = 5_000
NEWLINE = chr(10)
CRLF = chr(13) + chr(10)

CAPTCHA_MARKERS = (
    ("recaptcha", re.compile(r"g-recaptcha|google\.com/recaptcha|recaptcha/api\.js|recaptcha/enterprise", re.IGNORECASE)),
    ("hcaptcha", re.compile(r"h-captcha|hcaptcha\.com", re.IGNORECASE)),
    ("turnstile", re.compile(r"cf-turnstile|challenges\.cloudflare\.com/turnstile", re.IGNORECASE)),
)
# Script-built forms the plain HTML does not show, but a browser will.
EMBEDDED_FORMS = re.compile(r"hsforms\.(net|com)|hbspt\.forms\.create", re.IGNORECASE)
# Links to the page a contact form usually lives on.
CONTACT_LINK = re.compile(r"contact|get in touch|talk to us|reach us|say hello|work with us", re.IGNORECASE)
# Pages looked at beyond the crawl when it found no form: fetched, then rendered.
MAX_FORM_PAGES = 2
# A form for buying, not writing: using it for a student's email would misuse it.
SALES_FORM = re.compile(
    r"\b(request|book|schedule|get) (a |your )?(demo|quote|pricing)\b|\brequest (a )?(call ?back|consultation)\b|\bget a quote\b",
    re.IGNORECASE,
)
NOT_CONTACT = re.compile(r"search|subscri|newsletter|sign[\s_-]?up|log[\s_-]?in|sign[\s_-]?in|password|checkout|cart|comment-?form|wp-comments", re.IGNORECASE)
# The page saying the message arrived, in the words sites use.
SUCCESS_TEXT = re.compile(
    r"thank(s| you)\b[^.!\n]{0,80}\b(message|contact|reach|submi|inquir|enquir|writing|getting in touch|interest)"
    r"|\b(message|form|inquiry|enquiry|submission|request)\b[^.!\n]{0,40}\b(has been|was|been)\b[^.!\n]{0,20}\b(sent|received|submitted|delivered)"
    r"|\bwe('ll| will)\b[^.!\n]{0,40}\b(be in touch|get back|respond|reply|contact you)"
    r"|\bsuccessfully (sent|submitted)\b",
    re.IGNORECASE,
)
ERROR_TEXT = re.compile(r"\b(error|failed|invalid|please (fill|complete|enter|correct)|is required|required field|try again)\b", re.IGNORECASE)
# An automatic answer to a form, not a person replying. People also write "thank
# you for reaching out", so those words count only straight after the form went.
ACKNOWLEDGEMENT = re.compile(
    r"we('ve| have)? received your (message|inquiry|enquiry|submission|request|note)"
    r"|thank(s| you) for (contacting|reaching out|getting in touch|your (message|inquiry|enquiry|submission|interest|note))",
    re.IGNORECASE,
)
ALWAYS_AUTOMATIC = re.compile(r"copy of your (submission|message)|this is an automated|do not reply to this", re.IGNORECASE)
ACKNOWLEDGEMENT_WINDOW_MINUTES = 15
PAUSED_BEFORE_SENDING = "Paused before sending; nothing was sent"


# --- Finding the form in a crawled page ----------------------------------------------


class _FormParser(HTMLParser):
    """Every <form> on a page, with its fields and the text of the labels naming them."""

    FIELD_TAGS = {"input", "textarea", "select"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self.labels: dict[str, str] = {}
        self._form: dict[str, Any] | None = None
        self._label_for: str | None = None
        self._label_text: list[str] = []
        self._label_fields: list[dict[str, Any]] = []
        self._in_label = False
        self.loose_textarea = False
        self.loose_email = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "form":
            self._form = {
                "action": attributes.get("action", ""), "method": attributes.get("method", "get").lower(),
                "id": attributes.get("id", ""), "class": attributes.get("class", ""), "fields": [],
            }
            self.forms.append(self._form)
        elif tag == "label":
            self._in_label = True
            self._label_for = attributes.get("for") or None
            self._label_text = []
            self._label_fields = []
        elif tag in self.FIELD_TAGS and self._form is None:
            # Fields a script wires up without a <form> element around them.
            kind = (attributes.get("type") or "").lower()
            text = " ".join(attributes.get(key, "") for key in ("name", "id", "placeholder", "aria-label")).lower()
            self.loose_textarea = self.loose_textarea or tag == "textarea"
            self.loose_email = self.loose_email or kind == "email" or (tag == "input" and "email" in text)
        elif tag in self.FIELD_TAGS and self._form is not None:
            field = {
                "tag": tag, "type": (attributes.get("type") or ("textarea" if tag == "textarea" else "text")).lower(),
                "name": attributes.get("name", ""), "id": attributes.get("id", ""),
                "placeholder": attributes.get("placeholder", ""), "aria_label": attributes.get("aria-label", ""),
                "required": "required" in attributes or attributes.get("aria-required") == "true",
                "maxlength": int(attributes["maxlength"]) if attributes.get("maxlength", "").isdigit() else None,
                "accept": attributes.get("accept", ""), "label": "",
            }
            self._form["fields"].append(field)
            if self._in_label:
                self._label_fields.append(field)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None
        elif tag == "label" and self._in_label:
            text = " ".join("".join(self._label_text).split())
            if self._label_for:
                self.labels[self._label_for] = text
            for field in self._label_fields:
                field["label"] = field["label"] or text
            self._in_label = False

    def handle_data(self, data: str) -> None:
        if self._in_label:
            self._label_text.append(data)


def _field_text(field: dict[str, Any]) -> str:
    """The words that name a field. The autocomplete hint is read exactly (AUTOCOMPLETE_ROLES), never searched:
    "country-name" is not a name and "organization-title" is not a company.
    """
    return " ".join(str(field.get(key) or "") for key in ("label", "name", "id", "placeholder", "aria_label")).lower()


# The browser's own autofill hints, the most reliable thing a form says about a field.
# The last token counts ("section-1 shipping email" is an email).
AUTOCOMPLETE_ROLES = {
    "name": "full_name", "given-name": "first_name", "family-name": "last_name", "email": "email",
    "tel": "phone", "tel-national": "phone", "organization": "company", "organization-title": "job_title", "url": "link",
}
# Hints for things the app never fills in for the student.
AUTOCOMPLETE_NEVER = re.compile(r"^(country|country-name|address|address-line\d|address-level\d|street-address|postal-code|bday.*|sex|"
                                r"honorific-.*|additional-name|nickname|username|new-password|current-password|cc-.*|transaction-.*|language)$")


def _autocomplete(field: dict[str, Any]) -> str:
    tokens = str(field.get("autocomplete") or "").lower().split()
    return tokens[-1] if tokens else ""


def _is_email_field(field: dict[str, Any]) -> bool:
    if field["type"] == "email" or _autocomplete(field) == "email":
        return True
    if field["tag"] != "input" or field["type"] not in {"text", ""}:
        return False
    # A placeholder like "Subject of your email" does not make a field an email field; its label and name do.
    named = " ".join(str(field.get(key) or "") for key in ("label", "name", "id", "aria_label")).lower()
    return "email" in named if named.strip() else "email" in str(field.get("placeholder") or "").lower()


def detect_captcha(raw: str) -> str:
    return next((name for name, pattern in CAPTCHA_MARKERS if pattern.search(raw)), "")


def contact_forms(url: str, raw: str) -> list[dict[str, Any]]:
    """The contact forms in one page's HTML, best first: a message box and an email field, and nothing else it could be."""
    parser = _FormParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001 - a page too broken to parse holds no form we can use
        return []
    found = []
    for form in parser.forms:
        for field in form["fields"]:
            field["label"] = field["label"] or parser.labels.get(field["id"], "")
        fields = form["fields"]
        if any(field["type"] == "password" for field in fields):
            continue
        if NOT_CONTACT.search(" ".join((form["action"], form["id"], form["class"]))):
            continue
        if not any(field["tag"] == "textarea" for field in fields) or not any(_is_email_field(field) for field in fields):
            continue
        visible = [
            {key: field[key] for key in ("tag", "type", "name", "label", "required", "maxlength")}
            for field in fields if field["type"] not in {"hidden", "submit", "button", "image", "reset"}
        ]
        found.append({
            "page_url": url, "fields": visible,
            "accepts_file": any(field["type"] == "file" for field in fields),
            "rank": 0 if "contact" in url.lower() else 1,
        })
    if not found and ((parser.loose_textarea and parser.loose_email) or EMBEDDED_FORMS.search(raw)):
        # A HubSpot form, or fields with no <form> around them, are wired up by a
        # script; their fields are read on the live page when sending.
        found.append({"page_url": url, "fields": [], "accepts_file": False, "rank": 2})
    captcha = detect_captcha(raw)
    for form in found:
        form["captcha"] = captcha
    return sorted(found, key=lambda form: form["rank"])


def best_contact_form(pages: list[dict[str, Any]]) -> dict[str, Any] | None:
    forms = [form for page in pages for form in contact_forms(page["url"], page.get("raw") or "")]
    return min(forms, key=lambda form: form["rank"]) if forms else None


def find_contact_form(pages: list[dict[str, Any]], *, fetcher: Any = None, renderer: Any = None) -> dict[str, Any] | None:
    """The best contact form: in the crawled pages, else on the contact pages they link to.

    The crawl reads a dozen pages ranked for finding people, so the contact page
    can fall outside it; and a form built by scripts is not in the plain HTML at
    all. So when the crawl shows none, up to two contact pages it links to (the
    homepage when it links none) are fetched, then rendered in a browser.
    """
    form = best_contact_form(pages)
    # A form on a contact page is the one to use; one found elsewhere (often
    # hidden in a homepage pop-up) is kept only if the contact page has none.
    if not pages or (form is not None and CONTACT_LINK.search(form["page_url"])):
        return form
    fallback = form
    home = pages[0]["url"]
    domain = website_domain(home)
    seen = {page["url"].split("#", 1)[0].rstrip("/") for page in pages}
    links: list[str] = []
    for page in pages:
        for href, text in getattr(page.get("parser"), "links", []):
            absolute = urljoin(page["url"], href).split("#", 1)[0]
            if absolute.startswith(("http://", "https://")) and same_site(absolute, domain) and CONTACT_LINK.search(f"{href} {text}"):
                links.append(absolute)
    contact_pages = [link for link in dict.fromkeys(links)][:MAX_FORM_PAGES]
    unread = [link for link in contact_pages if link.rstrip("/") not in seen]
    if fetcher is not None and unread:
        robots = site_robots(home, fetcher)
        fetched = []
        for link in unread:
            if not robots.can_fetch(USER_AGENT, link):
                continue
            response = fetcher.fetch(link, same_host_only=True)
            if not response.error and response.status < 400 and "html" in (response.content_type or "html").lower():
                fetched.append({"url": response.url, "raw": response.text})
        form = best_contact_form(fetched)
        if form is not None:
            return form
    if fallback is not None and not contact_pages:
        return fallback
    if renderer is not None:
        rendered = []
        for link in contact_pages or [home]:
            page = renderer.render(link, styles=True)
            if page:
                rendered.append({"url": page[0], "raw": page[1]})
        form = best_contact_form(rendered)
    return form or fallback


def record_contact_form(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, pages: list[dict[str, Any]], fetcher: Any = None, renderer: Any = None,
) -> dict[str, Any] | None:
    """Keep the best contact form on the company's site. A form already used keeps its record."""
    existing = conn.execute(
        "SELECT state FROM outreach_contact_forms WHERE target_id=? AND user_id=?", (target_id, user_id)
    ).fetchone()
    if existing is not None and existing[0] in {"submitted", "unconfirmed"}:
        return None
    form = find_contact_form(pages, fetcher=fetcher, renderer=renderer)
    if form is None:
        return None
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO outreach_contact_forms(target_id, user_id, page_url, fields_json, captcha, accepts_file, state, note, found_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, 'found', '', ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET page_url=excluded.page_url, fields_json=excluded.fields_json,
                captcha=excluded.captcha, accepts_file=excluded.accepts_file, updated_at=excluded.updated_at
            """,
            (target_id, user_id, form["page_url"], json.dumps(form["fields"]), form["captcha"], int(form["accepts_file"]), timestamp, timestamp),
        )
        if existing is None:
            log_event(conn, target_id, user_id, "contact_form_found", detail=form["page_url"])
    return form


def set_contact_form(conn: sqlite3.Connection, target_id: str, page_url: str, *, user_id: str) -> dict[str, Any]:
    """The student names the page with the company's contact form themselves."""
    page_url = page_url.strip()
    validate_web_url(page_url, "Contact form page")
    target = get_target(conn, target_id, user_id=user_id)
    form = target["contact_form"]
    if form and form["state"] in {"submitted", "unconfirmed"}:
        raise ValueError("A message already went through this company's contact form")
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO outreach_contact_forms(target_id, user_id, page_url, fields_json, captcha, accepts_file, state, note, found_at, updated_at)
            VALUES(?, ?, ?, '[]', '', 0, 'found', '', ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET page_url=excluded.page_url, fields_json='[]', captcha='',
                accepts_file=0, state='found', note='', updated_at=excluded.updated_at
            """,
            (target_id, user_id, page_url, timestamp, timestamp),
        )
        log_event(conn, target_id, user_id, "contact_form_set", detail=page_url)
    return get_target(conn, target_id, user_id=user_id)


# --- Filling it in ---------------------------------------------------------------------

_ROLE_PATTERNS = (
    ("first_name", re.compile(r"first[\s_-]?name|given[\s_-]?name|\bfname\b|\bfirst\b")),
    ("last_name", re.compile(r"last[\s_-]?name|family[\s_-]?name|surname|\blname\b|\blast\b")),
    ("subject", re.compile(r"subject|\btopic\b|regarding|\bre\b")),
    ("company", re.compile(r"company|organi[sz]ation|business|employer|school|university|institution")),
    ("phone", re.compile(r"phone|\btel\b|mobile|cell")),
    ("link", re.compile(r"website|\burl\b|linkedin|portfolio|github|\bsite\b")),
    ("job_title", re.compile(r"job[\s_-]?title|\btitle\b|\brole\b|position")),
    ("full_name", re.compile(r"\bname\b|full[\s_-]?name|your[\s_-]?name")),
)
CONSENT = re.compile(r"agree|consent|privacy|terms|accept|acknowledge|gdpr", re.IGNORECASE)
MARKETING = re.compile(r"newsletter|subscribe|marketing|updates|offers|promotion", re.IGNORECASE)
# Options worth choosing for "what is this about", best first.
OPTION_PREFERENCE = (
    re.compile(r"career|\bjobs?\b|employment|hiring|\binterns?(hips?)?\b|recruit", re.IGNORECASE),
    re.compile(r"\bstudents?\b|education", re.IGNORECASE),
    re.compile(r"general|question|^\W*(inquiry|enquiry|contact|information|info)\W*$|contact us", re.IGNORECASE),
    re.compile(r"\bother\b|something else|none of (the|these) above", re.IGNORECASE),
)
# Options that say something a student writing about an internship is not:
# never chosen, whatever else they say ("Researcher / academic", "Project inquiry",
# "International Partnership Inquiry").
UNTRUE_OPTION = re.compile(
    r"research|partner|investor|investment|press|media|customer|client|vendor|supplier|sales|buy|purchase|pricing|quote|"
    r"project|distribut|oem|reseller|dealer|government|procure|defen[cs]e|military|support|service|demo|engineer",
    re.IGNORECASE,
)
# A choice the app may make is only ever what the message is about or who is
# writing (a student): never a country, a state, a budget, an application area,
# or anything else about the student it has no confirmed answer for.
TOPIC_CHOICE = re.compile(
    r"inquir|enquir|reason|topic|subject|regarding|interest|department|purpose|category|nature|about|help|request|"
    r"reaching out|who are you|you are|i am|i'm|org[\s_-]?type|type of (organi[sz]ation|contact)|\brole\b",
    re.IGNORECASE,
)
# "How did you hear about us?" is answered only with Other, the one choice that is never untrue.
OTHER_OPTION = re.compile(r"^\W*(other|something else)\b", re.IGNORECASE)
HEARD_CHOICE = re.compile(r"hear|find us|found us|referr|source", re.IGNORECASE)
REQUIRED_MARK = re.compile(r"\*|\(required\)|\brequired\b", re.IGNORECASE)
PLACEHOLDER_OPTION = re.compile(r"^\s*$|^(select|choose|please|--|—|\.\.\.)", re.IGNORECASE)
CAPTCHA_FIELD = re.compile(r"recaptcha|h-captcha|hcaptcha|turnstile|captcha", re.IGNORECASE)


def _role(field: dict[str, Any]) -> str:
    kind = field["type"]
    text = _field_text(field)
    if kind == "file":
        return "file"
    if kind in {"checkbox", "radio"} or field["tag"] == "select":
        return kind if kind in {"checkbox", "radio"} else "select"
    if _is_email_field(field):
        return "email"
    if field["tag"] == "textarea":
        return "message"
    if kind == "tel":
        return "phone"
    if kind == "url":
        return "link"
    hint = _autocomplete(field)
    if hint in AUTOCOMPLETE_ROLES:
        return AUTOCOMPLETE_ROLES[hint]
    if AUTOCOMPLETE_NEVER.search(hint):
        return "unknown"
    if re.search(r"\b(subject|topic)\b", text):
        return "subject"
    if re.search(r"message|comment|inquiry|enquiry|how can we help|question|details", text) and kind in {"text", ""}:
        return "message"
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(text):
            return role
    return "unknown"


def _choose_option(options: list[dict[str, str]], question: str = "") -> dict[str, str] | None:
    """The option to pick for a required choice, or None when the question is not one the app may answer."""
    usable = [
        option for option in options
        if not PLACEHOLDER_OPTION.search(option.get("text", "")) and option.get("value", "") != ""
        and not (UNTRUE_OPTION.search(option.get("text", "")) and not OPTION_PREFERENCE[0].search(option.get("text", "")))
    ]
    if HEARD_CHOICE.search(question):
        return next((option for option in usable if OTHER_OPTION.search(option.get("text", ""))), None)
    if question and not TOPIC_CHOICE.search(question):
        return None
    for pattern in OPTION_PREFERENCE:
        for option in usable:
            if pattern.search(option.get("text", "")):
                return option
    return None


def plan_fill(
    fields: list[dict[str, Any]],
    identity: dict[str, str],
    *,
    subject: str,
    body: str,
    attachment: str = "",
    avoid: frozenset[tuple[int, str]] = frozenset(),
) -> dict[str, Any]:
    """What goes in each field of the live form, and what the app cannot answer.

    ``avoid`` holds (field index, option value) choices already tried that made
    the page take the form away (Skyways: "Career Opportunities" swaps it for a
    pointer to their jobs page), so the next honest choice is tried instead.

    ``fields`` are as the page script reads them (index, tag, type, label, name,
    required, maxlength, visible, options). Only what the student confirmed goes
    in; an optional field the app has no confirmed answer for stays empty, a
    required one is a problem, and nothing is sent while there is a problem.
    """
    fills: list[dict[str, Any]] = []
    problems: list[str] = []
    used: set[str] = set()
    radio_groups: dict[str, list[dict[str, Any]]] = {}

    def label_of(field: dict[str, Any]) -> str:
        # The words the student sees on the page, without the required marks;
        # an internal name like "org-type" only as a last resort.
        for text in (field.get("label"), field.get("placeholder")):
            cleaned = REQUIRED_MARK.sub("", str(text or "")).strip(" :")
            if cleaned:
                return cleaned[:60]
        if field["type"] == "tel" or _role(field) == "phone":
            return "Phone"
        return str(field.get("name") or field["type"]).strip()[:60]

    reported: set[str] = set()

    def unanswerable(field: dict[str, Any], role: str = "") -> None:
        """One note per thing the form asks that the student has not confirmed, in one wording."""
        label = label_of(field)
        key = role if role in {"phone", "company", "link", "job_title"} else re.sub(r"[^a-z0-9]", "", label.lower())
        if key in reported:
            return
        reported.add(key)
        problems.append(f"The form requires \"{label}\", and your confirmed profile has no answer for it")

    # With a real message box on the form, a one-line box is a subject or a question, never the message.
    has_textarea = any(field["tag"] == "textarea" and field.get("visible", True) for field in fields)

    def put(field: dict[str, Any], role: str, value: str, action: str = "fill") -> None:
        fills.append({"index": field["index"], "action": action, "value": value, "role": role, "label": label_of(field)})
        used.add(role)

    first, _space, last = identity.get("name", "").strip().rpartition(" ")
    if not first:
        first, last = last, ""
    # "Name" beside a separate "Last name" field asks for the first name only.
    split_name = any(_role(field) == "last_name" for field in fields if field.get("visible", True))
    for field in fields:
        if field["type"] in {"hidden", "submit", "button", "image", "reset", "password"}:
            continue
        # A hidden field is a spam trap or the page's own bookkeeping, except a
        # required list hidden behind the site's own dropdown, which is answered through it.
        hidden_list = field["tag"] == "select" and field.get("required")
        if not field.get("visible", True) and not hidden_list:
            continue
        if CAPTCHA_FIELD.search(_field_text(field)):
            continue
        role = _role(field)
        if role == "message" and field["tag"] != "textarea" and has_textarea:
            role = "subject" if re.search(r"topic|subject|inquiry|enquiry", _field_text(field)) else "unknown"
        # A "*" beside the label is how most sites mark a required field.
        required = bool(field.get("required")) or "*" in str(field.get("label") or "")
        limit = field.get("maxlength")
        if role == "radio":
            radio_groups.setdefault(field.get("name") or str(field["index"]), []).append(field)
            continue
        if role == "email":
            if "email" in used:
                if required:
                    put(field, "email", identity["email"])  # "confirm your email"
                continue
            put(field, "email", identity["email"])
        elif role == "message":
            if "message" in used:
                if required:
                    problems.append(f"The form asks a second question, \"{label_of(field)}\"")
                continue
            if limit and len(body) > limit:
                problems.append(f"The message box takes {limit} characters and the draft is {len(body)}. Shorten the draft")
                used.add("message")
                continue
            if field["tag"] != "textarea" and NEWLINE in body.strip():
                problems.append("The form's message box is a single line, so the draft's paragraphs would run together")
                used.add("message")
                continue
            put(field, "message", body)
        elif role == "subject":
            if limit and len(subject) > limit:
                problems.append(f"The subject box takes {limit} characters and the subject is {len(subject)}")
                continue
            put(field, "subject", subject)
        elif role == "first_name" and first:
            put(field, role, first)
        elif role == "last_name" and last:
            put(field, role, last)
        elif role == "full_name" and split_name and first:
            put(field, "first_name", first)
        elif role == "full_name" and identity.get("name"):
            put(field, role, identity["name"])
        elif role in {"company", "phone", "link", "job_title"}:
            # A student has no company; these go in only when the form insists.
            value = {"company": identity.get("school", ""), "phone": identity.get("phone", ""),
                     "link": identity.get("link", ""), "job_title": identity.get("title", "")}[role]
            if required and value:
                put(field, role, value)
            elif required:
                unanswerable(field, role)
        elif role == "select":
            options = field.get("options") or []
            # A list that already shows an answer ("Customer", "Strike systems") would
            # send that answer as the student's, so it is chosen like a required one.
            shown = next((option for option in options if option.get("value") == field.get("selected")), None)
            preset = bool(shown) and not PLACEHOLDER_OPTION.search(shown.get("text", "")) and shown.get("value", "") != ""
            if not required and not preset:
                continue
            option = _choose_option([o for o in options if (field["index"], o.get("value", "")) not in avoid], label_of(field))
            if option is None and not (TOPIC_CHOICE.search(label_of(field)) or HEARD_CHOICE.search(label_of(field))):
                # A country, a state, an application area: facts about the student it has not confirmed.
                unanswerable(field)
            elif option is None:
                offered =", ".join(o.get("text", "") for o in options if o.get("value") and not PLACEHOLDER_OPTION.search(o.get("text", "")))[:120]
                what = f"shows \"{shown.get('text', '')}\" for" if preset and not required else "requires a choice for"
                problems.append(f"The form {what} \"{label_of(field)}\" and none of its choices ({offered}) is true of you")
            else:
                fills.append({"index": field["index"], "action": "select", "value": option["value"], "role": "select",
                              "label": f"{label_of(field)}: {option.get('text', '')}"[:120]})
        elif role == "checkbox":
            text = _field_text(field)
            if required and CONSENT.search(text) and not MARKETING.search(text):
                fills.append({"index": field["index"], "action": "check", "value": "", "role": "consent", "label": label_of(field)})
            elif required:
                problems.append(f"The form requires ticking \"{label_of(field)}\"")
        elif role == "file":
            if attachment and _accepts(field.get("accept", ""), attachment):
                put(field, "file", attachment, action="upload")
            elif required:
                problems.append("The form requires a file, and no resume is set to attach")
        elif required:
            unanswerable(field, role)
    for name, group in radio_groups.items():
        if not any(field.get("required") for field in group):
            continue
        question = next((str(field.get("name") or "") for field in group), name)
        choice = _choose_option([{"value": str(field["index"]), "text": field.get("label", "")} for field in group], question or "unnamed")
        if choice is None:
            problems.append(f"The form requires a choice for \"{name}\" and none fits")
        else:
            chosen = next(field for field in group if str(field["index"]) == choice["value"])
            fills.append({"index": chosen["index"], "action": "check", "value": "", "role": "radio", "label": label_of(chosen)})
    if "message" not in used:
        problems.append("The form has no message box the app could find")
    if "email" not in used:
        problems.append("The form has no email field, so a reply could not reach you")
    if "full_name" not in used and "first_name" not in used and any(
        _role(field) in {"full_name", "first_name"} for field in fields if field.get("visible", True)
    ):
        problems.append("Confirm your name in your profile first")
    return {"fills": fills, "problems": list(dict.fromkeys(problems))}


def _accepts(accept: str, path: str) -> bool:
    if not accept.strip():
        return True
    suffix = Path(path).suffix.lower()
    kinds = {part.strip().lower() for part in accept.split(",")}
    return suffix in kinds or (suffix == ".pdf" and "application/pdf" in kinds) or "*/*" in kinds or (
        suffix in {".doc", ".docx"} and any("word" in kind for kind in kinds)
    )


# --- In the browser --------------------------------------------------------------------

# Reads the contact form in one frame, marking its controls so they can be found
# again: the best form holds a message box and an email field. A page built
# without a <form> element is read from the message box's nearest container
# that also holds an email field and a button.
EXTRACT_SCRIPT = r"""
() => {
  const visible = (el) => {
    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    if (el.type === "hidden" || style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    if (box.width < 2 || box.height < 2 || box.right < 0 || box.bottom < 0 || box.left > 10000) return false;
    if (el.closest("[aria-hidden='true']")) return false;
    return !(el.tabIndex === -1 && box.width < 5);
  };
  // A container's own words, without the text of the controls inside it (a
  // select's options would otherwise read as its label).
  const ownText = (node) => {
    const copy = node.cloneNode(true);
    copy.querySelectorAll("select, option, textarea, input, button, script, style, ul, ol, [role=listbox], [role=option]").forEach((inner) => inner.remove());
    return (copy.textContent || "").replace(/\s+/g, " ").trim();
  };
  // The nearest heading above the form ("Request a demo", "Get in touch"), which says what it is for.
  const headingAbove = (root) => {
    let node = root;
    for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
      for (let before = node.previousElementSibling; before; before = before.previousElementSibling) {
        const heading = before.matches("h1, h2, h3, h4") ? before : before.querySelector("h1, h2, h3, h4");
        if (heading && heading.innerText.trim()) return heading.innerText.trim().slice(0, 120);
      }
    }
    return "";
  };
  const fieldCount = (node) => node.querySelectorAll("input:not([type=hidden]), textarea, select").length;
  // What names a field: its label, its ARIA name, else the text of the
  // nearest container that holds this field alone. A container holding
  // several fields names none of them, so one field never takes another's label.
  const labelFor = (el) => {
    if (el.labels && el.labels.length) return Array.from(el.labels).map(ownText).join(" ").slice(0, 160);
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label");
    const by = el.getAttribute("aria-labelledby");
    if (by) return by.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" ");
    let node = el.parentElement;
    while (node && node !== document.body && fieldCount(node) === 1) {
      const text = ownText(node);
      if (text) return text.slice(0, 160);
      node = node.parentElement;
    }
    // Laid out as label text, then the field, side by side in one container.
    let before = el.previousElementSibling;
    while (before && !before.matches("input, textarea, select") && !fieldCount(before)) {
      const text = ownText(before);
      if (text) return text.slice(0, 160);
      before = before.previousElementSibling;
    }
    return "";
  };
  const controls = (root) => Array.from(root.querySelectorAll("input, textarea, select"));
  const isEmail = (el) => el.type === "email" || /email/i.test(`${el.name} ${el.id} ${el.placeholder} ${labelFor(el)}`);
  // A dropdown's button (a phone country picker) is never the send button.
  const widget = (b) => b.hasAttribute("aria-haspopup") || b.hasAttribute("aria-expanded") || b.closest("[role=listbox], [role=combobox], select");
  const hasSend = (node) => Array.from(node.querySelectorAll("button, input[type=submit], [role=button]")).some((b) => !widget(b));
  const candidates = Array.from(document.querySelectorAll("form"));
  for (const area of document.querySelectorAll("textarea")) {
    if (area.closest("form")) continue;
    let node = area.parentElement;
    while (node && node !== document.body) {
      if (controls(node).some(isEmail) && hasSend(node)) { candidates.push(node); break; }
      node = node.parentElement;
    }
  }
  const score = (root) => {
    const all = controls(root).filter(visible);
    if (all.some((el) => el.type === "password")) return -1;
    const text = `${root.id} ${root.className} ${root.getAttribute("action") || ""}`;
    // Only a blog's comment form is refused: WordPress's own contact form carries "commentsblock".
    if (/search|subscri|newsletter|sign-?up|log-?in|checkout|comment-?form|commentform|wp-comments/i.test(text)) return -1;
    // A one-line message box still marks a contact form; filling it is refused later, with the reason.
    const message = all.some((el) => el.tagName === "TEXTAREA"
      || (el.tagName === "INPUT" && /message|comment|inquiry|enquiry|how can we help/i.test(`${el.name} ${el.id} ${el.placeholder} ${labelFor(el)}`)));
    const email = all.some(isEmail);
    return message && email ? 2 + (/contact/i.test(text) ? 1 : 0) : -1;
  };
  const ranked = candidates.map((root) => [score(root), root]).filter(([s]) => s > 0).sort((a, b) => b[0] - a[0]);
  if (!ranked.length) {
    // A contact form that is on the page but not shown (it opens from a button) is not filled blind.
    const hidden = candidates.some((root) => root.querySelector("textarea") && controls(root).some(isEmail));
    return hidden ? { hidden: true } : null;
  }
  const root = ranked[0][1];
  root.setAttribute("data-pipeline-form", "1");
  const fields = controls(root).map((el, index) => {
    el.setAttribute("data-pipeline-field", String(index));
    return {
      index, tag: el.tagName.toLowerCase(), type: (el.type || "text").toLowerCase(), name: el.name || "", id: el.id || "",
      label: labelFor(el).trim().slice(0, 200), placeholder: el.placeholder || "", autocomplete: el.getAttribute("autocomplete") || "",
      required: el.required || el.getAttribute("aria-required") === "true", maxlength: el.maxLength > 0 ? el.maxLength : null,
      accept: el.accept || "", visible: visible(el),
      options: el.tagName === "SELECT" ? Array.from(el.options).map((o) => ({ value: o.value, text: o.text.trim() })) : [],
      selected: el.tagName === "SELECT" ? el.value : "",
    };
  });
  const buttons = Array.from(root.querySelectorAll("button, input[type=submit], input[type=button], [role=button]")).filter(visible);
  // The send button says so.
  const candidatesToSend = buttons.filter((b) => !widget(b));
  const submit = candidatesToSend.find((b) => /send|submit|contact|get in touch|inquir|enquir|request|let.s talk|start/i.test(b.innerText || b.value || ""))
    || candidatesToSend.find((b) => (b.type || "").toLowerCase() === "submit" && b.tagName === "INPUT")
    || candidatesToSend.find((b) => (b.getAttribute("type") || "").toLowerCase() === "submit")
    || candidatesToSend[candidatesToSend.length - 1];
  if (submit) submit.setAttribute("data-pipeline-submit", "1");
  return { fields, submit: Boolean(submit), submit_text: submit ? (submit.innerText || submit.value || "").replace(/\s+/g, " ").trim().slice(0, 60) : "",
    is_form: root.tagName === "FORM", text: ownText(root).slice(0, 2000), heading: headingAbove(root) };
}
"""
# The CAPTCHA endpoints a rehearsal still lets the checkbox talk to; nothing else may post.
CAPTCHA_ENDPOINTS = re.compile(
    r"^https://(www\.)?(google\.com|recaptcha\.net)/recaptcha/|^https://([a-z0-9-]+\.)*hcaptcha\.com/|^https://challenges\.cloudflare\.com/",
    re.IGNORECASE,
)
CAPTCHA_WIDGETS = (
    # (name, the challenge's own frame, what to click in it, the token it leaves in the page)
    ("recaptcha", "iframe[src*='/recaptcha/'][src*='anchor']:not([src*='size=invisible'])", "#recaptcha-anchor", "textarea[name='g-recaptcha-response']"),
    ("hcaptcha", "iframe[src*='hcaptcha.com'][src*='checkbox']", "#checkbox", "textarea[name='h-captcha-response']"),
    ("turnstile", "iframe[src*='challenges.cloudflare.com']", "input[type='checkbox']", "input[name='cf-turnstile-response']"),
)


class FormSubmitter:
    """Chromium through Playwright, behind the same request guard as outreach_render.

    Headless unless ``headed``: a headed browser opens a window the student can
    see, for a CAPTCHA that asks a person. With ``rehearse`` it does everything
    but send: the form is found, filled, read back, and its CAPTCHA checkbox
    ticked, then left unsent, and every request that could carry the form (any
    method but GET, except to the CAPTCHA services) is refused as well. Use as a
    context manager on one thread.
    """

    def __init__(
        self,
        *,
        headed: bool = False,
        person_wait: float = 0,
        resolve: Resolver = resolve_host,
        screenshot_dir: Path = SCREENSHOT_DIR,
        launch_args: list[str] | None = None,
        route_hook: Callable[[Any], None] | None = None,
        rehearse: bool = False,
    ) -> None:
        self.headed = headed
        self.rehearse = rehearse
        # What a rehearsal refused to let leave the page.
        self.refused: list[str] = []
        self.person_wait = person_wait
        self._resolve = resolve
        self._allowed: dict[str, bool] = {}
        self.screenshot_dir = screenshot_dir
        self._launch_args = list(launch_args or [])
        # Tests serve fixture pages through this instead of the network.
        self._route_hook = route_hook
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    def __enter__(self) -> "FormSubmitter":
        return self

    def __exit__(self, *_exc: Any) -> None:
        close_browser(self)

    def _start(self) -> None:
        if self._context is not None:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=not self.headed, args=self._launch_args)
        self._context = self._browser.new_context(service_workers="block", accept_downloads=False)
        self._context.route("**/*", self._route)

    def _route(self, route: Any) -> None:
        request = route.request
        if self.rehearse and request.method != "GET" and not CAPTCHA_ENDPOINTS.search(request.url):
            self.refused.append(f"{request.method} {request.url}")
            route.abort("blockedbyclient")
            return
        (self._route_hook or self._guard)(route)

    def _guard(self, route: Any) -> None:
        if not request_allowed(route.request.url, self._resolve, self._allowed):
            route.abort("blockedbyclient")
            return
        route.continue_()

    def submit(
        self, page_url: str, *, identity: dict[str, str], subject: str, body: str, attachment: str = "", name: str = "form",
        should_continue: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Fill and send the form. Never raises: the outcome says whether anything left the page.

        ``should_continue`` is asked just before the send button is pressed;
        False (or failing to answer) stops there, with nothing sent and
        ``paused`` set on the result. Automatic sends pass it to honour a pause.
        """
        # "attached" names the file only once it is in the form's file field; most forms have none.
        result: dict[str, Any] = {"outcome": "failed", "note": "", "confirmation": "", "filled": [], "screenshot": "", "filled_screenshot": "", "attached": ""}
        clicked = False
        page = None
        try:
            if not request_allowed(page_url, self._resolve, self._allowed) and self._route_hook is None:
                result["note"] = "The contact form page is not a public web address"
                return result
            try:
                self._start()
            except Exception as exc:  # noqa: BLE001 - missing package, missing browser, no display
                result["note"] = f"Could not start the browser ({type(exc).__name__}). Install Playwright and Chromium: python -m playwright install chromium"
                return result
            page = self._context.new_page()
            response = page.goto(page_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            if response is not None and response.status >= 400:
                result["note"] = f"The contact page answered HTTP {response.status}"
                return result
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:  # noqa: BLE001 - a busy page is read as it stands
                pass
            frame, read = self._find_form(page)
            if frame is None:
                result["note"] = ("The contact form is on the page but hidden (it opens from a button), so it is not filled blind"
                                  if read and read.get("hidden") else
                                  "No contact form on the page (it may be gone, or built in a way the app cannot read)")
                return result
            # The form's own words and heading, not its dropdown's options; a
            # heading that also says contact ("Contact us / Book a demo") is a contact form.
            heading = str(read.get("heading") or "")
            purpose = None if CONTACT_LINK.search(heading) else SALES_FORM.search(f"{heading}\n{str(read.get('text') or '')[:400]}")
            if purpose:
                result.update(outcome="needs_you", note=(
                    f"This form is for sales (\"{purpose.group(0)}\"), not general contact, so the app does not use it for your email. "
                    "Nothing was sent"
                ))
                return result
            plan = plan_fill(read["fields"], identity, subject=subject, body=body, attachment=attachment)
            if plan["problems"]:
                result.update(outcome="needs_you", note=". ".join(plan["problems"]))
                return result
            if not read["submit"]:
                result["note"] = "The form has no send button the app could find"
                return result
            # Choices go in first: some sites redraw the form when one changes,
            # which would lose typed text and the marks on each field. The form
            # is then read again and planned afresh, and the text goes in last.
            first_read, avoid = read, frozenset()
            for _attempt in range(4):
                choices = [fill for fill in plan["fills"] if fill["action"] in {"select", "check"}]
                for fill in choices:
                    control = frame.locator(f"[data-pipeline-field='{fill['index']}']").first
                    try:
                        if fill["action"] == "select":
                            control.select_option(fill["value"], timeout=FIELD_TIMEOUT_MS, force=True)
                        else:
                            control.check(force=True, timeout=FIELD_TIMEOUT_MS)
                    except Exception:  # noqa: BLE001 - a choice the page would not take
                        result.update(outcome="needs_you", note=f"The choice \"{fill['label']}\" could not be made on the page. Nothing was sent")
                        return result
                if not choices:
                    break
                page.wait_for_timeout(500)
                again = frame.evaluate(EXTRACT_SCRIPT)
                if again and not again.get("hidden"):
                    read = again
                    plan = plan_fill(read["fields"], identity, subject=subject, body=body, attachment=attachment, avoid=avoid)
                    if plan["problems"]:
                        result.update(outcome="needs_you", note=". ".join(plan["problems"]))
                        return result
                    break
                # A choice took the form away: try the next honest choice for each list.
                avoid = avoid | {(fill["index"], fill["value"]) for fill in choices if fill["action"] == "select"}
                plan = plan_fill(first_read["fields"], identity, subject=subject, body=body, attachment=attachment, avoid=avoid)
                if plan["problems"]:
                    taken = ", ".join(f"\"{fill['label']}\"" for fill in choices if fill["action"] == "select")
                    result.update(outcome="needs_you", note=(
                        f"Choosing {taken} made the page take the form away, and no other choice is true of you. Nothing was sent"
                    ))
                    return result
            for fill in plan["fills"]:
                control = frame.locator(f"[data-pipeline-field='{fill['index']}']").first
                try:
                    if fill["action"] == "fill":
                        control.fill(fill["value"], timeout=FIELD_TIMEOUT_MS)
                        # Some sites only notice a value on change or blur, as when a person tabs away.
                        control.dispatch_event("change")
                        control.blur()
                    elif fill["action"] == "check":
                        control.check(force=True, timeout=FIELD_TIMEOUT_MS)
                    elif fill["action"] == "select":
                        # force: a list hidden behind the site's own dropdown is still the one that is sent.
                        control.select_option(fill["value"], timeout=FIELD_TIMEOUT_MS, force=True)
                    elif fill["action"] == "upload":
                        control.set_input_files(fill["value"], timeout=FIELD_TIMEOUT_MS)
                        result["attached"] = Path(fill["value"]).name
                except Exception:  # noqa: BLE001 - a field covered by another element, or read-only
                    result.update(outcome="needs_you", note=(
                        f"The field \"{fill['label']}\" did not take text (another element covers it, or it is read-only). Nothing was sent"
                    ))
                    return result
                result["filled"].append(fill["label"])
            # What the page holds now, read back: a site script that ate the blank
            # lines or changed the punctuation stops the send.
            for fill in plan["fills"]:
                if fill["action"] != "fill":
                    continue
                held = frame.locator(f"[data-pipeline-field='{fill['index']}']").first.input_value()
                changed = formatting_problem(fill["value"], held)
                if changed:
                    result.update(outcome="needs_you", note=f"The form changed what was typed into \"{fill['label']}\": {changed}. Nothing was sent")
                    return result
            result["filled_screenshot"] = self._screenshot(page, f"{name}-filled", frame.locator("[data-pipeline-form]").first)
            if read["is_form"]:
                invalid = frame.evaluate(
                    "() => Array.from(document.querySelector('[data-pipeline-form]').querySelectorAll('input:invalid, textarea:invalid, select:invalid'))"
                    ".map((el) => [el.getAttribute('data-pipeline-field'), el.validationMessage || el.name])"
                )
                labels = {str(field["index"]): (field.get("label") or field.get("name") or "").strip()[:60] for field in read["fields"]}
                invalid = [f"\"{labels.get(index) or '(unlabeled field)'}\": {message}" for index, message in invalid]
                if invalid:
                    result.update(outcome="needs_you", note="The form did not accept what was filled in: " + "; ".join(invalid[:3]))
                    return result
            captcha = self._pass_captcha(page, frame)
            if captcha:
                result.update(outcome="needs_you", note=captcha)
                return result
            if self.rehearse:
                result.update(outcome="rehearsed", note=f"Ready to send; the button reads \"{read.get('submit_text', '')}\"")
                return result
            if should_continue is not None and not _answers_yes(should_continue):
                result.update(outcome="failed", note=PAUSED_BEFORE_SENDING, paused=True)
                return result
            before = self._visible_text(frame)
            sent: list[Any] = []
            page.on("request", lambda request: sent.append(request) if (
                request.method != "GET" or request.resource_type == "document"
            ) and request.resource_type not in {"image", "stylesheet", "font", "media"} else None)
            clicked = True
            frame.locator("[data-pipeline-submit]").first.click(timeout=10_000)
            outcome = self._await_outcome(page, frame, before, sent)
            result.update(outcome)
            return result
        except Exception as exc:  # noqa: BLE001 - the outcome must still be recorded
            if clicked:
                result.update(outcome="unconfirmed", note=f"The browser failed after pressing send ({type(exc).__name__}). Check whether it arrived")
            else:
                result.update(outcome="failed", note=f"The page could not be filled in ({type(exc).__name__}: {str(exc)[:160]})")
            return result
        finally:
            if page is not None:
                if clicked or result["outcome"] == "needs_you":
                    result["screenshot"] = self._screenshot(page, name)
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass

    def _find_form(self, page: Any) -> tuple[Any, dict[str, Any] | None]:
        """The frame holding a visible contact form and what it reads, or (None, {"hidden": True}) when one is only hidden."""
        hidden = None
        for frame in [page.main_frame, *[child for child in page.frames if child != page.main_frame]]:
            try:
                read = frame.evaluate(EXTRACT_SCRIPT)
            except Exception:  # noqa: BLE001 - a frame that went away, or one the page walls off
                continue
            if read and read.get("hidden"):
                hidden = read
            elif read:
                return frame, read
        return None, hidden

    def _pass_captcha(self, page: Any, frame: Any) -> str:
        """Tick a checkbox CAPTCHA; with a window open, wait for the student to solve a harder one.

        Returns what is still in the way, or "" when there is no CAPTCHA or it passed.
        """
        for name, frame_selector, checkbox, token in CAPTCHA_WIDGETS:
            if not frame.locator(frame_selector).count():
                continue
            if self._token(frame, token):
                return ""
            try:
                frame.frame_locator(frame_selector).first.locator(checkbox).click(timeout=5_000)
            except Exception:  # noqa: BLE001 - Turnstile often has nothing to click; it runs by itself
                pass
            deadline = time.monotonic() + CAPTCHA_WAIT_SECONDS
            while time.monotonic() < deadline:
                if self._token(frame, token):
                    return ""
                page.wait_for_timeout(500)
            if self.headed and self.person_wait:
                page.bring_to_front()
                deadline = time.monotonic() + self.person_wait
                while time.monotonic() < deadline:
                    if self._token(frame, token):
                        return ""
                    page.wait_for_timeout(1_000)
                return f"The {name} challenge was not solved within {int(self.person_wait // 60)} minutes. Nothing was sent"
            return (f"The form's {name} asked for a picture challenge. Nothing was sent. "
                    "Use Finish in browser to solve it yourself; the app sends the form once it is solved")
        return ""

    @staticmethod
    def _token(frame: Any, selector: str) -> bool:
        try:
            return bool(frame.evaluate(
                "(selector) => Array.from(document.querySelectorAll(selector)).some((el) => (el.value || '').length > 20)",
                selector,
            ))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _visible_text(frame: Any) -> str:
        try:
            return str(frame.evaluate("() => document.body ? document.body.innerText : ''"))
        except Exception:  # noqa: BLE001 - the frame navigated away
            return ""

    def _await_outcome(self, page: Any, frame: Any, before: str, sent: list[Any]) -> dict[str, str]:
        start_url = frame.url
        known = {match.group(0).casefold() for match in SUCCESS_TEXT.finditer(before)}
        deadline = time.monotonic() + CONFIRM_WAIT_SECONDS
        confirmation = ""
        form_gone = False
        while time.monotonic() < deadline:
            page.wait_for_timeout(500)
            for place in (frame, page.main_frame):
                text = self._visible_text(place)
                fresh = [m for m in SUCCESS_TEXT.finditer(text) if m.group(0).casefold() not in known]
                if fresh:
                    confirmation = _sentence(text, fresh[0])
                    break
            if confirmation:
                break
            try:
                form_gone = frame.url != start_url or not frame.locator("[data-pipeline-form]").first.is_visible()
            except Exception:  # noqa: BLE001 - the frame itself went away with the form
                form_gone = True
            if form_gone and sent:
                page.wait_for_timeout(1_500)
                text = self._visible_text(page.main_frame)
                fresh = [m for m in SUCCESS_TEXT.finditer(text) if m.group(0).casefold() not in known]
                confirmation = _sentence(text, fresh[0]) if fresh else ""
                break
        if confirmation:
            return {"outcome": "submitted", "note": "", "confirmation": confirmation}
        rejected = [request for request in sent if (response := _response(request)) is not None and 400 <= response.status < 500]
        if not sent:
            text = self._visible_text(frame)
            errors = [line.strip() for line in text.splitlines() if ERROR_TEXT.search(line) and line.strip() not in before]
            if frame.locator("iframe[src*='bframe'], iframe[src*='hcaptcha.com'][src*='challenge']").count():
                return {"outcome": "needs_you", "note": "A CAPTCHA challenge appeared when the form was sent. Nothing was sent. Use Finish in browser", "confirmation": ""}
            return {"outcome": "failed", "note": "The form did not send" + (f": {errors[0][:200]}" if errors else ""), "confirmation": ""}
        if rejected and len(rejected) == len(sent):
            return {"outcome": "failed", "note": f"The site refused the form (HTTP {_response(rejected[0]).status})", "confirmation": ""}
        what = "The form was sent and the page moved on" if form_gone else "The form was sent"
        return {"outcome": "unconfirmed", "note": f"{what}, but the page did not say it arrived. Look for a confirmation email from them", "confirmation": ""}

    def _screenshot(self, page: Any, name: str, element: Any = None) -> str:
        """The page (or one element of it) as it looked, kept as evidence of what was sent."""
        try:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshot_dir / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', name)}.png"
            if element is not None:
                element.screenshot(path=str(path))
            else:
                page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:  # noqa: BLE001 - evidence is a bonus, not the outcome
            return ""


def _answers_yes(question: Callable[[], bool]) -> bool:
    """A question that cannot be answered counts as no: when unsure, nothing is sent."""
    try:
        return bool(question())
    except Exception:  # noqa: BLE001 - a failed check stops the send
        return False


def formatting_problem(expected: str, held: str) -> str:
    """How the text a field holds differs from what was typed, in words, or "" when it is the same.

    Browsers keep a textarea's line breaks as \n and send them as \r\n, so
    only that difference is allowed; every blank line and every character of
    punctuation must survive.
    """
    want = expected.replace(CRLF, NEWLINE)
    have = held.replace(CRLF, NEWLINE)
    if want == have:
        return ""
    if len(have) < len(want) and want.startswith(have):
        return f"it cut the text off after {len(have)} of {len(want)} characters"
    blank = NEWLINE * 2
    if want.count(blank) != have.count(blank):
        return f"the draft has {want.count(blank)} blank lines between paragraphs and the box kept {have.count(blank)}"
    if want.count(NEWLINE) != have.count(NEWLINE):
        return "it lost line breaks"
    lost = sorted({char for char in want if char not in have})
    if lost:
        return "it dropped " + ", ".join(repr(char) for char in lost[:6])
    return "the text differs from the approved draft"


def _response(request: Any) -> Any:
    try:
        return request.response()
    except Exception:  # noqa: BLE001
        return None


def _sentence(text: str, match: re.Match[str]) -> str:
    start = max(text.rfind("\n", 0, match.start()), text.rfind(". ", 0, match.start()) + 1, 0)
    end_candidates = [index for index in (text.find("\n", match.end()), text.find(". ", match.end())) if index != -1]
    end = min(end_candidates) + 1 if end_candidates else len(text)
    return " ".join(text[start:end].split())[:300]


def default_submitter_factory(*, in_browser: bool = False) -> FormSubmitter:
    return FormSubmitter(headed=in_browser, person_wait=PERSON_WAIT_SECONDS if in_browser else 0)


# --- Sending the approved first message ---------------------------------------------------------


def identity_for(conn: sqlite3.Connection, user_id: str) -> dict[str, str]:
    """What the student has confirmed that a contact form may ask for."""
    facts = confirmed_facts(conn, user_id)
    contact = facts.get("contact") if isinstance(facts.get("contact"), dict) else {}
    name = str(facts.get("name") or "").strip()
    if not name:
        raise ValueError("Confirm your name in your profile before sending a contact form")
    email = sender_account() or str(contact.get("email") or "").strip()
    if not email:
        raise ValueError("Set the address replies should go to (your outreach Gmail account) before sending a contact form")
    link = next((str(contact.get(key) or "").strip() for key in ("linkedin", "portfolio", "github") if str(contact.get(key) or "").strip()), "")
    return {
        "name": name, "email": email, "phone": str(contact.get("phone") or "").strip(),
        "school": str(facts.get("school") or "").strip(), "link": link, "title": "Student",
    }


def form_ready(target: dict[str, Any], *, fingerprint: str | None = None, retry: bool = False) -> None:
    """Every check a form submission must pass, with the reason it cannot go when one fails."""
    form = target["contact_form"]
    if target["contact_email"]:
        raise ValueError(f"{target['company']} has an email contact; send the email instead")
    if not form:
        raise ValueError(f"No contact form is on record for {target['company']}")
    if target["draft_status"] != "approved":
        raise ValueError("Approve this draft before sending it through the contact form")
    if target["draft_location"]["missing"]:
        raise ValueError(missing_location_message(target))
    if fingerprint is not None and fingerprint != target["draft_fingerprint"]:
        raise DraftChangedError("This draft changed after you confirmed it. Review it, then send again")
    if target["sent_at"] or target["status"] not in UNSENT_STATUSES:
        raise ValueError(f"{target['company']} is already marked {target['status'].replace('_', ' ')}, so the form is not sent again")
    if form["state"] == "submitted":
        raise ValueError("This message already went through their contact form")
    if form["state"] == "unconfirmed" and not retry:
        raise SendNeedsCheckError(
            "The form was sent before but the page did not say it arrived. Look for a confirmation email from them: "
            "if it went, use \"I sent it\"; if not, send it again.",
            "form-unconfirmed",
        )


def _click_held(row: Any) -> bool:
    """Whether a request may still be pressing the button under this form claim ('clicking').

    Held exactly as a claim still being worked on would be (send_claim_held): by
    this process while its request runs, or by another for a grace period.
    """
    return row["state"] == automation.FORM_HANDED_OVER and send_claim_held({**dict(row), "state": "drafting"})


def submit_contact_form(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    submitter_factory: Callable[..., Any] = default_submitter_factory,
    fingerprint: str | None = None,
    retry_unconfirmed: bool = False,
    in_browser: bool = False,
    automatic: bool = False,
) -> dict[str, Any]:
    """Send the approved first email through the company's contact form, once.

    ``automatic`` is the worker sending on the student's behalf: a pause then
    raises automation.AutomationPaused before the claim is kept, and is checked
    again just before the send button. Either way nothing is sent and the form
    stays 'found' for when they resume.
    """
    target = get_target(conn, target_id, user_id=user_id)
    form_ready(target, fingerprint=fingerprint, retry=retry_unconfirmed)
    identity = identity_for(conn, user_id)
    stale_token = ""
    existing = send_claim_row(conn, target_id, user_id, "initial")
    if existing is not None:
        if send_claim_held(existing) or _click_held(existing):
            raise SendConflictError(IN_PROGRESS)
        if existing["state"] == "sent":
            raise ValueError("This first message was already sent")
        # A claim left mid-click (the app stopped as the button was pressed) may have sent the form.
        if existing["state"] in {"unconfirmed", automation.FORM_HANDED_OVER} and not retry_unconfirmed:
            raise SendNeedsCheckError(
                "An earlier send of this message may have gone out. Check before sending it again.", "form-unconfirmed",
            )
        stale_token = existing["token"]
    path = attachment_path()
    attachment = str(path) if path and not attachment_problem(path) else ""

    def revalidate() -> dict[str, Any]:
        # Inside the claim's transaction, after the claim insert, so a pause either lands first or waits for it.
        if automatic and automation.pause_guard(conn, user_id):
            raise automation.AutomationPaused()
        fresh = get_target(conn, target_id, user_id=user_id)
        form_ready(fresh, fingerprint=fingerprint or target["draft_fingerprint"], retry=retry_unconfirmed)
        return fresh

    with claimed_send(conn, target_id, user_id, "initial", "form", revalidate, stale_token=stale_token) as (token, fresh):
        def hand_over() -> bool:
            """Just before the button: hand the claim over, in one step with checking the pause.

            The transaction starts with pause_guard, so a pause either lands
            first (seen here: nothing is pressed) or waits until the claim is
            'clicking', and then reports this form as in flight. False too if
            the claim is no longer ours as it was: when unsure, nothing is sent.
            """
            with conn:
                if automation.pause_guard(conn, user_id):
                    return False
                return bool(conn.execute(
                    "UPDATE outreach_send_claims SET state=?, claimed_at=? WHERE target_id=? AND kind='initial' AND token=? AND state='drafting'",
                    (automation.FORM_HANDED_OVER, utc_now(), target_id, token),
                ).rowcount)

        submit_options: dict[str, Any] = {"should_continue": hand_over} if automatic else {}
        page_url = fresh["contact_form"]["page_url"]
        try:
            with submitter_factory(in_browser=in_browser) as submitter:
                result = submitter.submit(
                    page_url, identity=identity, subject=fresh["email_subject"], body=fresh["email_body"],
                    attachment=attachment, name=target_id, **submit_options,
                )
        except BaseException:
            settle_send_claim(conn, target_id, "initial", token, "unconfirmed")
            raise
        outcome = result["outcome"]
        # Stopped by a pause just before the button: nothing went, and the form waits as it was.
        held = bool(result.get("paused")) and outcome not in {"submitted", "unconfirmed"}
        timestamp = utc_now()
        detail = {
            "kind": "initial", "fingerprint": fresh["draft_fingerprint"], "page_url": page_url,
            "confirmation": result.get("confirmation", ""), "filled": result.get("filled", []),
            # The file that went in with the form, not merely the one on hand to attach.
            "attachment": result.get("attached", ""), "screenshot": result.get("screenshot", ""),
            "filled_screenshot": result.get("filled_screenshot", ""),
            "note": result.get("note", ""), "in_browser": in_browser,
        }
        try:
            with conn:
                if outcome == "submitted":
                    conn.execute("UPDATE outreach_send_claims SET state='sent' WHERE target_id=? AND kind='initial' AND token=?", (target_id, token))
                elif outcome == "unconfirmed":
                    conn.execute("UPDATE outreach_send_claims SET state='unconfirmed' WHERE target_id=? AND kind='initial' AND token=?", (target_id, token))
                else:
                    conn.execute("DELETE FROM outreach_send_claims WHERE target_id=? AND kind='initial' AND token=?", (target_id, token))
                conn.execute(
                    "UPDATE outreach_contact_forms SET state=?, note=?, attempted_at=?, updated_at=? WHERE target_id=? AND user_id=?",
                    ("found" if held else outcome, result.get("note", "")[:500], timestamp, timestamp, target_id, user_id),
                )
                event = {"submitted": SUBMITTED_EVENT, "unconfirmed": UNCONFIRMED_EVENT}.get(outcome, NOT_SENT_EVENT)
                log_event(conn, target_id, user_id, event, detail=json.dumps(detail, sort_keys=True))
        except BaseException:
            settle_send_claim(conn, target_id, "initial", token, "sent" if outcome == "submitted" else "unconfirmed")
            raise
    marked = True
    if outcome == "submitted":
        try:
            update_target(conn, target_id, {"status": "sent"}, user_id=user_id)
        except Exception:
            marked = False  # it went and is recorded; "I sent it" catches the status up
    return {
        "outcome": outcome, "note": result.get("note", ""), "confirmation": result.get("confirmation", ""),
        "page_url": page_url, "filled": result.get("filled", []), "marked": marked,
        "target": get_target(conn, target_id, user_id=user_id),
    }


def form_due(conn: sqlite3.Connection, *, user_id: str) -> list[str]:
    """Companies whose approved first message can go through a contact form nobody has tried yet."""
    due = []
    for item in list_targets(conn, user_id=user_id, interested_only=True):
        form = item["contact_form"]
        if not form or form["state"] != "found":
            continue
        try:
            form_ready(item)
        except (ValueError, SendNeedsCheckError):
            continue
        due.append(item["id"])
    return due


def is_acknowledgement(sender: str, subject: str, text: str, minutes_after: float) -> bool:
    """An automatic "we received your message" after a form, not a person answering."""
    words = " ".join((subject, text[:2000]))
    if NO_REPLY_SENDER.search(sender) or ALWAYS_AUTOMATIC.search(words):
        return True
    return minutes_after <= ACKNOWLEDGEMENT_WINDOW_MINUTES and bool(ACKNOWLEDGEMENT.search(words))
