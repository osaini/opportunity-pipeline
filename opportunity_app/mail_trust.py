"""Who an email is really from, and which mail domains may speak for a company.

Registrable domains. ``registrable_domain`` is the Public Suffix List's
registrable domain: the public suffix plus one label, so careers.acme.com and
acme.com are one organization and acme.co.uk and evil.co.uk are two. It uses
the list bundled with the ``publicsuffixlist`` package and never fetches
anything. Private-section suffixes (github.io, herokuapp.com) count as
suffixes, so two tenants of one host never match. It fails closed: a host
whose suffix is unknown, an IP literal, or a malformed name has no
registrable domain and matches nothing. If the package is not installed,
nothing has one, so no sender can be checked: every email only proposes, and
authenticate's reason says why (NO_DOMAIN_CHECK).

Authentication. ``authenticate`` reads only the topmost
Authentication-Results header, and only when its authserv-id is
mx.google.com: Gmail's inbound servers prepend that header, and a receiver
strips forged copies of its own authserv-id (RFC 8601), so a copy lower down
was written by someone else. It must show dmarc=pass, or, when Gmail found no
DMARC policy to apply (dmarc=none, or no dmarc result), dkim=pass signed by
the From domain's own organization (relaxed alignment). A DMARC result that
failed (or could not be worked out) is never overridden by the DKIM check:
the domain's own policy, strict alignment included, has spoken. Anything
missing, added by another server, ambiguous, or failing is unauthenticated,
and the caller only proposes. Without publicsuffixlist nothing can be
checked, and the reason says so.

Employer domains. A domain is trusted for a company only when the student
says so. Suggestions, which authorize nothing, come from the job URL of an
application (when its host is not a job system or job board and no other
company's posting uses it), from an outreach record for the same company
with a website, and from an authenticated company-domain email.
"""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from pipeline import identity_tokens

from .mail_message import host_of
from .schema import utc_now

SENDERS_PATH = Path(__file__).resolve().parent / "data" / "application_senders.json"
# The categories a message is read for. job_boards and reserved never make a message worth reading.
READ_CATEGORIES = ("ats", "assessment", "scheduling")
# Only these senders can authorize an automatic change: a scheduling tool sends for anyone.
AUTHORIZING_CATEGORIES = ("ats", "assessment")
GMAIL_AUTHSERV_ID = "mx.google.com"
# DMARC results that leave the question to the DKIM check: no policy was found to apply.
DMARC_NO_POLICY = {"none", "bestguesspass"}
NO_DOMAIN_CHECK = "this computer cannot check sender domains (publicsuffixlist is not installed)"
# Domains shared by millions of senders say nothing about who wrote (outreach_inbox.FREEMAIL).
FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com",
    "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com", "yandex.com", "zoho.com",
    # Regional and national free mail.
    "yahoo.co.uk", "yahoo.ca", "yahoo.co.in", "yahoo.com.au", "yahoo.fr", "yahoo.de", "yahoo.co.jp", "ymail.com",
    "rocketmail.com", "hotmail.co.uk", "hotmail.ca", "hotmail.fr", "hotmail.de", "hotmail.it", "hotmail.es",
    "outlook.fr", "outlook.de", "outlook.es", "outlook.it", "outlook.co.uk", "live.ca", "live.co.uk", "live.fr",
    "mac.com", "pm.me", "protonmail.ch", "tutanota.com", "tuta.io", "fastmail.com", "fastmail.fm", "hey.com",
    "gmx.de", "gmx.net", "gmx.at", "web.de", "t-online.de", "freenet.de", "orange.fr", "free.fr", "laposte.net",
    "libero.it", "virgilio.it", "mail.ru", "yandex.ru", "rambler.ru", "qq.com", "163.com", "126.com", "sina.com",
    "naver.com", "daum.net", "hanmail.net", "rediffmail.com", "inbox.com", "zohomail.com", "duck.com",
    # Internet providers' mail.
    "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "bellsouth.net", "cox.net", "charter.net",
    "earthlink.net", "optonline.net", "frontier.com", "windstream.net", "shaw.ca", "rogers.com", "sympatico.ca",
    "btinternet.com", "sky.com", "virginmedia.com", "bigpond.com", "optusnet.com.au",
    # Relays that hide an address.
    "privaterelay.appleid.com", "mozmail.com", "simplelogin.com", "anonaddy.me",
}
DOMAIN_STATUSES = ("suggested", "trusted", "dismissed")

_PSL: Any = None
_PSL_LOCK = threading.Lock()
_PSL_MISSING = False
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def _psl() -> Any:
    """The bundled Public Suffix List, parsed once; None when publicsuffixlist is not installed."""
    global _PSL, _PSL_MISSING
    if _PSL is not None or _PSL_MISSING:
        return _PSL
    with _PSL_LOCK:
        if _PSL is None and not _PSL_MISSING:
            try:
                from publicsuffixlist import PublicSuffixList
            except ImportError:
                _PSL_MISSING = True
                return None
            # accept_unknown=False: a suffix the list does not know is not assumed to be public.
            _PSL = PublicSuffixList(accept_unknown=False)
    return _PSL


def psl_available() -> bool:
    return _psl() is not None


def _clean_host(host: str) -> str:
    text = str(host or "").strip().rstrip(".").lower()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    return text


def registrable_domain(host: str) -> str | None:
    """The host's registrable domain (acme.com for careers.acme.com), or None when it has none. Fails closed."""
    text = _clean_host(host)
    if not text or len(text) > 253:
        return None
    try:
        ipaddress.ip_address(text.strip("[]"))
        return None  # an IP literal belongs to no organization
    except ValueError:
        pass
    try:
        ascii_host = text.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if not _HOST.match(ascii_host):
        return None
    psl = _psl()
    if psl is None:
        return None
    try:
        found = psl.privatesuffix(ascii_host)
    except Exception:  # noqa: BLE001 - a name the list cannot read matches nothing
        return None
    return str(found) if found else None


def same_organization(first: str, second: str) -> bool:
    """Whether two hosts share a registrable domain. False when either has none."""
    a, b = registrable_domain(first), registrable_domain(second)
    return bool(a and b and a == b)


# --- The shipped sender list -----------------------------------------------------------


@lru_cache(maxsize=1)
def sender_lists() -> dict[str, tuple[str, ...]]:
    data = json.loads(SENDERS_PATH.read_text(encoding="utf-8"))
    return {
        key: tuple(sorted({str(item).strip().lower() for item in value if str(item).strip()}))
        for key, value in data.items() if not key.startswith("_") and isinstance(value, list)
    }


def listed(host: str, categories: tuple[str, ...] = READ_CATEGORIES) -> str | None:
    """The category of the shipped list a host is on (itself or as a subdomain), or None."""
    text = _clean_host(host)
    if not text:
        return None
    lists = sender_lists()
    for category in categories:
        for domain in lists.get(category, ()):
            if text == domain or text.endswith(f".{domain}"):
                return category
    return None


def not_an_employer(host: str) -> bool:
    """A job system, job board, scheduling tool, reserved or free-mail domain: never a company's own."""
    return bool(listed(host, tuple(sender_lists()))) or (registrable_domain(host) or _clean_host(host)) in FREEMAIL


# --- Authentication ----------------------------------------------------------------------


@dataclass(frozen=True)
class Authentication:
    ok: bool
    method: str  # 'dmarc' or 'dkim' when ok
    reason: str  # why not, in plain words; '' when ok
    from_address: str
    from_domain: str

    def as_evidence(self) -> dict[str, Any]:
        return {"ok": self.ok, "method": self.method, "reason": self.reason, "from_domain": self.from_domain}


_COMMENT = re.compile(r"\([^()]*\)")


def _uncomment(value: str) -> str:
    text = " ".join(str(value).split())
    for _ in range(5):  # nested comments, innermost first
        stripped = _COMMENT.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    return text


def _results(header: str) -> tuple[str, list[tuple[str, str, dict[str, str]]]]:
    """(authserv-id, [(method, result, {ptype.property: value})]) of one Authentication-Results header."""
    parts = [part.strip() for part in _uncomment(header).split(";")]
    head = parts[0].split() if parts and parts[0] else []
    authserv = head[0].lower() if head else ""
    results = []
    for part in parts[1:]:
        tokens = part.split()
        if not tokens or "=" not in tokens[0]:
            continue
        method, result = tokens[0].split("=", 1)
        props = {}
        for token in tokens[1:]:
            if "=" in token:
                key, value = token.split("=", 1)
                props[key.lower()] = value.strip().strip('"').lower()
        results.append((method.lower().split("/")[0], result.lower(), props))
    return authserv, results


def _unauthenticated(reason: str, address: str = "", domain: str = "") -> Authentication:
    return Authentication(False, "", reason, address, domain)


def authenticate(message: EmailMessage) -> Authentication:
    """Whether Gmail itself vouches for the From domain. See the module docstring for the rule."""
    froms = message.get_all("From") or []
    addresses = [address for _name, address in getaddresses([str(value) for value in froms]) if address]
    if len(froms) != 1 or len(addresses) != 1 or "@" not in addresses[0]:
        return _unauthenticated("the sender is not one clear address")
    address = addresses[0].strip().lower()
    domain = address.rsplit("@", 1)[1].rstrip(".")
    if not psl_available():
        # Every domain comparison below would fail, and "another domain" would be a false reason.
        return _unauthenticated(NO_DOMAIN_CHECK, address, domain)
    headers = message.get_all("Authentication-Results") or []
    if not headers:
        return _unauthenticated("Gmail's sender check is missing", address, domain)
    authserv, results = _results(str(headers[0]))
    if authserv != GMAIL_AUTHSERV_ID:
        return _unauthenticated("the sender check was not added by Gmail", address, domain)
    dmarc = {result for method, result, _props in results if method == "dmarc"}
    if len(dmarc) > 1:
        return _unauthenticated("Gmail's sender check is ambiguous", address, domain)
    if dmarc == {"pass"}:
        header_from = next((props.get("header.from", "") for method, _r, props in results if method == "dmarc"), "")
        if header_from and not same_organization(header_from, domain):
            return _unauthenticated("Gmail's DMARC result is for another domain", address, domain)
        if registrable_domain(domain) is None:
            return _unauthenticated("the sender's domain is not one the public suffix list knows", address, domain)
        return Authentication(True, "dmarc", "", address, domain)
    if dmarc == {"fail"}:
        return _unauthenticated("Gmail's DMARC check failed", address, domain)
    if dmarc and not dmarc <= DMARC_NO_POLICY:
        return _unauthenticated("Gmail could not complete its DMARC check", address, domain)
    signed_elsewhere = False
    for method, result, props in results:
        if method != "dkim" or result != "pass":
            continue
        signer = props.get("header.d") or props.get("header.i", "").rsplit("@", 1)[-1]
        if signer and same_organization(signer, domain):
            return Authentication(True, "dkim", "", address, domain)
        signed_elsewhere = True
    if signed_elsewhere:
        return _unauthenticated("the email is signed by another domain", address, domain)
    return _unauthenticated("the sender did not pass Gmail's check", address, domain)


# --- Employer domains ---------------------------------------------------------------------


def company_key(company: str) -> str:
    """identity_tokens(company), sorted and joined: how employer_domains names a company."""
    return " ".join(sorted(identity_tokens(str(company or ""))))


def _row(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in ("id", "company_key", "company", "domain", "status", "source", "evidence", "created_at", "confirmed_at")}


def suggest(
    conn: sqlite3.Connection, user_id: str, *, company: str, host: str, source: str, evidence: str,
) -> str | None:
    """Record a suggestion that ``host``'s domain is ``company``'s, unless it is known already. Opens no transaction.

    Returns the row's id (new or existing), or None when the host can never be
    an employer's own (a job system, a job board, free mail, or no registrable domain).
    """
    domain = registrable_domain(host)
    key = company_key(company)
    if not domain or not key or not_an_employer(domain):
        return None
    conn.execute(
        """
        INSERT INTO employer_domains(id, user_id, company_key, company, domain, status, source, evidence, created_at)
        VALUES(?, ?, ?, ?, ?, 'suggested', ?, ?, ?) ON CONFLICT(user_id, company_key, domain) DO NOTHING
        """,
        (f"domain-{uuid4().hex}", user_id, key, str(company)[:200], domain, source, evidence[:500], utc_now()),
    )
    row = conn.execute(
        "SELECT id FROM employer_domains WHERE user_id=? AND company_key=? AND domain=?", (user_id, key, domain),
    ).fetchone()
    return str(row["id"]) if row else None


def _url_hosts_elsewhere(conn: sqlite3.Connection, domain: str, key: str) -> bool:
    """Whether another company's posting also lives on ``domain``: then the host says nothing about whose it is."""
    rows = conn.execute(
        "SELECT company, url FROM opportunities WHERE url LIKE ? OR url LIKE ? LIMIT 500",
        (f"%://{domain}%", f"%.{domain}%"),
    ).fetchall()
    return any(registrable_domain(host_of(row["url"])) == domain and company_key(row["company"]) != key for row in rows)


def refresh_suggestions(conn: sqlite3.Connection, user_id: str) -> int:
    """Suggest domains from the student's applications and outreach records. Returns how many are new."""
    before = conn.execute("SELECT COUNT(*) FROM employer_domains WHERE user_id=?", (user_id,)).fetchone()[0]
    applications = conn.execute(
        """
        SELECT DISTINCT o.company, o.url FROM applications a JOIN opportunities o ON o.id=a.opportunity_id
        WHERE a.user_id=?
        """,
        (user_id,),
    ).fetchall()
    keys = {company_key(row["company"]): row["company"] for row in applications}
    keys.pop("", None)
    # Every (company, domain) the student already has a row for, whatever its status (a dismissal included): suggest
    # leaves such a row alone, so neither it nor the posting scan below is run for one. Rows written here join it.
    known = {
        (str(row["company_key"]), str(row["domain"]))
        for row in conn.execute("SELECT company_key, domain FROM employer_domains WHERE user_id=?", (user_id,)).fetchall()
    }
    elsewhere: dict[tuple[str, str], bool] = {}
    with conn:
        for row in applications:
            host = host_of(row["url"])
            domain = registrable_domain(host)
            key = company_key(row["company"])
            if not domain or not key or not_an_employer(domain):
                continue
            if (key, registrable_domain(domain)) in known:
                continue
            if (domain, key) not in elsewhere:
                elsewhere[(domain, key)] = _url_hosts_elsewhere(conn, domain, key)
            if elsewhere[(domain, key)]:
                continue
            if suggest(conn, user_id, company=row["company"], host=domain, source="job_url",
                       evidence=f"The posting you applied to at {row['company']} is on {domain}."):
                known.add((key, registrable_domain(domain)))
        for row in conn.execute(
            "SELECT company, website FROM outreach_targets WHERE user_id=? AND website IS NOT NULL AND website<>''", (user_id,),
        ).fetchall():
            key = company_key(row["company"])
            if key not in keys:
                continue
            host = host_of(row["website"] if "//" in str(row["website"]) else f"https://{row['website']}")
            if (key, registrable_domain(host)) in known:
                continue
            if suggest(conn, user_id, company=keys[key], host=host, source="outreach",
                       evidence=f"Your outreach record for {row['company']} lists the website {registrable_domain(host) or host}."):
                known.add((key, registrable_domain(host)))
    after = conn.execute("SELECT COUNT(*) FROM employer_domains WHERE user_id=?", (user_id,)).fetchone()[0]
    return int(after) - int(before)


def list_domains(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    """Suggested and trusted domains; the student's dismissals stay out of sight."""
    rows = conn.execute(
        """
        SELECT * FROM employer_domains WHERE user_id=? AND status IN ('suggested', 'trusted')
        ORDER BY CASE status WHEN 'suggested' THEN 0 ELSE 1 END, company, domain
        """,
        (user_id,),
    ).fetchall()
    return [_row(row) for row in rows]


def decide(conn: sqlite3.Connection, user_id: str, domain_id: str, status: str) -> dict[str, Any]:
    """Trust, stop trusting (back to suggested), or dismiss one domain. Raises LookupError when it is not the student's.

    Stopping trusting keeps the domain's mail read, only ever proposing. A
    dismissed domain's mail is no longer read at all (unless a job system sends it).
    """
    if status not in DOMAIN_STATUSES:
        raise ValueError("A domain is suggested, trusted or dismissed")
    timestamp = utc_now()
    with conn:
        changed = conn.execute(
            "UPDATE employer_domains SET status=?, confirmed_at=? WHERE id=? AND user_id=?",
            (status, timestamp if status == "trusted" else None, domain_id, user_id),
        ).rowcount
        if not changed:
            raise LookupError(domain_id)
    row = conn.execute("SELECT * FROM employer_domains WHERE id=? AND user_id=?", (domain_id, user_id)).fetchone()
    return _row(row)


def trusted_for(conn: sqlite3.Connection, user_id: str, key: str) -> set[str]:
    """The domains the student trusts for one company."""
    if not key:
        return set()
    return {str(row["domain"]) for row in conn.execute(
        "SELECT domain FROM employer_domains WHERE user_id=? AND company_key=? AND status='trusted'", (user_id, key),
    ).fetchall()}


def known_domains(conn: sqlite3.Connection, user_id: str) -> dict[str, set[str]]:
    """Every suggested or trusted domain, with the companies it may be for: what the reader looks out for."""
    found: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT domain, company_key FROM employer_domains WHERE user_id=? AND status IN ('suggested', 'trusted')", (user_id,),
    ).fetchall():
        found.setdefault(str(row["domain"]), set()).add(str(row["company_key"]))
    return found
