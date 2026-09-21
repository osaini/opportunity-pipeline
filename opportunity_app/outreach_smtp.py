"""Ask a company's mail server whether a guessed address exists, without sending mail.

The conversation stops at RCPT TO: the server is asked whether it would accept
mail for an address, and then the session quits. No DATA command is ever sent,
so no message is delivered to anyone.

The answer is weak evidence, and the code treats it that way:

- Many servers accept every address (catch-all). A made-up address is asked
  first; when the server accepts it, every answer from that server is recorded
  as "catch_all", which proves nothing.
- A refusal only counts when it says the mailbox does not exist (an enhanced
  status of 5.1.x, or words like "user unknown"). Servers that refuse a
  residential connection answer 550 5.7.x to everything. That is policy, not an
  answer about the address, so it is recorded as "smtp_unknown".
- Anything else (greylisting, timeouts, a refused connection) is "smtp_unknown".

An accepted address stays unverified. It is only a reason to prefer one guess
over another.

The check contacts only the domain's own MX hosts, and only when they resolve to
public addresses, with one short session per domain.
"""

from __future__ import annotations

import ipaddress
import os
import re
import smtplib
import socket
from typing import Any, Callable, Protocol
from uuid import uuid4

import httpx

ACCEPTED = "smtp_accepted"
REJECTED = "smtp_rejected"
CATCH_ALL = "catch_all"
UNKNOWN = "smtp_unknown"

ENABLE_ENV = "PIPELINE_OUTREACH_SMTP_VERIFY"
HELO_ENV = "PIPELINE_OUTREACH_SMTP_HELO"
# One session asks about at most this many addresses; more looks like probing.
MAX_ADDRESSES_PER_DOMAIN = 6
MAX_MX_HOSTS = 2
TIMEOUT = 10.0
# An enhanced status code (RFC 3463) settles it when present: 5.1.1 bad
# destination mailbox, 5.1.0 other address status, 5.1.6 mailbox moved, 5.1.10
# null MX. Anything else (5.7.x policy, 5.1.8 bad sender) says nothing about the
# address. Without a code, only wording about the mailbox itself counts.
ENHANCED_STATUS = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")
MISSING_MAILBOX_DETAILS = {0, 1, 6, 10}
NO_SUCH_MAILBOX = re.compile(
    r"user unknown|unknown user|no such user|does not exist|doesn't exist|mailbox (?:not found|unavailable)|"
    r"invalid recipient|recipient not found|unknown recipient|no mailbox",
    re.IGNORECASE,
)


class SmtpSession(Protocol):
    def ehlo(self, name: str = ...) -> tuple[int, bytes]: ...
    def helo(self, name: str = ...) -> tuple[int, bytes]: ...
    def mail(self, sender: str, options: Any = ...) -> tuple[int, bytes]: ...
    def rcpt(self, recip: str, options: Any = ...) -> tuple[int, bytes]: ...
    def quit(self) -> Any: ...
    def close(self) -> None: ...


Connect = Callable[[str, float], SmtpSession]
Resolver = Callable[[str], list[str]]


def _connect(ip: str, timeout: float) -> SmtpSession:
    return smtplib.SMTP(ip, 25, timeout=timeout)


def _resolve(host: str) -> list[str]:
    return sorted({item[4][0] for item in socket.getaddrinfo(host, 25, proto=socket.IPPROTO_TCP)})


def mx_hosts(client: httpx.Client, domain: str) -> list[str] | None:
    """The domain's MX hosts, best preference first, over DNS-over-HTTPS. None when the lookup failed."""
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
        return []
    records = []
    for answer in payload.get("Answer") or []:
        if answer.get("type") != 15:
            continue
        parts = str(answer.get("data") or "").split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].strip(".") and parts[1] != ".":
            records.append((int(parts[0]), parts[1].rstrip(".").lower()))
    return [host for _preference, host in sorted(records)]


def _reply_text(message: bytes | str) -> str:
    return message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message)


def classify(code: int, message: bytes | str) -> str:
    """One RCPT reply as accepted, rejected (the mailbox does not exist), or unknown."""
    if code in (250, 251):
        return ACCEPTED
    if not 500 <= code < 600:
        return UNKNOWN
    text = _reply_text(message)
    enhanced = ENHANCED_STATUS.search(text)
    if enhanced:
        missing = enhanced.group(1) == "5" and enhanced.group(2) == "1" and int(enhanced.group(3)) in MISSING_MAILBOX_DETAILS
        return REJECTED if missing else UNKNOWN
    if re.search(r"\bsender\b", text, re.IGNORECASE):
        return UNKNOWN
    return REJECTED if NO_SUCH_MAILBOX.search(text) else UNKNOWN


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


class SmtpVerifier:
    """Checks guessed addresses against the domain's mail server, one session per domain."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        connect: Connect = _connect,
        resolve: Resolver = _resolve,
        helo_name: str | None = None,
        timeout: float = TIMEOUT,
        max_addresses: int = MAX_ADDRESSES_PER_DOMAIN,
    ) -> None:
        self.client = client
        self.connect = connect
        self.resolve = resolve
        self.helo_name = helo_name or os.environ.get(HELO_ENV, "").strip() or "localhost"
        self.timeout = timeout
        self.max_addresses = max_addresses

    def __enter__(self) -> "SmtpVerifier":
        self.client.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self.client.__exit__(*args)

    def check(self, domain: str, addresses: list[str]) -> dict[str, str]:
        """A result for each address on this domain, in the order given. Others are skipped."""
        domain = domain.lower().strip(".")
        wanted = []
        for address in addresses:
            address = address.strip().lower()
            if address.rsplit("@", 1)[-1] == domain and address not in wanted:
                wanted.append(address)
        wanted = wanted[:self.max_addresses]
        if not wanted:
            return {}
        unknown = {address: UNKNOWN for address in wanted}
        hosts = mx_hosts(self.client, domain)
        if not hosts:
            return unknown
        for host in hosts[:MAX_MX_HOSTS]:
            answered = self._ask(host, domain, wanted)
            if answered is not None:
                return answered
        return unknown

    def _public_ip(self, host: str) -> str | None:
        try:
            addresses = self.resolve(host)
        except OSError:
            return None
        if not addresses:
            return None
        # One private answer is enough to refuse the host: an MX record must
        # never make this machine talk to its own network.
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            return None
        return addresses[0]

    def _ask(self, host: str, domain: str, wanted: list[str]) -> dict[str, str] | None:
        """Results from one MX host, or None when it gave no session to ask in."""
        ip = self._public_ip(host)
        if ip is None:
            return None
        try:
            session = self.connect(ip, self.timeout)
        except (OSError, smtplib.SMTPException):
            return None
        try:
            code, _ = session.ehlo(self.helo_name)
            if code != 250:
                code, _ = session.helo(self.helo_name)
                if code != 250:
                    return None
            code, _ = session.mail("")
            if code != 250:
                return None
            probe = f"no-such-mailbox-{uuid4().hex[:12]}@{domain}"
            probed = classify(*session.rcpt(probe))
            if probed == ACCEPTED:
                return {address: CATCH_ALL for address in wanted}
            if probed != REJECTED:
                # The server would not say a made-up mailbox is missing, so its
                # answers about real ones cannot be told apart from policy.
                return {address: UNKNOWN for address in wanted}
            return {address: classify(*session.rcpt(address)) for address in wanted}
        except (OSError, smtplib.SMTPException):
            return None
        finally:
            try:
                session.quit()
            except (OSError, smtplib.SMTPException):
                session.close()


def default_verifier() -> SmtpVerifier | None:
    """A verifier unless PIPELINE_OUTREACH_SMTP_VERIFY turns it off."""
    if not enabled():
        return None
    return SmtpVerifier(httpx.Client(timeout=10.0, follow_redirects=False, verify=True))
