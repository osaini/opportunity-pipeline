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
(a spam trap) is never filled, and an optional phone number, school, or link
is left blank. A required address box (street, city, state, ZIP, country) is
answered only from the mailing address the student confirmed in their profile;
with none, it stops the send like any other unanswerable field, as does a street
box with no city, state or ZIP box beside it. When a form requires the street
address, the rest of the confirmed address goes into its other address boxes; a
form that requires only a country, state, city or ZIP gets only that. A form
that asks for any part of an address twice (a second block for a reference or an
emergency contact) gets no address at all: the app cannot tell which block is the
student's, so each required address box there stops the send.
A checkbox CAPTCHA is clicked like a person would; one that asks
for a picture challenge is left to the student. Nothing disguises the browser.

Finish in browser (``in_browser``) hands the form to the student in a window
they can see. The app fills what it truthfully can, outlines each box it left
empty, and names what is left in a note on the page; the student fills those,
solves any CAPTCHA, and presses the form's own send button. The app never
presses it there. It fails closed: nothing that could carry the form leaves
the window until the app has seen the student's own press of the form's send
button, so a press it misses sends nothing rather than something it would not
record, and with no press seen the outcome is needs_you, nothing sent. From
the press the claim is 'clicking' and the form 'unconfirmed' with
FORM_PRESSED_NOTE, and the app stops judging: it does not read the page for a
thank-you. When the window closes the card asks the student whether their
page said the message was sent; Yes marks it sent, No opens Finish in browser
again (and the history records that they said so).

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
from urllib.parse import unquote_plus, urljoin, urlsplit

from .. import ROOT
from ..automation import ledger as automation
from .contact_names import NO_REPLY_SENDER, website_domain
from .targets import FORM_PRESSED_NOTE, UNSENT_STATUSES, DraftChangedError, list_targets, log_event, get_target, update_target, validate_web_url
from .location import US_STATES, missing_location_message
from .config import sender_account
from .gmail import SendNeedsCheckError, attachment_path, attachment_problem
from .send_claims import (
    SendConflictError,
    send_claim_row,
    send_claim_held,
    claimed_send,
    settle_send_claim,
)
from .render import request_allowed
from ..student.preparation import confirmed_facts
from ..student.profile import ADDRESS_FIELDS
from ..core.timestamps import utc_now
from ..integrations.web_fetch import USER_AGENT, Resolver, close_browser, resolve_host, same_site, site_robots

FORM_STATES = ("found", "submitted", "unconfirmed", "needs_you", "failed")
SUBMITTED_EVENT = "form_submitted"
UNCONFIRMED_EVENT = "form_unconfirmed"
NOT_SENT_EVENT = "form_not_sent"
SCREENSHOT_DIR = ROOT / "data" / "private" / "outreach-forms"
# How long a Finish in browser window is the student's: to fill what the app left, solve a CAPTCHA, and press send.
# The app's answer waits for the window, so this stays well under the 300 seconds Firefox waits for an answer,
# leaving room for the page to load and be filled first.
PERSON_WAIT_SECONDS = 240
# How long the form's frame has to say the press listener is in place before the window is handed over.
READY_WAIT_SECONDS = 3
# A request that could carry the form, made while no press has been seen, waits this long for the press to be reported:
# the press is reported first, by a few milliseconds, but the two reach this process by different routes.
PRESS_GRACE_SECONDS = 0.15
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
FORM_IN_PROGRESS = "This form is already being sent, or its Finish in browser window is still open. Wait for it to finish, then reload"
NOT_WATCHED = "The app could not watch the window for your press of send, so it did not hand the form over. Nothing was sent"
NO_SEND_BUTTON = "The app could not find the form's send button, so it did not hand the form over. Nothing was sent"
# The student answered No on the card ("it was not sent") and opened Finish in browser again: who and when are the event's.
SAID_NOT_SENT_EVENT = "form_said_not_sent"
TRIED_BEFORE = ("You said your last press did not send this form. If their page or an email from them says it did, "
                "close this window: pressing send again would send it twice.")


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
# The student's mailing address, when they confirmed one in their profile (profile.ADDRESS_FIELDS).
# Like a phone number, it goes in only where the form requires the box.
AUTOCOMPLETE_ADDRESS = {
    "address-line1": "address_line1", "address-line2": "address_line2", "street-address": "street_address",
    "address-level2": "city", "address-level1": "state", "postal-code": "postal_code",
    "country": "country", "country-name": "country",
}
ADDRESS_ROLES = frozenset(AUTOCOMPLETE_ADDRESS.values()) | {"street_address"}
# What each address role is called in a note.
ADDRESS_WORDS = {
    "address_line1": "street address", "address_line2": "address line 2", "street_address": "street address",
    "city": "city", "state": "state", "postal_code": "ZIP code", "country": "country",
}
# Every hint that names part of an address. One the student's address does not answer (a billing or work
# address, a third line, a district) leaves the box unanswerable, whatever its label says ("Company address").
_ADDRESS_HINTS = frozenset(AUTOCOMPLETE_ADDRESS) | {"address", "address-line3", "address-level3", "address-level4"}
# The browser's autofill field names (the HTML standard's list). A box with one of these that is not an address
# hint is that thing, not an address, whatever its label says ("Country" on a tel-country-code box).
AUTOFILL_TOKEN = re.compile(
    r"^(name|honorific-prefix|given-name|additional-name|family-name|honorific-suffix|nickname|username|new-password|"
    r"current-password|one-time-code|organization-title|organization|street-address|address|address-line[123]|address-level[1-4]|"
    r"country|country-name|postal-code|cc-.*|transaction-currency|transaction-amount|language|bday.*|sex|url|photo|"
    r"tel|tel-.*|email|impp|webauthn)$"
)
# Hints for things the app never fills in for the student.
AUTOCOMPLETE_NEVER = re.compile(r"^(address|address-line3|address-level[34]|bday.*|sex|"
                                r"honorific-.*|additional-name|nickname|username|new-password|current-password|cc-.*|transaction-.*|language)$")
# A label names an address box only when every word of it is one of these: "Address Line 1", "ZIP / Postal Code",
# "Country/Region". "Please state your interest", "Company address" and "City you want to work in" are not.
# "Nation" (a nationality), "PO Box" and "Street number" are none of the student's address lines. Nor is a
# "Home country", "Home state" or "Permanent address": the mailing address may be a dorm, and a home country
# may mean a nationality.
_ADDRESS_QUALIFIERS = frozenset(
    "your mailing current primary of the and or required optional "
    "region line address addr street 1 2 one two apt apartment suite unit flat floor".split()
)
_ADDRESS_WORD_ROLES = (
    ("country", frozenset({"country"})),
    ("postal_code", frozenset({"zip", "postal", "postcode"})),
    ("state", frozenset({"state", "province", "territory"})),
    ("city", frozenset({"city", "town", "suburb"})),
    ("address_line2", frozenset({"apt", "apartment", "suite", "unit", "flat", "floor", "2", "two"})),
    ("address_line1", frozenset({"address", "addr", "street"})),
)
# "Unit" alone is as often a department; beside one of these it is an apartment.
_UNIT_COMPANIONS = frozenset({"apt", "apartment", "suite", "address", "addr", "line"})
_ADDRESS_WORDS = _ADDRESS_QUALIFIERS | {word for _role_name, words in _ADDRESS_WORD_ROLES for word in words} | {"code"}
# Not the student's own home address, whatever the box is called.
_NOT_HOME_SECTION = {"billing", "shipping", "work"}
# A name or id with one of these words is some organization's address ("company_address", "officeCity").
# Words that make a country or place a fact about the student's papers or birth, not where mail reaches them
# ("nationality", "citizenshipCountry", "Country of birth"), in a name or id or in the words the student sees.
_NOT_A_MAILING_PLACE = frozenset({"nationality", "national", "citizenship", "citizen", "birth", "origin", "passport", "issuing"})
# Words that make an address someone else's: a reference's, an emergency contact's, a parent's, a past one.
_SOMEONE_ELSE = frozenset({"reference", "emergency", "alternate", "alternative", "secondary", "other", "previous", "prior",
                           "mother", "father", "parent", "guardian", "spouse"})
_NOT_HOME_ATTRIBUTE = frozenset({
    "company", "org", "organization", "organisation", "business", "office", "work", "billing", "shipping", "employer",
    "school", "campus", "hq", "headquarters", "kin", "relative", "supervisor", "manager", "landlord", "recipient",
    "delivery", "venue", "event", "property", "alt",
}) | _NOT_A_MAILING_PLACE | _SOMEONE_ELSE


def _attribute_words(field: dict[str, Any]) -> list[str]:
    """The words of a field's name and id in order, split on anything not a letter and between camelCase words."""
    raw = f"{field.get('name') or ''} {field.get('id') or ''}"
    return [word.lower() for word in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", raw)]


def _someone_elses(words: list[str], vetoed: frozenset[str]) -> bool:
    """Whether these words (in order) name a box that is not the student's own address."""
    return bool(vetoed & set(words)) or " contact person " in f" {' '.join(words)} "


def _someones_elses_attribute(field: dict[str, Any]) -> bool:
    return _someone_elses(_attribute_words(field), _NOT_HOME_ATTRIBUTE)


def _autocomplete(field: dict[str, Any]) -> str:
    tokens = str(field.get("autocomplete") or "").lower().split()
    return tokens[-1] if tokens else ""


def _address_role(field: dict[str, Any]) -> str:
    """The address box a field is ("city", "postal_code", ...), "" when it is not one the student's address answers."""
    if field["type"] in {"checkbox", "radio", "file", "email", "tel", "url", "hidden"} or _is_email_field(field):
        return ""
    tokens = str(field.get("autocomplete") or "").lower().split()
    if tokens and _NOT_HOME_SECTION & set(tokens[:-1]):
        return ""
    if _someones_elses_attribute(field):
        return ""
    seen = " ".join(str(field.get(key) or "") for key in ("label", "placeholder", "aria_label"))
    if _someone_elses(re.findall(r"[a-z]+", seen.lower()), _NOT_A_MAILING_PLACE | _SOMEONE_ELSE):
        return ""
    if tokens and tokens[-1] in AUTOCOMPLETE_ADDRESS:
        return AUTOCOMPLETE_ADDRESS[tokens[-1]]
    # Any other real hint ("tel-country-code", "organization", "address-level3") says what the box is.
    if tokens and AUTOFILL_TOKEN.search(tokens[-1]):
        return ""
    # The words the student sees; a field with none is named by its attributes ("address_line_1").
    shown = next((str(field.get(key) or "") for key in ("label", "placeholder", "aria_label") if re.search(r"[a-z]", str(field.get(key) or ""), re.I)), "")
    words = re.findall(r"[a-z]+|\d+", (shown or f"{field.get('name') or ''} {field.get('id') or ''}").lower())
    words = [word for word in dict.fromkeys(words) if word not in {"required", "optional"}]
    if not words or any(word not in _ADDRESS_WORDS for word in words):
        return ""
    roles = [role for role, named in _ADDRESS_WORD_ROLES if named & set(words)]
    if "address_line2" in roles and "address_line1" in roles:
        roles.remove("address_line1")  # "Address Line 2" says address too
    # "Country code" picks a phone's prefix; "City / State" is two answers in one box.
    if len(roles) != 1:
        return ""
    # "Territory" alone is a sales territory; "State/Territory" is where the student lives.
    if "territory" in words and not {"state", "province"} & set(words):
        return ""
    if "unit" in words and not _UNIT_COMPANIONS & set(words):
        return ""
    if roles[0] == "country" and ("code" in words or re.search(r"code|dial|calling|phone|(?<![a-z])tel", f"{field.get('name') or ''} {field.get('id') or ''}", re.I)):
        return ""
    return roles[0]


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
    if kind in {"checkbox", "radio"}:
        return kind
    # Before the list and the message box: a country list is not a topic, a street box is not a message.
    address = _address_role(field)
    if address:
        return address
    # An address hint the student's address does not answer ("work street-address" on "Company address")
    # is never the school or the student's name.
    if _autocomplete(field) in _ADDRESS_HINTS and kind not in {"email", "tel", "url"} and not _is_email_field(field):
        return "unknown"
    # Nor is a box whose label names an address and whose name or id says whose ("Address" named school_address,
    # "City" named hq_city): it is not the student's, and not the school's name either.
    if _someones_elses_attribute(field) and _address_role({**field, "name": "", "id": ""}):
        return "unknown"
    if field["tag"] == "select":
        return "select"
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


def _plain(text: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text).casefold()))


# Names a list may use for the same country; a list names the student's own however it likes.
COUNTRY_SPELLINGS = (
    ("United States", "United States of America", "USA", "US", "U.S.", "U.S.A."),
    ("United Kingdom", "UK", "Great Britain", "GB"),
)
# The ISO 3166 codes among them: a box hinted "country" wants the code, so it goes first among the short ones.
COUNTRY_CODES = frozenset({"US", "GB"})


def _spellings(role: str, value: str, *, code: bool = False) -> list[str]:
    """The ways a form may write one address value, the student's own first (shortest first when ``code``)."""
    spellings = [value]
    plain = _plain(value)
    if role == "state":
        abbreviation = next((short for short, name in US_STATES.items() if plain in {short.casefold(), name}), "")
        if abbreviation:
            spellings += [abbreviation, US_STATES[abbreviation].title()]
    elif role == "country":
        for names in COUNTRY_SPELLINGS:
            if plain in {_plain(name) for name in names}:
                spellings += names
    spellings = list(dict.fromkeys(spellings))
    return sorted(spellings, key=lambda text: (len(text), text not in COUNTRY_CODES)) if code else spellings


def _address_option(role: str, value: str, options: list[dict[str, str]]) -> dict[str, str] | None:
    """The list choice that is the student's own state or country, matched whole ("Kansas" is not "Arkansas")."""
    wanted = {_plain(spelling) for spelling in _spellings(role, value)}
    if role == "state":
        wanted |= {f"{short} {name}" for short in wanted for name in wanted if short != name}  # "TX - Texas"
    return next((
        option for option in options
        if option.get("value", "") != "" and not PLACEHOLDER_OPTION.search(option.get("text", ""))
        and (_plain(option.get("text", "")) in wanted or _plain(option.get("value", "")) in wanted)
    ), None)


def _address_value(role: str, identity: dict[str, str]) -> str:
    if role == "street_address":  # one box for both lines
        return ", ".join(part for part in (identity.get("address_line1", ""), identity.get("address_line2", "")) if part)
    return identity.get(role, "")


def _shown_answer(field: dict[str, Any]) -> dict[str, str] | None:
    """The choice a list already shows, when it is an answer and not a "Select…" prompt."""
    shown = next((option for option in field.get("options") or [] if option.get("value") == field.get("selected")), None)
    if shown and not PLACEHOLDER_OPTION.search(shown.get("text", "")) and shown.get("value", "") != "":
        return shown
    return None


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
    ``left`` holds each field a problem leaves empty (its index, label and
    role), which Finish in browser outlines for the student to fill.
    """
    fills: list[dict[str, Any]] = []
    problems: list[str] = []
    left: dict[int, tuple[str, str]] = {}
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
    missing_address = False

    def unanswerable(field: dict[str, Any], role: str = "") -> None:
        """One note per thing the form asks that the student has not confirmed, in one wording."""
        nonlocal missing_address
        label = label_of(field)
        left[field["index"]] = (label, _role(field))
        key = role if role in {"phone", "company", "link", "job_title"} else re.sub(r"[^a-z0-9]", "", label.lower())
        missing_address = missing_address or role in ADDRESS_ROLES
        if key in reported:
            return
        reported.add(key)
        problems.append(f"The form requires \"{label}\", and your confirmed profile has no answer for it")

    def stuck(field: dict[str, Any], note: str) -> None:
        """A field left empty because of ``note``."""
        left[field["index"]] = (label_of(field), _role(field))
        problems.append(note)

    # With a real message box on the form, a one-line box is a subject or a question, never the message.
    has_textarea = any(field["tag"] == "textarea" and field.get("visible", True) and _role(field) == "message" for field in fields)

    def put(field: dict[str, Any], role: str, value: str, action: str = "fill") -> None:
        fills.append({"index": field["index"], "action": action, "value": value, "role": role, "label": label_of(field)})
        used.add(role)

    first, _space, last = identity.get("name", "").strip().rpartition(" ")
    if not first:
        first, last = last, ""
    # "Name" beside a separate "Last name" field asks for the first name only.
    split_name = any(_role(field) == "last_name" for field in fields if field.get("visible", True))
    # The address boxes the form has, answered or not. With no box for the second line the street box takes both;
    # with no city, state or ZIP box it would have to hold the whole address in a format the app would invent.
    form_roles = {_role(field) for field in fields if field.get("visible", True) or (field["tag"] == "select" and field.get("required"))}
    has_line2 = bool({"address_line2", "street_address"} & form_roles)
    lone_street = not {"city", "state", "postal_code"} & form_roles
    street_roles = {"address_line1", "address_line2", "street_address"}

    def address_role_here(field: dict[str, Any]) -> str:
        role = _role(field)
        return "street_address" if role == "address_line1" and not has_line2 else role

    # A form that asks for a part of the address twice has a second address block (a reference's, an emergency
    # contact's), and nothing says which block is the student's: no address goes in at all, and each required
    # address box stops the send. Otherwise each part has one box.
    claimed: set[str] = set()
    two_blocks = False
    for field in fields:
        if field["type"] in {"hidden", "submit", "button", "image", "reset", "password"} or CAPTCHA_FIELD.search(_field_text(field)):
            continue
        if not field.get("visible", True) and not (field["tag"] == "select" and field.get("required")):
            continue
        role = address_role_here(field)
        if role in ADDRESS_ROLES:
            parts = {"address_line1", "address_line2"} if role == "street_address" else {role}
            two_blocks = two_blocks or bool(parts & claimed)
            claimed |= parts
    # Optional address boxes, filled from the same address once the form has required the street.
    optional_address: list[tuple[dict[str, Any], str]] = []
    typed_street = False

    def address_text(field: dict[str, Any], role: str, value: str) -> str | None:
        """The spelling of the student's value that fits the box: "TX" or "GB" for a box of two letters."""
        limit = field.get("maxlength")
        code = role == "country" and (_autocomplete(field) == "country" or limit in (2, 3))
        return next((text for text in _spellings(role, value, code=code) if not limit or len(text) <= limit), None)
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
                    stuck(field, f"The form asks a second question, \"{label_of(field)}\"")
                continue
            if limit and len(body) > limit:
                stuck(field, f"The message box takes {limit} characters and the draft is {len(body)}. Shorten the draft")
                used.add("message")
                continue
            if field["tag"] != "textarea" and NEWLINE in body.strip():
                stuck(field, "The form's message box is a single line, so the draft's paragraphs would run together")
                used.add("message")
                continue
            put(field, "message", body)
        elif role == "subject":
            if limit and len(subject) > limit:
                stuck(field, f"The subject box takes {limit} characters and the subject is {len(subject)}")
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
        elif role in ADDRESS_ROLES:
            # Like a phone number, the student's confirmed address goes in only where the form requires the box,
            # or where a list already shows a country or state that would be sent as theirs.
            is_list = field["tag"] == "select"
            role = address_role_here(field)
            if two_blocks:
                if required or (is_list and _shown_answer(field) is not None):
                    unanswerable(field)  # no role: which block is the student's is the student's to say
                continue
            if not (required or (is_list and _shown_answer(field) is not None)):
                optional_address.append((field, role))
                continue
            if role in street_roles and lone_street:
                unanswerable(field)  # no role: an address in the profile would not answer it either
                continue
            value = _address_value(role, identity)
            if not value:
                unanswerable(field, role)
            elif is_list:
                option = _address_option(role, value, [o for o in field.get("options") or [] if (field["index"], o.get("value", "")) not in avoid])
                if option is None:
                    stuck(field, f"The form requires a choice for \"{label_of(field)}\", and none of its choices is the {ADDRESS_WORDS[role]} in your profile")
                else:
                    fills.append({"index": field["index"], "action": "select", "value": option["value"], "role": role,
                                  "label": f"{label_of(field)}: {option.get('text', '')}"[:120]})
            else:
                fitting = address_text(field, role, value)
                if fitting is None:
                    stuck(field, f"The box \"{label_of(field)}\" takes {limit} characters, and the {ADDRESS_WORDS[role]} in your profile is longer")
                else:
                    put(field, role, fitting)
                    typed_street = typed_street or (required and role in {"address_line1", "street_address"})
        elif role == "select":
            options = field.get("options") or []
            # A list that already shows an answer ("Customer", "Strike systems") would
            # send that answer as the student's, so it is chosen like a required one.
            shown = _shown_answer(field)
            preset = shown is not None
            if not required and not preset:
                continue
            option = _choose_option([o for o in options if (field["index"], o.get("value", "")) not in avoid], label_of(field))
            if option is None and not (TOPIC_CHOICE.search(label_of(field)) or HEARD_CHOICE.search(label_of(field))):
                # A country, a state, an application area: facts about the student it has not confirmed.
                unanswerable(field)
            elif option is None:
                offered =", ".join(o.get("text", "") for o in options if o.get("value") and not PLACEHOLDER_OPTION.search(o.get("text", "")))[:120]
                what = f"shows \"{shown.get('text', '')}\" for" if preset and not required else "requires a choice for"
                stuck(field, f"The form {what} \"{label_of(field)}\" and none of its choices ({offered}) is true of you")
            else:
                fills.append({"index": field["index"], "action": "select", "value": option["value"], "role": "select",
                              "label": f"{label_of(field)}: {option.get('text', '')}"[:120]})
        elif role == "checkbox":
            text = _field_text(field)
            if required and CONSENT.search(text) and not MARKETING.search(text):
                fills.append({"index": field["index"], "action": "check", "value": "", "role": "consent", "label": label_of(field)})
            elif required:
                stuck(field, f"The form requires ticking \"{label_of(field)}\"")
        elif role == "file":
            if attachment and _accepts(field.get("accept", ""), attachment):
                put(field, "file", attachment, action="upload")
            elif required:
                stuck(field, "The form requires a file, and no resume is set to attach")
        elif required or (field["tag"] == "select" and _shown_answer(field) is not None):
            # A list that already shows an answer (a billing country) would send it as the student's.
            unanswerable(field, role)
    # A form that required the street address gets the rest of it in its optional address boxes, so the
    # apartment or the city is not left off; a box the student's address does not fit is left alone. A form
    # that required only a country, state, city or ZIP gets only that (the least the student discloses).
    for field, role in optional_address if typed_street else []:
        value = _address_value(role, identity)
        if not value or (role in street_roles and lone_street):
            continue
        if field["tag"] == "select":
            option = _address_option(role, value, [o for o in field.get("options") or [] if (field["index"], o.get("value", "")) not in avoid])
            if option is not None:
                fills.append({"index": field["index"], "action": "select", "value": option["value"], "role": role,
                              "label": f"{label_of(field)}: {option.get('text', '')}"[:120]})
        elif (fitting := address_text(field, role, value)) is not None:
            put(field, role, fitting)
    for name, group in radio_groups.items():
        if not any(field.get("required") for field in group):
            continue
        question = next((str(field.get("name") or "") for field in group), name)
        choice = _choose_option([{"value": str(field["index"]), "text": field.get("label", "")} for field in group], question or "unnamed")
        if choice is None:
            left.update((field["index"], (name, "radio")) for field in group)
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
    if missing_address:
        problems.append("Add your mailing address on the Profile page, under About you, and the app can fill in address boxes")
    return {"fills": fills, "problems": list(dict.fromkeys(problems)), "left": [{"index": index, "label": label, "role": role} for index, (label, role) in sorted(left.items())]}


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

# Finish in browser and a rehearsal: what a page sends as it closes is sent after the route handler is no longer asked
# (Playwright stalls a closing page's requests, and Chromium lets them go when the page's session ends), so the gate
# would be bypassed on every close: the student's, and the app's after the outcome or the time running out. The events
# that announce a close never reach the page's scripts (this listener is registered first, on the window, in the capture
# phase), a beacon is refused as the browser does when it cannot queue one, and a fetch is never kept alive past the
# page. The channels a route never sees at all (a peer connection, WebTransport, a WebSocket stream, fetchLater) are
# removed; WebSockets are closed at the context (FormSubmitter._start). A subset of Apply for me's NO_SIDE_CHANNELS
# (apply/agent.py), without the parts a contact form's CAPTCHA may need (its workers).
CLOSE_GUARD = r"""(() => {
  const call = Function.prototype.call.bind(Function.prototype.call);
  const apply = Reflect.apply, construct = Reflect.construct, reflectGet = Reflect.get, define = Object.defineProperty;
  for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'RTCDataChannel', 'fetchLater', 'FetchLaterResult',
                      'WebTransport', 'WebSocketStream']) {
    try { delete window[name]; } catch (error) { /* already gone */ }
    try { define(window, name, {value: undefined, configurable: false, writable: false}); } catch (error) { /* kept as it is */ }
  }
  const fix = (target, name, value) => { try { define(target, name, {value, configurable: false, writable: false, enumerable: false}); } catch (error) { /* kept */ } };
  try { fix(Navigator.prototype, 'sendBeacon', function () { return false; }); } catch (error) { /* no beacons */ }
  try {
    const plain = (init) => (init !== null && typeof init === 'object')
      ? new Proxy(init, {get: (target, key) => (key === 'keepalive' ? false : reflectGet(target, key, target))}) : init;
    const nativeFetch = window.fetch, NativeRequest = window.Request;
    fix(window, 'fetch', function (input, init) { return apply(nativeFetch, this, [input, plain(init)]); });
    const WrappedRequest = new Proxy(NativeRequest, {construct: (target, args, newTarget) => construct(target, [args[0], plain(args[1])], newTarget)});
    fix(NativeRequest.prototype, 'constructor', WrappedRequest);
    fix(window, 'Request', WrappedRequest);
  } catch (error) { /* no fetch */ }
  try {
    const addListener = EventTarget.prototype.addEventListener, stopNow = Event.prototype.stopImmediatePropagation;
    const swallow = (event) => { call(stopNow, event); };
    for (const type of ['pagehide', 'unload', 'beforeunload', 'visibilitychange', 'freeze', 'pageswap']) call(addListener, window, type, swallow, true);
  } catch (error) { /* no events */ }
})();"""
# Finish in browser, in the form's frame: outline the boxes the app left for the student.
OUTLINE_SCRIPT = r"""
(left) => {
  for (const index of left) {
    const box = document.querySelector(`[data-pipeline-field='${index}']`);
    if (!box) continue;
    box.style.setProperty("outline", "3px solid #d9480f", "important");
    box.style.setProperty("outline-offset", "2px", "important");
  }
}
"""
# Finish in browser: the student's own press of send, seen from a world the page cannot reach (as Apply for me's
# PRESS_LISTENER in apply/agent.py). The listener runs in an isolated world (a JavaScript realm of its own over the
# same page, made by the browser through the DevTools protocol), and the binding it calls exists in that world only,
# so no script on the page can call it, find its name, or change the built-ins it uses. It is registered on the
# window, in the capture phase, before any script of the page runs, so a page listener cannot stop the event first,
# and again in every new document of the window. A form in another site's frame is watched from when the frame is
# found; a press that frame's scripts stop first is not seen, and then nothing leaves (FormSubmitter._route). It
# says "ready" once in each document, which the app waits for before handing the form over.
#
# A press is a trusted click whose path (``composedPath``, so a send button drawn inside a custom element counts)
# holds the send button the app found (data-pipeline-submit) or a control that submits the <form> holding the
# message box, judged from the live page so a redraw or a reload, which drops the app's marks, does not lose a
# <form>'s own submit. A keyboard's Space or Enter on that button is a trusted click too. A press is also a trusted
# Enter in a one-line box of that <form>, which submits it. A click a script makes has ``isTrusted`` false, and
# nothing else (a newsletter's "Submit", a header's "Contact us", a help toggle, "Next",
# "Add a file") is a press, and neither is a press of a <form> the browser's own check stops (a required box empty),
# which sends nothing. It carries what each marked box holds then.
PRESS_WORLD = "outreach-student-press"
PRESS_BINDING = "outreachStudentPress"
PRESS_LISTENER = r"""(() => {
  const values = () => {
    const held = {};
    for (const el of document.querySelectorAll("[data-pipeline-field]")) {
      held[el.getAttribute("data-pipeline-field")] = el.type === "checkbox" || el.type === "radio" ? (el.checked ? "on" : "") : String(el.value || "");
    }
    return held;
  };
  const tell = (kind) => {
    try { window.__BINDING__(JSON.stringify({ kind, values: kind === "press" ? values() : {} })); } catch (_error) { /* the page is going away */ }
  };
  const widget = (b) => b.hasAttribute("aria-haspopup") || b.hasAttribute("aria-expanded") || Boolean(b.closest("[role=listbox], [role=combobox], select"));
  const holdsMessage = (node) => Boolean(node && node.querySelector("textarea"));
  const submits = (el) => (el.matches("button") && el.type === "submit") || el.matches("input[type=submit], input[type=image]");
  // A <form> the browser's own check will stop (a required box empty) fires no submit: pressing its submit button sends nothing.
  const stopped = (el) => submits(el) && el.form && !el.form.noValidate && !el.formNoValidate && el.form.matches(":invalid");
  const sendControl = (el) => el.matches("[data-pipeline-submit]") || (submits(el) && !widget(el) && holdsMessage(el.form));
  window.addEventListener("click", (event) => {
    if (!event.isTrusted) return;
    const control = event.composedPath().find((node) => node instanceof Element && sendControl(node));
    if (control && !stopped(control)) tell("press");
  }, true);
  window.addEventListener("keydown", (event) => {
    const box = event.target;
    if (!event.isTrusted || event.key !== "Enter" || !(box instanceof Element) || !box.matches("input")) return;
    const form = box.form;
    if (form && holdsMessage(form) && !(form.matches(":invalid") && !form.noValidate)
        && Array.from(form.elements).some((el) => el instanceof Element && submits(el))) tell("press");
  }, true);
  tell("ready");
})();""".replace("__BINDING__", PRESS_BINDING)
# Finish in browser, in a corner of the page: what is left for the student and how long the window stays, in the app's
# words, apart from the site's styles. Clicks pass through it to the page, except on its own Hide button, so it never
# stands between them and the form.
NOTE_SCRIPT = r"""
({ lines, seconds, title: heading }) => {
  document.querySelectorAll("[data-pipeline-note]").forEach((old) => old.remove());
  const host = document.createElement("div");
  host.setAttribute("data-pipeline-note", "");
  host.style.cssText = "all:initial;position:fixed;right:12px;bottom:12px;z-index:2147483647;width:min(380px,calc(100vw - 24px));pointer-events:none;";
  const panel = document.createElement("div");
  panel.setAttribute("role", "status");
  panel.style.cssText = "font:14px/1.45 system-ui,sans-serif;color:#1b1b1b;background:#fff8e6;border:2px solid #d9480f;"
    + "border-radius:8px;padding:10px 14px;box-shadow:0 4px 16px rgba(0,0,0,.25);pointer-events:none;";
  const title = document.createElement("strong");
  title.textContent = heading || "Your turn: nothing is sent until you press the form's send button.";
  panel.append(title);
  const clock = document.createElement("div");
  clock.style.cssText = "margin-top:4px;";
  const ends = Date.now() + seconds * 1000;
  const tick = () => {
    const left = Math.max(0, Math.round((ends - Date.now()) / 1000));
    clock.textContent = `This window closes in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}.`;
  };
  tick();
  setInterval(tick, 1000);
  panel.append(clock);
  if (lines.length) {
    const list = document.createElement("ul");
    list.style.cssText = "margin:6px 0 0;padding-left:18px;";
    for (const line of lines) {
      const item = document.createElement("li");
      item.textContent = line;
      list.append(item);
    }
    panel.append(list);
  }
  const hide = document.createElement("button");
  hide.type = "button";
  hide.textContent = "Hide this note";
  hide.style.cssText = "margin-top:8px;font:inherit;padding:2px 10px;cursor:pointer;pointer-events:auto;";
  hide.addEventListener("click", () => host.remove());
  panel.append(hide);
  host.attachShadow({ mode: "open" }).append(panel);
  document.documentElement.append(host);
}
"""


class FormSubmitter:
    """Chromium through Playwright, behind the same request guard as outreach_render.

    Headless unless ``headed``: a headed browser opens a window the student can
    see. With ``person_wait`` the form is the student's to finish and send in
    that window, for that many seconds (Finish in browser; see
    _hand_to_student); without it the app presses send. With ``rehearse`` it does everything
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
        student_hook: Callable[[Any], None] | None = None,
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
        # Tests act as the student through this: it is given the page once the window is theirs.
        self._student_hook = student_hook
        # Finish in browser: where the student's presses go once the window is theirs (None before, and after);
        # which sessions' listeners said they are in place; and whether only reads may leave the page (all but
        # while the window is the student's).
        self._press_sink: Callable[[str, dict[str, Any]], None] | None = None
        self._ready: set[str] = set()
        # The gate (``_route``): "open" lets everything through (the app's own press, or the student's once seen);
        # "load", from page load to the app's first fill, holds only what carries the student's details, since
        # nothing of theirs is on the page yet (a site's own check, such as Cloudflare's, may post then); "closed",
        # from the first fill until the student's press and again once the outcome is decided, holds every request
        # but a read and a CAPTCHA's own call, and anything carrying their details.
        self._gate = "open"
        self._sites: set[str] = set()
        # What only the student's form would carry (``_needles``), and the window, for the gate; and what the gate held back
        # (method and host), which the window's note and the history name, so a form that will not send explains itself.
        self._needles: list[str] = []
        self._window: Any = None
        self.held_back: list[str] = []
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
        # A route never sees a WebSocket, so each is refused by never connecting it to its server, as Apply for me's
        # are (apply/agent.py _refuse_socket): the page's socket goes nowhere. Closing it from inside this handler
        # would deadlock the sync API.
        if hasattr(self._context, "route_web_socket"):
            self._context.route_web_socket("**/*", lambda _socket: None)
        if self.person_wait or self.rehearse:
            self._context.add_init_script(CLOSE_GUARD)

    def _route(self, route: Any) -> None:
        request = route.request
        if self.rehearse and request.method != "GET" and not CAPTCHA_ENDPOINTS.search(request.url):
            self.refused.append(f"{request.method} {request.url}")
            route.abort("blockedbyclient")
            return
        # Finish in browser fails closed: until the student's press is seen, and once the window's outcome is decided,
        # nothing that could carry the form leaves, so a press the app did not see sends nothing.
        if self._gate != "open" and self._could_carry(request) and not self._press_arrives():
            if _carries(request, self._needles) or any(same_site(request.url, site) for site in self._sites):
                # What the window's note and the history name: not a third party's beacon, which no form needs.
                self.held_back.append(f"{request.method} {urlsplit(request.url).hostname or ''}")
            route.abort("blockedbyclient")
            return
        (self._route_hook or self._guard)(route)

    def _could_carry(self, request: Any) -> bool:
        """Anything carrying the student's details, whatever it is and wherever it goes (a CAPTCHA's own address too);
        and once the app has filled the form ("closed"), any request but a read and a CAPTCHA's own call, and a
        script's read of the form's own site (a lookup, which can carry what was typed in ways the app cannot see)."""
        if _carries(request, self._needles):
            return True
        if self._gate == "load" or CAPTCHA_ENDPOINTS.search(request.url):
            return False
        if request.method != "GET":
            return True
        return getattr(request, "resource_type", "") in {"fetch", "xhr", "ping", "eventsource", "other"} and any(
            same_site(request.url, site) for site in self._sites)

    def _press_arrives(self) -> bool:
        """While the window is the student's, wait up to PRESS_GRACE_SECONDS for a press that opens the gate. Page events run meanwhile."""
        if self._press_sink is None or self._window is None:
            return False
        deadline = time.monotonic() + PRESS_GRACE_SECONDS
        while self._gate != "open" and time.monotonic() < deadline:
            try:
                self._window.wait_for_timeout(20)
            except Exception:  # noqa: BLE001 - the window is going away: the request is refused
                break
        return self._gate == "open"

    def _guard(self, route: Any) -> None:
        if not request_allowed(route.request.url, self._resolve, self._allowed):
            route.abort("blockedbyclient")
            return
        route.continue_()

    def submit(
        self, page_url: str, *, identity: dict[str, str], subject: str, body: str, attachment: str = "", name: str = "form",
        should_continue: Callable[[], bool] | None = None, on_press: Callable[[], Any] | None = None,
        tried_before: bool = False,
    ) -> dict[str, Any]:
        """Fill and send the form. Never raises: the outcome says whether anything left the page.

        ``should_continue`` is asked just before the send button is pressed;
        False (or failing to answer) stops there, with nothing sent and
        ``paused`` set on the result. Automatic sends pass it to honour a pause.
        ``on_press`` is told when the student presses send in Finish in browser
        (``person_wait``); it cannot stop a press that has already happened.
        ``tried_before`` (the student said their last press did not send it) puts
        a warning in the window that the form may already have the message.
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
            self._ready = set()
            self._gate = "load" if self.person_wait else "open"
            self._sites = {website_domain(page_url)} - {""}
            self._needles = _needles(identity, subject, body)
            self.held_back = []
            page = self._context.new_page()
            self._window = page
            # Before the page loads, so the press listener is in place ahead of every script of the page.
            watching = bool(self.person_wait) and self._watch_presses(page, "page") == "watched"
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
            if self.person_wait:
                clicked = self._hand_to_student(
                    page, frame, read, result, identity=identity, subject=subject, body=body, attachment=attachment,
                    name=name, on_press=on_press, watching=watching, tried_before=tried_before,
                )
                return result
            purpose = _sales_purpose(read)
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
                    try:
                        self._apply(frame, fill)
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
                try:
                    self._apply(frame, fill)
                    if fill["action"] == "upload":
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
            self._press_sink = None
            self._gate = "closed" if self.person_wait else "open"
            self._window = None
            if page is not None:
                if clicked or result["outcome"] == "needs_you":
                    result["screenshot"] = self._screenshot(page, name)
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _apply(frame: Any, fill: dict[str, Any]) -> None:
        """Make one planned fill on the page. Raises when the page will not take it."""
        control = frame.locator(f"[data-pipeline-field='{fill['index']}']").first
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

    def _watch_presses(self, target: Any, tag: str) -> str:
        """Have the browser report the student's presses in ``target`` (the page, or a frame of another site), from PRESS_WORLD.

        "watched" when the session is made (its listener's "ready" is then recorded under ``tag``), "shared" for a
        frame in its page's own process, which the page's session watches already, and "" when no session could be
        made: Finish in browser then does not hand the form over, since it could not tell the student's press.
        """
        try:
            cdp = self._context.new_cdp_session(target)
        except Exception as exc:  # noqa: BLE001 - a frame in its page's process, or a browser without DevTools sessions
            return "shared" if "does not have a separate CDP session" in str(exc) else ""
        try:
            cdp.on("Runtime.bindingCalled", lambda event: self._on_press_binding(event, tag))
            cdp.send("Page.enable")
            cdp.send("Runtime.enable")
            cdp.send("Runtime.addBinding", {"name": PRESS_BINDING, "executionContextName": PRESS_WORLD})
            cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": PRESS_LISTENER, "worldName": PRESS_WORLD, "runImmediately": True})
        except Exception:  # noqa: BLE001 - a session that would not take the listener watches nothing
            return ""
        return "watched"

    def _on_press_binding(self, event: dict[str, Any], tag: str) -> None:
        """The press listener called its binding, which exists only in the listener's world: ready, a press, or a submit."""
        if event.get("name") != PRESS_BINDING:
            return
        try:
            said = json.loads(event.get("payload") or "{}")
        except ValueError:
            return
        if not isinstance(said, dict):
            return
        if said.get("kind") == "ready":
            self._ready.add(tag)
        elif said.get("kind") in {"press", "submit"} and self._press_sink is not None:
            values = said.get("values")
            self._press_sink(said["kind"], values if isinstance(values, dict) else {})

    def _fill_for_student(self, frame: Any, fills: list[dict[str, Any]], result: dict[str, Any], notes: list[str]) -> None:
        """Make each fill the page takes; one it will not take is named for the student, not a reason to stop."""
        for fill in fills:
            try:
                self._apply(frame, fill)
            except Exception:  # noqa: BLE001 - covered by another element, read-only, or a choice the page refused
                notes.append(f"The field \"{fill['label']}\" would not take what the app filled in")
                continue
            result["filled"].append(fill["label"])
            if fill["action"] == "upload":
                result["attached"] = Path(fill["value"]).name

    def _hand_to_student(
        self, page: Any, frame: Any, read: dict[str, Any], result: dict[str, Any], *, identity: dict[str, str],
        subject: str, body: str, attachment: str, name: str, on_press: Callable[[], Any] | None, watching: bool,
        tried_before: bool = False,
    ) -> bool:
        """Finish in browser: fill what the app truthfully can, then the window is the student's until they press send.

        What the app cannot answer stays empty and outlined, and a note on the page names what is left and counts down
        the time; a sales form gets nothing. The app never presses send here, and never ticks a CAPTCHA (a ticked one
        would expire while they type). The form is handed over only with a send button found and its frame's press
        listener (PRESS_LISTENER) in place.

        It fails closed (``_route``): from the app's first fill until the student's first press of the form's send
        button, nothing that could carry the form leaves, so with no press seen the outcome is needs_you, nothing
        sent, with what was held back named. The first press opens the way and tells ``on_press``, and from then on the
        app does not judge: whatever the page says, the outcome is unconfirmed with FORM_PRESSED_NOTE, and the card
        asks the student whether it went. Returns whether they pressed; ``result`` holds the outcome.
        """
        if not watching:
            result.update(outcome="needs_you", note=NOT_WATCHED)
            return False
        if not read.get("submit"):
            result.update(outcome="needs_you", note=NO_SEND_BUTTON)
            return False
        self._gate = "closed"  # from the first fill on, what the app puts in stays in the window until the student's press
        self._sites |= {website_domain(frame.url)} - {""}
        notes: list[str] = []
        plan: dict[str, Any] = {"fills": [], "left": []}
        purpose = _sales_purpose(read)
        if purpose:
            notes.append(f"This form is for sales (\"{purpose.group(0)}\"), not general contact, so the app filled in nothing")
            plan["own"] = True
        else:
            plan = plan_fill(read["fields"], identity, subject=subject, body=body, attachment=attachment)
            # Choices go in first: some sites redraw the form when one changes, so it is read and planned again.
            choices = [fill for fill in plan["fills"] if fill["action"] in {"select", "check"}]
            self._fill_for_student(frame, choices, result, notes)
            if choices:
                page.wait_for_timeout(500)
                again = frame.evaluate(EXTRACT_SCRIPT)
                if again and not again.get("hidden"):
                    plan = plan_fill(again["fields"], identity, subject=subject, body=body, attachment=attachment)
                else:
                    notes.append("A choice the app made took the form off the page")
            notes[:0] = plan["problems"]
            typed = [fill for fill in plan["fills"] if fill["action"] not in {"select", "check"}]
            self._fill_for_student(frame, typed, result, notes)
        # What each box the app typed into holds now, read back: the student's own changes are told apart from these.
        held: dict[str, str] = {}
        for fill in plan["fills"]:
            if fill["action"] != "fill" or fill["label"] not in result["filled"]:
                continue
            try:
                held[str(fill["index"])] = frame.locator(f"[data-pipeline-field='{fill['index']}']").first.input_value()
            except Exception:  # noqa: BLE001 - the box went with a redraw
                continue
            changed = formatting_problem(fill["value"], held[str(fill["index"])])
            if changed:
                notes.append(f"The form changed what the app typed into \"{fill['label']}\": {changed}. Check it before you send")
        result["filled_screenshot"] = self._screenshot(page, f"{name}-filled", frame.locator("[data-pipeline-form]").first)

        # The form's frame must have said its listener is in place: another site's frame has a session of its own.
        tag = "page" if frame is page.main_frame else {"watched": "frame", "shared": "page"}.get(self._watch_presses(frame, "frame"), "")
        ready_by = time.monotonic() + READY_WAIT_SECONDS
        while tag and tag not in self._ready and time.monotonic() < ready_by:
            page.wait_for_timeout(100)
        if not tag or tag not in self._ready:
            result.update(outcome="needs_you", note=NOT_WATCHED)
            return False

        presses: list[dict[str, Any]] = []

        def heard(kind: str, values: dict[str, Any]) -> None:
            if kind == "press":
                presses.append(values)
                self._gate = "open"  # the student pressed send: what the form sends may leave

        def held_line() -> list[str]:
            hosts = sorted({entry.split(" ", 1)[1] for entry in self.held_back if " " in entry})
            if not hosts:
                return []
            return [f"Before your press, the app held back what this page tried to send to {', '.join(hosts)}. "
                    "If the form will not let you send, that is why: close this window and send it from the page in your own browser."]

        self._press_sink = heard
        frame.evaluate(OUTLINE_SCRIPT, [entry["index"] for entry in plan["left"]])
        lines = ([TRIED_BEFORE] if tried_before else []) + (["Fill in the boxes outlined in orange."] if plan["left"] else []) + notes + [
            "If pressing send does nothing, the app did not see your press and nothing left: close this window."]
        try:
            page.evaluate(NOTE_SCRIPT, {"lines": lines + held_line(), "seconds": int(self.person_wait)})
        except Exception:  # noqa: BLE001 - the note is a help, not the hand-over
            pass
        page.bring_to_front()
        if self._student_hook is not None:
            self._student_hook(page)

        deadline = time.monotonic() + self.person_wait
        told, closed, said_held = False, False, len(self.held_back)
        try:
            while True:
                if presses and not told:
                    told = True
                    if on_press is not None:
                        try:
                            on_press()
                        except Exception:  # noqa: BLE001 - the press has happened; recording it cannot undo it
                            pass
                    try:
                        page.evaluate(NOTE_SCRIPT, {
                            "lines": ["When you have seen what the page says, close this window. The app will ask you whether your message was sent."],
                            "seconds": max(0, int(deadline - time.monotonic())), "title": "You pressed send."})
                    except Exception:  # noqa: BLE001
                        pass
                if page.is_closed():
                    closed = True
                    break
                if not presses and len(self.held_back) > said_held:
                    said_held = len(self.held_back)
                    try:
                        page.evaluate(NOTE_SCRIPT, {"lines": lines + held_line(), "seconds": max(0, int(deadline - time.monotonic()))})
                    except Exception:  # noqa: BLE001
                        pass
                if time.monotonic() >= deadline:
                    break
                page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001 - the student closed the window
            closed = True
        self._gate = "closed"
        if presses:
            # The app does not judge what the page said: the student does, on the card.
            result.update(outcome="unconfirmed", confirmation="", note=FORM_PRESSED_NOTE)
            self._note_students_part(result, plan, held, presses[-1])
        else:
            minutes = max(1, round(self.person_wait / 60))
            parts = ["You closed the window before the form was sent. Nothing was sent" if closed else
                     f"The window waited {minutes} minute{'s' if minutes != 1 else ''} and the form was not sent. Nothing was sent"]
            if notes:
                parts.append("Left for you: " + "; ".join(notes))
            hosts = sorted({entry.split(" ", 1)[1] for entry in self.held_back if " " in entry})
            if hosts:
                parts.append(f"The page tried to reach {', '.join(hosts)} before your press and the app held it back, "
                             "so this form may not send from Finish in browser: send it from the page in your own browser")
            result.update(outcome="needs_you", note=". ".join(parts))
        result["held_back"] = list(self.held_back)
        return bool(presses)

    @staticmethod
    def _note_students_part(result: dict[str, Any], plan: dict[str, Any], held: dict[str, str], values: dict[str, Any]) -> None:
        """What the student filled in, and where what went is their own text, from what the boxes held at their press.

        Their own text is a box of the app's they changed, a message or subject box the app left for them, or the
        whole form when the app filled nothing in (a sales form).
        """
        def text(value: Any) -> str:
            return str(value or "").replace(CRLF, NEWLINE)

        result["by_you"] = list(dict.fromkeys(
            entry["label"] for entry in plan["left"] if text(values.get(str(entry["index"]))).strip()
        ))
        changed = [
            fill["label"] for fill in plan["fills"]
            if str(fill["index"]) in held and str(fill["index"]) in values and text(values[str(fill["index"])]) != text(held[str(fill["index"])])
        ]
        wrote = [entry["label"] for entry in plan["left"]
                 if entry.get("role") in {"message", "subject"} and text(values.get(str(entry["index"]))).strip()]
        result["changed"] = list(dict.fromkeys([*changed, *wrote]))
        if plan.get("own"):
            said = "The app filled in nothing, so if it went, it is your own text, not the approved draft"
        elif result["changed"]:
            named = ", ".join(f"\"{label}\"" for label in result["changed"])
            said = f"What you had in {named} is your own text, so if it went, it is not the approved draft word for word"
        else:
            return
        note = result.get("note") or ""
        result["note"] = f"{note}{'' if note.endswith(('?', '.')) else '.'} {said}" if note else said

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
        """Tick a checkbox CAPTCHA. A harder one is the student's, under Finish in browser.

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
            return (f"The form's {name} asked for a picture challenge. Nothing was sent. "
                    "Use Finish in browser to solve it and send the form yourself")
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
        what = "After the app pressed send, the page moved on" if form_gone else "After the app pressed send, something left the page"
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


def _sales_purpose(read: dict[str, Any]) -> re.Match[str] | None:
    """The words that make a form one for buying ("Request a demo"), or None for a contact form.

    Read from the form's own words and heading, not its dropdown's options; a
    heading that also says contact ("Contact us / Book a demo") is a contact form.
    """
    heading = str(read.get("heading") or "")
    return None if CONTACT_LINK.search(heading) else SALES_FORM.search(f"{heading}\n{str(read.get('text') or '')[:400]}")


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


def _needles(identity: dict[str, str], subject: str, body: str) -> list[str]:
    """What only the student's form would carry: everything of theirs the app may type that is theirs alone.

    Their email, name, phone (as typed and as digits), school, link and street address, the subject and the
    message's opening words. Words many pages hold anyway (their title "Student", a state, a country, a city, a ZIP)
    are left out, so a page's own requests are not held for them.
    """
    opening = next((line.strip() for line in body.splitlines() if line.strip()), "")[:40]
    phone = str(identity.get("phone", "") or "")
    digits = "".join(char for char in phone if char.isdigit())
    found = [identity.get(key, "") for key in ("email", "name", "school", "link", "address_line1", "address_line2")]
    found += [phone, digits if len(digits) >= 7 else "", subject, opening]
    return list(dict.fromkeys(str(text).strip().casefold() for text in found if len(str(text).strip()) >= 6))


def _carries(request: Any, needles: list[str]) -> bool:
    """Whether a request carries any of ``needles``, as typed or form-encoded, in its body or its address.

    The body is read as bytes, so one with a file in it (a résumé) is still searched; a body the browser does not
    show (a multipart upload it keeps back) is taken as carrying them, since it may.
    """
    if not needles:
        return False
    texts = [unquote_plus(str(getattr(request, "url", "") or ""))]
    try:
        body = request.post_data_buffer
    except Exception:  # noqa: BLE001 - a request with no body to give
        body = None
    if body:
        raw = body.decode("utf-8", "replace")
        texts += [raw, unquote_plus(raw)]
    elif getattr(request, "method", "GET") not in {"GET", "HEAD"}:
        try:
            kind = str((request.headers or {}).get("content-type", "")).lower()
        except Exception:  # noqa: BLE001
            kind = ""
        if kind.startswith("multipart/"):
            return True
    seen = " ".join(texts).casefold()
    return any(needle in seen for needle in needles)


def _refused(request: Any) -> bool:
    response = _response(request)
    return response is not None and 400 <= response.status < 500


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
    # The mailing address, if they confirmed one: the form gets it only for a box it requires (plan_fill).
    address = {key: " ".join(str(contact.get(key) or "").split()) for key in ADDRESS_FIELDS}
    return {
        "name": name, "email": email, "phone": str(contact.get("phone") or "").strip(),
        "school": str(facts.get("school") or "").strip(), "link": link, "title": "Student", **address,
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
            "The form may have been sent before, but the page did not say it arrived. Look for a confirmation email from them: "
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
    tried_before = False
    existing = send_claim_row(conn, target_id, user_id, "initial")
    if existing is not None:
        if send_claim_held(existing) or _click_held(existing):
            raise SendConflictError(FORM_IN_PROGRESS)
        if existing["state"] == "sent":
            raise ValueError("This first message was already sent")
        # A claim left mid-click (the app stopped as the button was pressed) may have sent the form.
        if existing["state"] in {"unconfirmed", automation.FORM_HANDED_OVER} and not retry_unconfirmed:
            raise SendNeedsCheckError(
                "An earlier send of this message may have gone out. Check before sending it again.", "form-unconfirmed",
            )
        # The student said an earlier try did not go ("No, it was not sent", or a checked resend): the history says
        # so, and Finish in browser warns them the form may already have it.
        tried_before = existing["state"] in {"unconfirmed", automation.FORM_HANDED_OVER}
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

        def student_pressed() -> None:
            """Finish in browser: the student pressed send, so from here the form may have gone, as after the app's own press.

            The form reads 'unconfirmed' too, until the outcome is written over it: after a restart in between, the card
            then offers "It arrived" and a checked resend, as for any send that may have gone.
            """
            with conn:
                conn.execute(
                    "UPDATE outreach_send_claims SET state=?, claimed_at=? WHERE target_id=? AND kind='initial' AND token=? AND state='drafting'",
                    (automation.FORM_HANDED_OVER, utc_now(), target_id, token),
                )
                conn.execute(
                    "UPDATE outreach_contact_forms SET state='unconfirmed', note=?, updated_at=? WHERE target_id=? AND user_id=?",
                    (FORM_PRESSED_NOTE, utc_now(), target_id, user_id),
                )

        submit_options: dict[str, Any] = (
            {"should_continue": hand_over} if automatic else {"on_press": student_pressed, "tried_before": tried_before} if in_browser else {}
        )
        if tried_before:
            with conn:
                log_event(conn, target_id, user_id, SAID_NOT_SENT_EVENT, detail=json.dumps({"in_browser": in_browser}))
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
        if in_browser:
            # What the student filled in the window, and which of the app's boxes they changed before sending.
            # And what the window held back before their press: a form that would not send can say why.
            detail.update(by_you=result.get("by_you", []), changed=result.get("changed", []), held_back=result.get("held_back", []))
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
