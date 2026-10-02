"""What an email says in itself: its text, its headers, its addresses and its links. Standard library only.

A leaf module. Nothing here opens a database, calls Gmail or knows a company, so
the job-mail reader, the reply reader, the delivery and send watchers and
``pipeline_mailbox`` (which must start without httpx or Fernet) all use the same
primitives. Who an address belongs to is ``outreach_identity``'s; whether a
sender can be trusted is ``mail_trust``'s; talking to Gmail is
``gmail_client``'s.

Look-alikes that mean different things, kept apart on purpose:

- ``html_text_spaced`` (job mail: a tag becomes a space, quoted HTML is kept)
  and ``html_text_reply`` (replies: a tag is removed, everything from the first
  ``<blockquote>`` on is dropped, since a quoted email is not what the sender wrote).
- ``has_list_headers`` (job mail's "bulk": List-Unsubscribe, List-Id or a bulk,
  list or junk Precedence) and ``is_bulk_or_generated`` (replies: also any
  Auto-Submitted other than "no" or "auto-replied").
- ``received_or_epoch`` (job mail: Gmail's internalDate, else 1970, so a message
  with no time looks older than anything and is only ever proposed) and
  ``received_or_none`` (replies: internalDate, then the Date header, then None,
  which the caller reads as "now").
- ``hosts_in`` (every link of a plain text, a set), ``link_hosts_or_none`` (every link of every text
  part of a message, None when any cannot be read: fails closed) and ``clean_url`` (one link,
  unescaped, with its sentence punctuation cut). ``unquoted_link_hosts`` is the hosts of what the sender
  wrote (href and src of the HTML, plus text links, quoted email left out) for judging who sent a
  message. application_inbox's Mail.link_hosts is a list,
  never None.
- ``header_map`` (a Gmail API message's headers by lowercased name, the last of a
  repeated header winning) and ``addresses`` and ``sender`` (a parsed
  EmailMessage's headers). pipeline_mailbox keeps its own first-match, one-line
  header reader: that one is for display.
"""

from __future__ import annotations

import base64
import html
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import urlsplit

# Failure notices come from one of these local parts (outreach_delivery reads them; outreach_inbox leaves them alone).
MAILER_DAEMONS = frozenset({"mailer-daemon", "mailerdaemon", "mail-daemon", "postmaster"})

URL = re.compile(r"""https?://[^\s<>"'`]+""", re.IGNORECASE)
# Sentence punctuation that ends a link in running text without being part of it.
URL_TAIL = ".,;:!?)]}'\""
_QUERY = re.compile(r"(https?://[^\s?#]+)[?#][^\s]*")


def decode_base64url(text: str) -> bytes:
    """Gmail's base64url (a raw message, a body part), with the padding it leaves off put back. ValueError when it is not base64."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def header_map(message: dict[str, Any], *, guarded: bool = False) -> dict[str, str]:
    """A Gmail API message's headers by name, lowercased; the last of a repeated header wins.

    ``guarded`` also skips a header item that is not an object and folds the
    name with casefold: for a caller that reads whatever Gmail sends rather than
    failing on a malformed answer. The two agree for ASCII header names.
    """
    items = (message.get("payload") or {}).get("headers") or []
    if guarded:
        return {str(item.get("name", "")).casefold(): str(item.get("value", "")) for item in items if isinstance(item, dict)}
    return {str(item.get("name", "")).lower(): str(item.get("value", "")) for item in items}


def host_of(url: str) -> str:
    """The host of a URL, lowercased and without a trailing dot; '' when it has none or cannot be read."""
    try:
        return (urlsplit(str(url or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def clean_url(url: str) -> str:
    """A link as found in text: HTML entities undone, sentence punctuation cut off its end."""
    return html.unescape(url).rstrip(URL_TAIL)


def strip_queries(text: str) -> str:
    """For anything logged: a URL keeps its host and path, never its query string."""
    return _QUERY.sub(r"\1", str(text or ""))


def mailbox_key(address: str) -> str:
    """An address as a mailbox: lowercased, without a +tag, and Gmail's dots and googlemail.com folded away.

    Whether two spellings are one mailbox. Other modules compare the student's
    own address with a plain casefold or lower (application_inbox,
    outreach_labels, outreach_gmail's wrong-account check), which differs for a
    +tagged address; that is reported, not unified here.
    """
    text = str(address or "").strip().casefold()
    if "@" not in text:
        return text
    local, domain = text.rsplit("@", 1)
    local = local.split("+", 1)[0] or local
    if domain in {"gmail.com", "googlemail.com"}:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def received_or_epoch(data: dict[str, Any]) -> datetime:
    """When Gmail received a message (its internalDate), or the epoch when it gives no time."""
    return datetime.fromtimestamp(int(data.get("internalDate") or 0) / 1000, tz=timezone.utc)


def has_list_headers(message: EmailMessage) -> bool:
    """List-Unsubscribe or List-Id, or a bulk, list or junk Precedence. Not is_bulk_or_generated: no Auto-Submitted rule."""
    return bool(message.get("List-Unsubscribe") or message.get("List-Id")) or str(
        message.get("Precedence", "")
    ).strip().lower() in {"bulk", "list", "junk"}


def _line_broken(markup: str) -> str:
    """Markup without its scripts and styles, a line break where a paragraph, row or <br> ended."""
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", markup)
    return re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", markup)


def html_text_spaced(markup: str) -> str:
    """HTML as text for job mail: each tag is a space, and quoted HTML stays."""
    return html.unescape(re.sub(r"<[^>]+>", " ", _line_broken(markup)))


def html_text_reply(markup: str) -> str:
    """HTML as text for a reply: each tag is removed, and everything from the first <blockquote> on is dropped."""
    markup = re.sub(r"(?is)<blockquote\b.*", "", _line_broken(markup))
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


# "Re:", "RE[2]:", "[Acme] Re:", and replies in other languages (AW, SV, Antw, VS, Rif, Odp, Ynt).
# Forwards are not answers: a forward is fresh content.
_ANSWER_SUBJECT = re.compile(r"^\s*(\[[^\]]{1,60}\]\s*)*(re|aw|sv|antw|vs|rif|odp|ynt)\s*(\[\d+\]|\(\d+\))?\s*[:：]", re.IGNORECASE)
_AUTO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto(matic)?[- ]?reply|auto:|out of (the )?office|ooo\b|away from|automatische antwort|"
    r"abwesenheit|r[ée]ponse automatique|absence|respuesta autom[áa]tica|fuera de la oficina|risposta automatica|"
    r"resposta autom[áa]tica|automatisch antwoord|automatiskt svar|autosvar)",
    re.IGNORECASE,
)
# Where the quoted email starts in a reply: "On Fri, ... wrote:", "-----Original Message-----",
# Outlook's "From: ... Sent: ..." header block, or a "> " line.
_ON_WROTE = re.compile(r"^\s*On .{0,300}wrote:\s*$", re.IGNORECASE | re.DOTALL)
_ORIGINAL = re.compile(r"^\s*-{2,}\s*(original message|forwarded message)\s*-{2,}\s*$", re.IGNORECASE)


# --- The text of a message ---------------------------------------------------------


def _quote_start(lines: list[str]) -> tuple[int, str] | None:
    """Where the quoted email starts, and how: "quote" (a "> " line), "wrote" or "wrote2" (an "On ... wrote:"
    line, or one split over two lines), or "header" (Outlook's From:/Sent: block, or an Original Message line)."""
    for index, line in enumerate(lines):
        pair = f"{line} {lines[index + 1]}" if index + 1 < len(lines) else line
        header_block = line.startswith("From:") and any(
            following.startswith(("Sent:", "Date:")) for following in lines[index + 1:index + 3]
        )
        if line.lstrip().startswith(">"):
            return index, "quote"
        if _ORIGINAL.match(line) or header_block:
            return index, "header"
        if _ON_WROTE.match(line):
            return index, "wrote"
        if line.lstrip().startswith("On ") and _ON_WROTE.match(pair):
            return index, "wrote2"
    return None


def strip_quoted(text: str) -> str:
    """The reply above the email it quotes."""
    lines = text.replace("\r\n", "\n").split("\n")
    found = _quote_start(lines)
    if found is not None:
        lines = lines[:found[0]]
    return "\n".join(lines).strip()


def written_between_quotes(text: str, sent: Iterable[str] = ()) -> str:
    """What the sender wrote after the email they quote begins: between its quoted lines, or below them.

    strip_quoted keeps only what is above the quote, so an answer typed inline
    ("> Would you have time for a call?" then "Sure, Thursday?") is lost there.
    A "> " quote marks its lines. An Outlook-style quote (a From:/Sent: block,
    or an Original Message line) does not, so below it every line that is not
    a header field and not the student's own words (``sent``: the emails they
    sent, which it quotes) counts as written by the sender. With nothing in
    ``sent``, every line below such a header counts: nothing is ruled out.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    found = _quote_start(lines)
    if found is None:
        return ""
    index, how = found
    if how == "header":
        return _written_below_header(lines[index:], sent)
    rest = lines[index + {"quote": 0, "wrote": 1, "wrote2": 2}[how]:]
    return "\n".join(line for line in rest if line.strip() and not line.lstrip().startswith(">")).strip()


# The fields of an Outlook header block ("From:", "Sent:", "To:", "Subject:"), with or without bold marks.
_HEADER_FIELD = re.compile(r"^\s*\**\s*(from|sent|date|to|cc|bcc|subject|importance|reply-to)\s*:", re.IGNORECASE)
_QUOTE_MARKS = re.compile(r"^[\s>]+")
_FLAT = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " ", "\u200b": None, "\ufeff": None})


def _flat(text: str) -> str:
    """Text as compared with what the student sent: curly quotes straightened, spaces collapsed, case folded."""
    return " ".join(str(text).translate(_FLAT).split()).casefold()


def _header_starts(lines: list[str], index: int) -> bool:
    line = lines[index]
    return bool(_ORIGINAL.match(line)) or (
        _HEADER_FIELD.match(line) is not None and line.lstrip(" *").casefold().startswith("from")
        and any(_HEADER_FIELD.match(following) and following.lstrip(" *").casefold().startswith(("sent", "date"))
                for following in [other for other in lines[index + 1:index + 6] if other.strip()][:2])
    )


def _written_below_header(lines: list[str], sent: Iterable[str]) -> str:
    """The lines below an Outlook quote's header that are neither header fields nor the student's own words.

    Words are the student's when they are a whole line of what they sent, or
    four or more words that run on in it. A paragraph is tried whole first,
    since a long line can come back re-wrapped into short pieces; a paragraph
    that is not theirs as a whole is tried line by line. A header block quoted
    further down (the thread's older email) is skipped the same way. What is
    left, the sender wrote.
    """
    bodies = [str(body) for body in sent if str(body or "").strip()]
    whole_lines = {_flat(line) for body in bodies for line in body.replace("\r\n", "\n").split("\n")} - {""}
    running = " ".join(_flat(body) for body in bodies)

    def theirs(words: str) -> bool:  # the student's own
        return not words or words in whole_lines or (
            len(words.split()) >= 4 and re.search(rf"(?<!\w){re.escape(words)}(?!\w)", running) is not None
        )

    paragraphs: list[list[str]] = [[]]
    in_header = False
    for index, line in enumerate(lines):
        if not line.strip():
            paragraphs.append([])
            continue
        if index == 0 or _header_starts(lines, index):
            in_header = True
            paragraphs.append([])
            continue
        if in_header and _HEADER_FIELD.match(line):
            continue
        in_header = False
        paragraphs[-1].append(line.strip())
    written: list[str] = []
    for paragraph in paragraphs:
        if theirs(_flat(" ".join(_QUOTE_MARKS.sub("", line) for line in paragraph))):
            continue
        written.extend(line for line in paragraph if not theirs(_flat(_QUOTE_MARKS.sub("", line))))
    return "\n".join(written)


def html_full_text(markup: str) -> str:
    """HTML as text with each quoted (<blockquote>) line marked "> ", as a plain-text reply marks it."""
    markup = _line_broken(markup)
    markup = re.sub(r"(?i)<blockquote\b[^>]*>", "\n\x00quote-open\x00\n", markup)
    markup = re.sub(r"(?i)</blockquote\s*>", "\n\x00quote-close\x00\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    depth, lines = 0, []
    for line in text.split("\n"):
        if line == "\x00quote-open\x00":
            depth += 1
        elif line == "\x00quote-close\x00":
            depth = max(0, depth - 1)
        else:
            lines.append(f"> {line}" if depth and line.strip() else line)
    return "\n".join(lines)


FULL_TEXT_LIMIT = 20_000


def body_text(message: EmailMessage, *, whole: bool) -> str:
    """The message's plain text, or its HTML as text (``whole``: quoted lines kept and marked, else dropped); '' when unreadable."""
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        text = str(body.get_content())
    except (LookupError, ValueError):
        return ""
    if body.get_content_type() == "text/html":
        return html_full_text(text) if whole else html_text_reply(text)
    return text


def reply_text(message: EmailMessage) -> str:
    return strip_quoted(body_text(message, whole=False))[:20_000]


def full_reply_text(message: EmailMessage) -> str:
    """The whole message as it arrived, quoted lines marked "> ", so an answer typed inline is kept."""
    return body_text(message, whole=True).replace("\r\n", "\n").strip()


# --- Who wrote it, and when ---------------------------------------------------------


def _parse_header(message: EmailMessage, name: str, raw: str) -> Any:
    """One header as the message's policy parses it, or its raw text when the parser fails on it (a stray ":;")."""
    try:
        return message.policy.header_fetch_parse(name, raw)
    except Exception:  # noqa: BLE001 - the header parser raises IndexError and others on odd headers
        return raw


def addresses(message: EmailMessage, *names: str) -> list[str]:
    """Every address in these headers, lowercased. Reads the parsed header, which copes with 'Reyes, Dana <…>'.

    Each header is parsed on its own, so one the parser cannot read (a Cc of
    "a@b.com, :;") falls back to its raw text instead of failing the message.
    """
    found: list[str] = []
    for name in names:
        for header, raw in message.raw_items():
            if str(header).casefold() != name.casefold():
                continue
            value = _parse_header(message, header, raw)
            try:
                parsed = [str(address.addr_spec) for address in getattr(value, "addresses", ())]
            except Exception:  # noqa: BLE001 - as above
                parsed = []
            if not parsed:
                parsed = [address for _name, address in getaddresses([str(raw)])]
            found += [address.strip().casefold() for address in parsed if "@" in address]
    return found


def sender(message: EmailMessage) -> tuple[str, str]:
    """(display name, address) of the one who wrote it. Copes with 'Lee, Greg <greg@…>', where a comma splits the name."""
    value = next((_parse_header(message, name, raw) for name, raw in message.raw_items() if str(name).casefold() == "from"), None)
    try:
        parsed = list(getattr(value, "addresses", ()))
    except Exception:  # noqa: BLE001 - the header parser raises IndexError and others on odd headers
        parsed = []
    for address in parsed:
        if "@" in str(address.addr_spec):
            name = str(address.display_name or "")
            # The name before a comma the header parser split off ("Lee, Greg"): both halves are the name.
            if parsed.index(address) > 0 and not name.count("@"):
                name = " ".join(str(part.display_name or part.addr_spec or "") for part in parsed[:parsed.index(address) + 1]).strip()
            return name, str(address.addr_spec).strip().casefold()
    found = addresses(message, "From")
    if found:
        return parseaddr(str(value or ""))[0], found[0]
    name, address = parseaddr(str(value or ""))
    return name, address.strip().casefold()


def received_or_none(data: dict[str, Any], message: EmailMessage) -> datetime | None:
    """When Gmail received it; its Date header when Gmail gives no time; None when neither says."""
    stamp = int(data.get("internalDate") or 0)
    if stamp > 0:
        return datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)
    try:
        dated = parsedate_to_datetime(str(message.get("Date", "")))
    except (TypeError, ValueError, IndexError):
        return None
    return (dated if dated.tzinfo else dated.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


# --- What kind of mail it is --------------------------------------------------------


def answers_something(message: EmailMessage) -> bool:
    """Whether a message is written as a reply, not a fresh email or a forward."""
    return bool(message.get("In-Reply-To") or message.get("References")) or bool(
        _ANSWER_SUBJECT.match(str(message.get("Subject", "")))
    )


def is_bulk_or_generated(message: EmailMessage) -> bool:
    """Sent to many, or through a mailing or sales tool: list headers, a bulk precedence, or any Auto-Submitted but "no" or "auto-replied"."""
    if message.get("List-Unsubscribe") or message.get("List-Id"):
        return True
    if str(message.get("Precedence", "")).strip().casefold() in {"bulk", "junk", "list"}:
        return True
    auto = str(message.get("Auto-Submitted", "")).strip().casefold()
    return bool(auto) and auto not in {"no", "auto-replied"}


def is_automatic(message: EmailMessage) -> bool:
    """An out-of-office or other automatic answer (RFC 3834), by its headers or its subject."""
    if str(message.get("Auto-Submitted", "")).strip().casefold() == "auto-replied":
        return True
    if message.get("X-Autoreply") or message.get("X-Autorespond"):
        return True
    if str(message.get("Precedence", "")).strip().casefold() == "auto_reply":
        return True
    return bool(_AUTO_SUBJECT.search(str(message.get("Subject", ""))))


# --- What is kept of it, and the links in it ------------------------------------------


# The headers kept with a Gmail reply: who it was to, whether a person or a system sent it, and Gmail's own sender
# check. Headers only, never more of the body than full_text already keeps.
KEPT_HEADERS = (
    "From", "Sender", "To", "Cc", "Subject", "Return-Path", "Auto-Submitted", "X-Auto-Response-Suppress",
    "List-Unsubscribe", "List-Id", "Precedence", "X-Autoreply", "X-Autorespond", "DKIM-Signature", "Authentication-Results",
)
# A message with more of these, or a longer one, is not kept at all, so a check that reads them fails closed.
KEPT_HEADER_LIMIT = 4_000
KEPT_HEADER_COUNT = 40
LINK_HOST_LIMIT = 50


def kept_headers(message: EmailMessage) -> list[list[str]] | None:
    """The KEPT_HEADERS of a message, in the order they arrived and as they arrived ([name, raw value] pairs).

    None when there are too many or one is too long to keep whole: a check
    that reads them then finds none, and fails closed.
    """
    wanted = {name.casefold() for name in KEPT_HEADERS}
    kept: list[list[str]] = []
    for name, value in message.raw_items():
        if str(name).casefold() not in wanted:
            continue
        text = str(value).encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        if len(text) > KEPT_HEADER_LIMIT or len(kept) >= KEPT_HEADER_COUNT:
            return None
        kept.append([str(name), text])
    return kept


def _hosts(text: str) -> Iterable[str]:
    for url in URL.finditer(html.unescape(str(text or ""))):
        host = host_of(url.group(0).rstrip(URL_TAIL))
        if host:
            yield host


def hosts_in(text: str) -> set[str]:
    """The host of every link in a text. Hosts only."""
    return set(_hosts(text))


_BLOCKQUOTE_TAG = re.compile(r"(?is)<(/?)blockquote\b[^>]*>")
# Outlook and OWA quote the original under these markers, with no <blockquote>; everything after is the quote.
_OUTLOOK_QUOTE = re.compile(r"""(?is)<[^<>]*\bid\s*=\s*["']?(?:divRplyFwdMsg|appendonsend)\b""")


def _without_quoted_markup(markup: str) -> str:
    """The markup without its quoted email: balanced <blockquote> elements are cut out and what follows them
    (a tracking pixel the sender's tool adds after the quote) is kept; an element never closed runs to the end.
    Outlook's reply markers cut everything after them."""
    outlook = _OUTLOOK_QUOTE.search(markup)
    if outlook:
        markup = markup[:outlook.start()]
    kept: list[str] = []
    position = 0
    depth = 0
    for tag in _BLOCKQUOTE_TAG.finditer(markup):
        if tag.group(1):
            if depth:
                depth -= 1
                if not depth:
                    position = tag.end()
            continue
        if not depth:
            kept.append(markup[position:tag.start()])
        depth += 1
    if not depth:
        kept.append(markup[position:])
    return "".join(kept)


# Gmail, Yahoo and Proton quote the original in a container (with a <blockquote> inside, or not): the element is the quote.
_QUOTE_CONTAINER = re.compile(
    r"""(?is)<div\b[^<>]*\bclass\s*=\s*["']?[^"'<>]*(?:gmail_quote|yahoo_quoted|protonmail_quote)[^<>]*>"""
)
_DIV_TAG = re.compile(r"(?is)<(/?)div\b[^<>]*>")
_MARKUP_TAG = re.compile(r"<[^>]+>")


def _without_quote_containers(markup: str) -> str:
    """The markup without its quote containers: each balanced <div> element is cut out and what follows it is kept
    (as with a <blockquote>); one never closed runs to the end."""
    while True:
        opening = _QUOTE_CONTAINER.search(markup)
        if opening is None:
            return markup
        depth, end = 1, len(markup)
        for tag in _DIV_TAG.finditer(markup, opening.end()):
            depth += -1 if tag.group(1) else 1
            if not depth:
                end = tag.end()
                break
        markup = markup[:opening.start()] + markup[end:]


def _above_text_quote(markup: str) -> str:
    """The markup above the line where the quoted email starts in its text ("On ... wrote:", a From:/Sent: block, an
    Original Message line, a "> " line), the same boundary strip_quoted draws for the text path; all of it when none."""
    prepared = _line_broken(markup)
    segments: list[tuple[int, int, int]] = []  # (start in the text, length, start in the markup) of each run of text
    pieces: list[str] = []
    length = position = 0
    for tag in [*_MARKUP_TAG.finditer(prepared), None]:
        end = tag.start() if tag else len(prepared)
        text = html.unescape(prepared[position:end])
        if text:
            segments.append((length, len(text), position))
            pieces.append(text)
            length += len(text)
        position = tag.end() if tag else end
    lines = "".join(pieces).split("\n")
    found = _quote_start(lines)
    if found is None:
        return markup
    offset = sum(len(line) + 1 for line in lines[:found[0]])
    for start, size, at in segments:
        if start <= offset < start + size:
            # No tag lies inside a run of text, so cutting at its start keeps every tag above the quote's first line.
            return prepared[:at]
    return markup


_ATTRIBUTE_URL = re.compile(r"""(?is)\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_ATTRIBUTE_SCHEME = re.compile(r"(?i)(?:https?:)?//")


def _attribute_host(value: str) -> str:
    """The host of an href or src value: an http(s) URL or a protocol-relative one (//host/path); '' for any other
    (a relative path, mailto:, cid:). The value is parsed as the URL it is, not searched for one in text."""
    value = re.sub(r"[\t\r\n]", "", clean_url(value.strip()))
    return host_of(value) if _ATTRIBUTE_SCHEME.match(value) else ""


def unquoted_link_hosts(message: EmailMessage) -> set[str]:
    """The host of every link in what the sender wrote, quoted email left out. Hosts only.

    The text of an HTML message has no hrefs, so they are read from the markup
    (href and src, a tracking pixel included, protocol-relative links too; the
    quoted email is cut out: <blockquote> and Gmail-style quote containers,
    what follows Outlook's reply marker, and everything from the line where
    the text path's strip_quoted would cut) as well as from the links written
    out in the text. An
    unreadable HTML part adds nothing. link_hosts_or_none reads the quoted
    parts too and fails closed; this is for judging who sent a message, not for
    vouching that it has no link.
    """
    hosts = set(_hosts(strip_quoted(body_text(message, whole=False))))
    part = message.get_body(preferencelist=("html",))
    if part is not None:
        try:
            markup = str(part.get_content())
        except Exception:  # noqa: BLE001 - a part that cannot be read has no links to give
            return hosts
        markup = _above_text_quote(_without_quote_containers(_without_quoted_markup(markup)))
        for match in _ATTRIBUTE_URL.finditer(markup):
            host = _attribute_host(next(group for group in match.groups() if group is not None))
            if host:
                hosts.add(host)
    return hosts


def all_link_hosts(message: EmailMessage) -> set[str]:
    """The host of every link in a message, quoted email included. Hosts only.

    Links written out in the text and every href and src in the HTML (a
    tracking pixel and protocol-relative links too), with nothing cut out. This
    is for checks where a link anywhere is the sign, such as a sales tool whose
    sequence step quotes the step before, tracked link and all, or appends its
    pixel after the quote. unquoted_link_hosts is for checks where the student's
    own quoted email must not count. An unreadable HTML part adds nothing.
    """
    hosts = set(_hosts(body_text(message, whole=False)))
    part = message.get_body(preferencelist=("html",))
    if part is not None:
        try:
            markup = str(part.get_content())
        except Exception:  # noqa: BLE001 - a part that cannot be read has no links to give
            return hosts
        for match in _ATTRIBUTE_URL.finditer(markup):
            host = _attribute_host(next(group for group in match.groups() if group is not None))
            if host:
                hosts.add(host)
    return hosts


def link_hosts_or_none(message: EmailMessage) -> list[str] | None:
    """The host of every link in a message's text parts, plain and HTML (href too), quoted parts included. Hosts only.

    None when a text part cannot be read (a charset Python does not know) or
    it links more than LINK_HOST_LIMIT hosts: like kept_headers, a check that
    reads them then fails closed rather than missing a link it never saw.
    """
    hosts: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_maintype() != "text":
            continue
        try:
            content = str(part.get_content())
        except Exception:  # noqa: BLE001 - a part that cannot be read hides its links, so none are vouched for
            return None
        for host in _hosts(content):
            if host not in hosts:
                if len(hosts) >= LINK_HOST_LIMIT:
                    return None
                hosts.append(host)
    return hosts
