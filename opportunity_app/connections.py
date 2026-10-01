"""Sandbox-first provider lifecycle, monitored event previews, and notifications."""

from __future__ import annotations

import hashlib
import hmac
import json
import base64
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from cryptography.fernet import Fernet

from .actions import ApplicationNotFoundError, add_application_task, update_application
from .inbox_classifiers import classify_email
from .outreach_config import sender_account
from .timestamps import utc_now
from .typesafe_decisions import DecisionClient


class ConnectionNotFoundError(LookupError):
    pass


OAUTH_PROVIDERS = {
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.events.readonly"],
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
    },
    # Approved outreach, as drafts or sent after a confirm click, with the
    # resume attached. gmail.compose drafts and sends. gmail.readonly lets the
    # app find and read the delivery failure notice for a send that bounced
    # (outreach_delivery.py). gmail.modify is used only to add the student's
    # label to outreach threads, sent mail and replies (outreach_labels.py: messages.batchModify
    # with addLabelIds, and labels.list and labels.create); nothing in the app
    # removes a label, trashes, archives or marks mail read. Not
    # gmail.metadata: with that granted, Gmail refuses to return a message's
    # text even alongside gmail.readonly.
    "gmail_drafts": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
        ],
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
    },
    "microsoft": {
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": ["offline_access", "Mail.Read", "Calendars.Read"],
        "client_id_env": "MICROSOFT_OAUTH_CLIENT_ID",
        "client_secret_env": "MICROSOFT_OAUTH_CLIENT_SECRET",
    },
}


GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


def begin_oauth(conn: sqlite3.Connection, provider: str, redirect_uri: str, *, user_id: str, login_hint: str = "") -> dict[str, Any]:
    config = OAUTH_PROVIDERS.get(provider)
    if not config:
        raise ValueError("Unsupported OAuth provider")
    client_id = os.environ.get(config["client_id_env"], "")
    if not client_id:
        raise ValueError(f"{config['client_id_env']} is not configured")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    created = datetime.now(timezone.utc)
    with conn:
        conn.execute("INSERT INTO oauth_states(state_hash, user_id, provider, code_verifier, redirect_uri, expires_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                     (hashlib.sha256(state.encode()).hexdigest(), user_id, provider, verifier, redirect_uri, (created + timedelta(minutes=10)).isoformat(), created.isoformat()))
    from urllib.parse import urlencode
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(config["scopes"]),
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"}
    if login_hint:
        params["login_hint"] = login_hint
    query = urlencode(params)
    return {"provider": provider, "authorization_url": f"{config['authorize']}?{query}", "expires_at": (created + timedelta(minutes=10)).isoformat()}


def _google_reasons(response: httpx.Response) -> set[str]:
    """The reasons Google gave an error (status, errors[].reason, details[].reason); empty for anything unreadable."""
    reasons: set[str] = set()
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return reasons
    if not isinstance(error, dict):
        return reasons
    if isinstance(error.get("status"), str):
        reasons.add(error["status"])
    for key in ("errors", "details"):
        for item in error.get(key) or []:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reasons.add(item["reason"])
    return reasons


async def _gmail_account(client: httpx.AsyncClient, access_token: str) -> str:
    """The address a new Gmail connection signed into, or a ValueError that refuses the connection.

    Fails closed: a connection whose account cannot be confirmed is not saved.
    PIPELINE_OUTREACH_ACCOUNT is read through outreach_config.
    """
    try:
        profile = await client.get(GMAIL_PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as exc:
        raise ValueError("Could not confirm which Gmail account connected; try connecting again") from exc
    if profile.status_code == 403:
        # A 403 is not always a missing permission: the Gmail API may be off in the project, or Google may be rate limiting.
        reasons = _google_reasons(profile)
        if reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
            raise ValueError("Enable the Gmail API in your Google Cloud project (README, Gmail drafts setup), then connect again")
        if reasons & {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RESOURCE_EXHAUSTED"}:
            raise ValueError("Could not confirm which Gmail account connected; try connecting again")
        raise ValueError("Google did not grant the Gmail permissions; connect again and tick every box on Google's screen")
    try:
        address = str(profile.json().get("emailAddress") or "").strip() if profile.status_code == 200 else ""
    except (ValueError, AttributeError):
        address = ""
    if not address:
        raise ValueError("Could not confirm which Gmail account connected; try connecting again")
    expected = sender_account()
    if expected and expected.casefold() != address.casefold():
        raise ValueError(f"Google signed in as {address}, but this pipeline's mailbox is {expected}. Connect again and choose {expected}.")
    return address


async def complete_oauth(conn: sqlite3.Connection, provider: str, state: str, code: str, encryption_key: str, *, user_id: str) -> dict[str, Any]:
    config = OAUTH_PROVIDERS.get(provider)
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    row = conn.execute("SELECT * FROM oauth_states WHERE state_hash=? AND user_id=? AND provider=?", (state_hash, user_id, provider)).fetchone()
    if not config or not row or row["consumed_at"] or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        raise ValueError("OAuth state is invalid or expired")
    client_id = os.environ.get(config["client_id_env"], "")
    client_secret = os.environ.get(config["client_secret_env"], "")
    if not client_id or not client_secret or not encryption_key:
        raise ValueError("OAuth client credentials and PIPELINE_CONNECTION_KEY are required")
    try:
        fernet = Fernet(encryption_key.encode())
    except Exception as exc:
        raise ValueError("PIPELINE_CONNECTION_KEY must be a valid Fernet key") from exc
    account_email = ""
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.post(config["token"], data={"grant_type": "authorization_code", "client_id": client_id,
            "client_secret": client_secret, "redirect_uri": row["redirect_uri"], "code": code, "code_verifier": row["code_verifier"]})
        if response.status_code == 200 and provider == "gmail_drafts":
            # Which mailbox this connection reads, before anything is written: a connection made
            # with the wrong Google account is refused here, not found out by a reply that never comes.
            try:
                access_token = str(response.json().get("access_token") or "")
            except ValueError:
                access_token = ""
            if access_token:
                account_email = await _gmail_account(client, access_token)
    if response.status_code != 200:
        raise ValueError("Provider rejected the OAuth exchange")
    tokens = response.json()
    if not tokens.get("access_token"):
        raise ValueError("Provider response did not contain an access token")
    # Google names the permissions it actually granted, and the student may leave a box unticked.
    granted = str(tokens.get("scope") or "").split() if provider == "gmail_drafts" else []
    scopes = sorted(set(granted)) if granted else config["scopes"]
    connector_id = f"connector-{provider}-{user_id}"
    timestamp = utc_now()
    existing_connector = conn.execute(
        "SELECT encrypted_refresh_token, token_granted_at FROM connector_accounts WHERE user_id=? AND provider=?",
        (user_id, provider),
    ).fetchone()
    refresh_token = tokens.get("refresh_token")
    # token_granted_at is when the refresh token in use was granted, from which
    # a Testing-mode Gmail grant's likely expiry is estimated. A reconnect that
    # kept the old refresh token keeps its grant time.
    if refresh_token:
        encrypted_refresh_token = fernet.encrypt(str(refresh_token).encode()).decode()
        token_granted_at = timestamp
    elif existing_connector:
        encrypted_refresh_token = str(existing_connector["encrypted_refresh_token"] or "")
        token_granted_at = existing_connector["token_granted_at"]
    else:
        encrypted_refresh_token = fernet.encrypt(b"").decode()
        token_granted_at = None
    with conn:
        # A fresh connection clears the error that asked for it.
        conn.execute("""INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token, encrypted_refresh_token, status, created_at, updated_at, token_granted_at, last_error, account_email)
            VALUES(?, ?, ?, ?, ?, ?, 'connected', ?, ?, ?, '', ?) ON CONFLICT(user_id, provider) DO UPDATE SET scopes_json=excluded.scopes_json,
            encrypted_access_token=excluded.encrypted_access_token, encrypted_refresh_token=excluded.encrypted_refresh_token,
            status='connected', updated_at=excluded.updated_at, disconnected_at=NULL,
            token_granted_at=excluded.token_granted_at, last_error='', account_email=excluded.account_email""",
            (connector_id, user_id, provider, json.dumps(scopes), fernet.encrypt(tokens["access_token"].encode()).decode(),
             encrypted_refresh_token, timestamp, timestamp, token_granted_at, account_email))
        conn.execute("UPDATE oauth_states SET consumed_at=? WHERE state_hash=?", (timestamp, state_hash))
    return connector_record(conn, connector_id, user_id=user_id)


def ensure_preferences(conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO notification_preferences(user_id, updated_at)
            VALUES(?, ?)
            """,
            (user_id, timestamp),
        )
    row = conn.execute("SELECT * FROM notification_preferences WHERE user_id=?", (user_id,)).fetchone()
    return {key: bool(row[key]) if key.endswith("_enabled") or key == "phone_verified" else row[key] for key in row.keys()}


def update_preferences(conn: sqlite3.Connection, updates: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    allowed = {"timezone", "quiet_start", "quiet_end", "digest_frequency", "in_app_enabled", "email_enabled", "push_enabled", "sms_enabled", "voice_enabled"}
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError(f"Unsupported notification preferences: {', '.join(sorted(unknown))}")
    current = ensure_preferences(conn, user_id=user_id)
    merged = {**current, **updates}
    try:
        ZoneInfo(str(merged["timezone"]))
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Unknown IANA timezone") from exc
    if merged["digest_frequency"] not in {"immediate", "daily", "weekly", "off"}:
        raise ValueError("Unsupported digest frequency")
    for field in ("quiet_start", "quiet_end"):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(merged[field])):
            raise ValueError(f"{field} must use 24-hour HH:MM")
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            UPDATE notification_preferences SET
                timezone=?, quiet_start=?, quiet_end=?, digest_frequency=?,
                in_app_enabled=?, email_enabled=?, push_enabled=?, sms_enabled=?,
                voice_enabled=?, timezone_explicit=?, updated_at=? WHERE user_id=?
            """,
            (merged["timezone"], merged["quiet_start"], merged["quiet_end"], merged["digest_frequency"], int(bool(merged["in_app_enabled"])), int(bool(merged["email_enabled"])), int(bool(merged["push_enabled"])), int(bool(merged["sms_enabled"])), int(bool(merged["voice_enabled"])), int("timezone" in updates or bool(current.get("timezone_explicit"))), timestamp, user_id),
        )
    return ensure_preferences(conn, user_id=user_id)


def connect_provider(conn: sqlite3.Connection, provider: str, *, user_id: str) -> dict[str, Any]:
    if provider != "sandbox":
        raise ValueError("Live Google/Microsoft OAuth is disabled until provider credentials are configured")
    connector_id = f"connector-{provider}-{user_id}"
    timestamp = utc_now()
    scopes = ["mail.metadata", "calendar.events.readonly"]
    with conn:
        conn.execute(
            """
            INSERT INTO connector_accounts(
                id, user_id, provider, scopes_json, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'connected', ?, ?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                scopes_json=excluded.scopes_json, status='connected',
                encrypted_access_token='', encrypted_refresh_token='',
                updated_at=excluded.updated_at, disconnected_at=NULL
            """,
            (connector_id, user_id, provider, json.dumps(scopes), timestamp, timestamp),
        )
    return connector_record(conn, connector_id, user_id=user_id)


def connector_record(conn: sqlite3.Connection, connector_id: str, *, user_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM connector_accounts WHERE id=? AND user_id=?", (connector_id, user_id)).fetchone()
    if not row:
        raise ConnectionNotFoundError(connector_id)
    result = dict(row)
    result["scopes"] = json.loads(row["scopes_json"] or "[]")
    for key in ("scopes_json", "encrypted_access_token", "encrypted_refresh_token"):
        result.pop(key, None)
    return result


def list_connectors(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    ids = [row[0] for row in conn.execute("SELECT id FROM connector_accounts WHERE user_id=? ORDER BY created_at", (user_id,)).fetchall()]
    return [connector_record(conn, str(value), user_id=user_id) for value in ids]


def disconnect_provider(conn: sqlite3.Connection, connector_id: str, *, user_id: str) -> dict[str, Any]:
    timestamp = utc_now()
    with conn:
        cursor = conn.execute(
            """
            UPDATE connector_accounts SET status='disconnected', encrypted_access_token='',
                encrypted_refresh_token='', token_granted_at=NULL, updated_at=?, disconnected_at=?
            WHERE id=? AND user_id=?
            """,
            (timestamp, timestamp, connector_id, user_id),
        )
        if not cursor.rowcount:
            raise ConnectionNotFoundError(connector_id)
    return connector_record(conn, connector_id, user_id=user_id)


# The keyword rules for an application email, first match wins, in the order
# inbox_classifiers.EMAIL_QUESTION asks Jev to use. Assessment and scheduling
# add tasks, never a stage. application_inbox adds what the sender says (an
# assessment platform, a scheduling link) on top of these.
MONITORED_PATTERNS = (
    # "Pleased to offer" counts only for a job, never for an interview (_is_job_offer): "happy to offer
    # you an interview slot" is an invitation, "pleased to offer you the opportunity to join" an offer.
    ("offer", (
        r"\b(offer of employment|offer letter|(extend|extending) (you )?an offer"
        r"|(pleased|happy|delighted|excited|thrilled|glad) to (offer|extend) (you|the|a|an|this|our))\b"
    ), 0.95),
    # A definite statement only: "other candidates" or "not selected" said of the process in general
    # ("along with other candidates", "if you are not selected") is not a rejection (_definite).
    ("rejected", (
        r"\b(not moving forward|regret to inform|(the |this )?(position|role|opening) has (now |already )?been filled"
        r"|(mov(e|ed|ing)|proceed(ed|ing)?|go(ing)?|went|gone|continu(e|ed|ing)) ((forward|ahead|on) )?with"
        r" (an?other|other|different|a different|(some|several|the) other|more qualified|stronger|more experienced)"
        r" (candidates?|applicants?)"
        r"|(selected|chosen|chose|hired|identified|decided on|pursued?|pursuing|offered the (position|role) to)"
        r" (an?other|other|a different) (candidates?|applicants?)"
        r"|other (candidates|applicants) (whose ([\w-]+ ){1,4})?(more closely|better) (match|matches|matched|align|aligns|aligned|fit|fits|meet|meets|met)"
        r"|other (candidates|applicants) (who |that )?(were|was|are|is) (a )?(better|stronger|closer) (fit|match)"
        r"|no longer (under consideration|being considered)|not (been )?selected (for|to)"
        r"|(will not|won't|decided not to|unable to|not be able to) (be )?(mov(e|ing) (you |your application |your candidacy )?forward"
        r"|proceed(ing)? with your|continu(e|ing) with your|advanc(e|ing) your))\b"
    ), 0.92),
    ("interview", (
        r"\b(schedule|invite|invitation).{0,30}\binterview\b|\binterview availability\b"
        r"|\byour interview (is |has been )?(confirmed|scheduled)\b"
        r"|\b(like|love|want) to (invite you to|schedule|set up|arrange) (an? |some time for an? )?"
        r"(phone |video |technical |virtual |first[- ]round |final[- ]round |onsite |on-site )?(interview|phone screen|screening call)\b"
        r"|\b(offer|offering) you (an? |the )?([\w-]+ ){0,2}(interview|phone screen|screening call)\b"
    ), 0.9),
    ("assessment", (
        r"\b(online assessment|coding (challenge|assessment|test|exercise)|technical (assessment|challenge)"
        r"|take[- ]home (assignment|exercise|challenge|project)|assessment (link|invitation|invite)"
        r"|(complete|take|finish) (the |your |an |this |our )?([a-z]+ ){0,2}(assessment|coding test|challenge))\b"
    ), 0.88),
    ("scheduling", (
        r"\b((pick|choose|select|book) a (time|slot)|schedule a (time|call|chat|meeting)|share your availability"
        r"|let us know your availability|calendly\.com|goodtime\.io|modernloop\.io)\b"
    ), 0.85),
    ("application_confirmation", (
        r"\b(application (?:was |has been )?received|thank you for applying|thanks for applying|submission confirmation"
        r"|(we have |we've )?received your application)\b"
    ), 0.9),
    ("deadline", r"\b(deadline|complete by|due by)\b", 0.65),
    ("recruiter_reply", r"\b(recruiter|talent acquisition|hiring team)\b", 0.55),
)
# A rejection of one role that asks about another is not a plain rejection:
# it keeps its label, but no longer sure enough to act on alone.
_HEDGED_REJECTION = re.compile(
    r"\b(consider(ing)? you for|would you be (open|interested)|like to (move|put) you forward for"
    r"|great fit for (another|a different)|another (role|position|opening) (that|which|we))\b"
)
HEDGED_REJECTION_CONFIDENCE = 0.7

# A phrase said of what may happen is not news that it did. "If you are not
# selected", "until the position has been filled" and "we may not be able to
# move forward with all applicants" are what confirmations say; so are "if your
# background is a match, a recruiter will reach out to schedule a call" and "we
# invite the strongest candidates to interview". _definite reads the grammar
# around a match, not just nearby words, since the same words open real news:
# "we know this may be disappointing, but we will not be moving forward",
# "once again, we regret to inform you", "following your application in May,
# we would like to invite you to interview", "if you're available, we would
# like to schedule an interview" and "if you are still interested, please let
# us know your availability" are all definite.
_SENTENCE_MARKS = ".!?;\n"
# Where a clause ends inside a sentence.
_CLAUSE_BREAK = re.compile(r"[,:()]|\s[-\u2013\u2014]+\s|\bbut\b")
_DAYS_AND_MONTHS = (r"january|february|march|april|may|june|july|august|september|october|november|december"
                    r"|jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec|(mon|tues|wednes|thurs|fri|satur|sun)day")
# A word that makes what follows it conditional, up to its main clause: "if", "unless", "until the
# position has been filled", "once we have reviewed", "should you be selected", "in the event that".
# Not "once again", "at once", "you have until Friday" or "until October 5".
_CONJUNCTION = re.compile(
    r"\b(if|unless|whether|in case|in the event"
    r"|(?<!at )(?<!than )once(?! (again|more)\b)"
    r"|(?<!have )(?<!has )until(?! (then|now|today|tomorrow|tonight|midnight|noon|next|this|last|the end|end|"
    + _DAYS_AND_MONTHS + r")\b)(?! \d)"
    r"|should(?= (you|we|they|your|our|the|there|it|this|that|a|an|any|anyone)\b))\b"
)
# "May" or "might" as a modal: not the month ("in May", "mid-May", "May 5"), nor a request ("may we
# schedule"). _modal also leaves out permission to do the task ("you may now book a time").
_MODAL = re.compile(
    r"(?<!\bin )(?<!\bon )(?<!\bof )(?<!\bby )(?<!\bsince )(?<!\bearly )(?<!\bmid )(?<!\blate )(?<!\bthis )"
    r"(?<!\bnext )(?<!\blast )(?<!\bfrom )(?<!\buntil )(?<!-)"
    r"\b(might|may(?! (i|we)\b)(?!,? \d)(?! (and|or|through|to)\b))\b"
)
_PERMISSION = re.compile(
    r"may (now |also |then |still )?(schedule|book|pick|choose|select|complete|take|start|begin|access|use|reply|respond"
    r"|proceed|log in|sign in|click)\b"
)
_FUTURE = re.compile(r"\b(will|we'll|they'll|you'll|it'll|shall|going to)\b")
# Tense or mood that leaves the main clause after a condition contingent: "if selected, we will"
# (and a modal, _modal).
_CONTINGENT = re.compile(r"\b(will|we'll|they'll|you'll|it'll|shall|going to|could|would(?! (like|love)\b))\b")
# What opens a main clause that is a real request after a condition that is not about being chosen:
# "if you're available, we would like to", "if it works for you, please schedule a time". After "if
# you are selected" (_SELECTION) a request is as contingent as the selection.
_REQUEST = re.compile(
    r"\b(please|kindly|feel free"
    r"|(we|i)('d| would)( really)? (like|love)|(we|i)('d| would) be (happy|glad|delighted|pleased)"
    r"|(we|i)('re| are| am) (happy|glad|delighted|pleased|excited) to|(we|i) (invite|want) you)\b"
)
# A task a main clause asks for, by its first word: "if still interested, let us know your availability".
_IMPERATIVE = re.compile(r"(pick|choose|select|book|schedule|share|let us know|complete|take|finish|click|use)\b")
_SELECTION = re.compile(
    r"\b(selected|shortlisted|chosen|successful|qualif\w*|an? (good |strong |great |close )?(fit|match)|advance|advances"
    r"|progress|progresses|move forward|moving forward|pass|passes|meets? (our|the|all)|considered|consider you|we decide)\b"
)
# The student, addressed as the one invited: future tense then is a plan, not a maybe ("we will invite you").
_TO_YOU = re.compile(
    r"\b(invite you|invited you|you('re| are| will be| have been) invited|send you an? ([\w-]+ )?invitation"
    r"|you('ll| will) (receive|get) an? ([\w-]+ )?(invitation|invite)|your ([\w-]+ ){0,2}interview|interview with you)\b"
)
_OTHERS = re.compile(r"\b(selected|strongest|shortlisted|qualified|successful|top|chosen|a few|some) (candidates|applicants)\b")
# The labels a hedge can undo (an offer is always a proposal, so it is never hedged away).
_HEDGED = {"rejected", "interview", "assessment", "scheduling"}
_FOR_OTHERS = {"interview", "assessment", "scheduling"}


def _bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Where the sentence holding text[start:end] begins and ends."""
    left = max(text.rfind(mark, 0, start) for mark in _SENTENCE_MARKS) + 1
    rights = [index for index in (text.find(mark, end) for mark in _SENTENCE_MARKS) if index != -1]
    return left, (min(rights) if rights else len(text))


def _clause_start(text: str, low: int, high: int) -> int:
    start = low
    for found in _CLAUSE_BREAK.finditer(text, low, high):
        start = found.end()
    return start


def _clause_end(text: str, low: int, high: int) -> int:
    found = _CLAUSE_BREAK.search(text, low, high)
    return found.start() if found else high


def _modal(text: str, low: int, high: int) -> bool:
    """Whether text[low:high] has a modal "may" or "might" that is not permission given to the student."""
    return any(
        not (text[max(0, modal.start() - 4):modal.start()] == "you " and _PERMISSION.match(text, modal.start()))
        for modal in _MODAL.finditer(text, low, high)
    )


def _conditional(text: str, sentence_start: int, clause_start: int, found: re.Match[str]) -> bool:
    """Whether a condition earlier in the sentence governs the match."""
    conditions = list(_CONJUNCTION.finditer(text, sentence_start, found.start()))
    if not conditions:
        return False
    condition = conditions[-1]
    main = _clause_end(text, condition.end(), found.start())
    if _SELECTION.search(text, condition.start(), main):
        return True  # whatever follows waits on being chosen: "if selected, please book a time"
    if _REQUEST.search(text, condition.end(), found.start()):
        return False  # a request: "if you're interested, please book a time"
    if main < found.start() and not text[clause_start:found.start()].strip() and _IMPERATIVE.match(text, found.start()):
        return False  # a request by its verb: "if still interested, let us know your availability"
    if condition.start() >= clause_start:
        return True  # the match is inside the condition: "if you are not selected for this role"
    # The match is in the main clause after the condition: contingent only when that clause is.
    return bool(_CONTINGENT.search(text, main, found.end())) or _modal(text, main, found.end())


def _definite(event_type: str, text: str, found: re.Match[str]) -> bool:
    """Whether one match states what happened, rather than what might."""
    if event_type == "offer":
        return _is_job_offer(text, found)
    if event_type not in _HEDGED:
        return True
    sentence_start, sentence_end = _bounds(text, found.start(), found.end())
    clause_start = _clause_start(text, sentence_start, found.start())
    clause_end = _clause_end(text, found.end(), sentence_end)
    # A modal in the match's own clause: "we may not be able to move forward with all applicants".
    if _modal(text, clause_start, found.start()):
        return False
    if _conditional(text, sentence_start, clause_start, found):
        return False
    if event_type not in _FOR_OTHERS:
        return True
    # An invitation or a task meant for others: "we invite the strongest candidates to interview".
    if _OTHERS.search(text, clause_start, clause_end):
        return False
    # Future tense is a promise to get in touch ("we will reach out to schedule an interview"),
    # unless it invites the student outright ("we will invite you to an onsite interview next week")
    # with nothing after it making that conditional ("... if your application is selected").
    if _FUTURE.search(text, clause_start, found.start()):
        return bool(_TO_YOU.search(text, clause_start, found.end())) and not _CONJUNCTION.search(text, found.end(), clause_end)
    return True


_JOB = re.compile(r"\b(position|role|internship|intern|job|co-?op|offer|employment|join|joining|team|hire)\b")
_NOT_A_JOB = re.compile(
    r"\b(interviews?|phone|video|call|chat|screen|screening|meeting|slot|conversation|assessment|challenge|test|feedback)\b"
)


def _is_job_offer(text: str, found: re.Match[str]) -> bool:
    """An offer phrase that offers a job. "Pleased to offer you the opportunity to join Acme as an intern" and
    "pleased to offer the Software Intern position to you" do; "happy to offer you an interview slot", "pleased
    to offer you a phone screen" and "glad to offer you feedback" do not."""
    if not re.search(r"to (offer|extend) \w+$", found.group(0)):
        return True  # "offer letter", "offer of employment", "extend you an offer"
    _start, end = _bounds(text, found.start(), found.end())
    rest = text[found.end() - len(found.group(0).rsplit(" ", 1)[-1]):min(end, found.end() + 120)]
    job = _JOB.search(rest)
    other = _NOT_A_JOB.search(rest)
    return job is not None and (other is None or job.start() < other.start())


def classify_monitored_message(subject: str, body: str) -> tuple[str, float]:
    # A curly apostrophe reads as a straight one ("we'll", "won't" typed on a phone); the length stays the same.
    text = f"{subject}\n{body}".lower().replace("\u2019", "'")
    for event_type, pattern, confidence in MONITORED_PATTERNS:
        if any(_definite(event_type, text, found) for found in re.finditer(pattern, text, re.DOTALL)):
            if event_type == "rejected" and _HEDGED_REJECTION.search(text):
                return event_type, HEDGED_REJECTION_CONFIDENCE
            return event_type, confidence
    return "unknown", 0.1


def ingest_message(
    conn: sqlite3.Connection,
    connector_id: str,
    external_id: str,
    subject: str,
    body: str,
    sender: str = "",
    *,
    user_id: str,
    decisions: DecisionClient | None = None,
) -> dict[str, Any]:
    """Record one delivered email as a pending tracker update the student confirms or ignores.

    With a decisions client its type comes from Jev when it is sure enough;
    without one, or when Jev cannot answer, from classify_monitored_message.
    """
    connector = connector_record(conn, connector_id, user_id=user_id)
    if connector["status"] != "connected":
        raise ValueError("Connector is disconnected")
    existing = conn.execute(
        "SELECT id FROM monitored_events WHERE user_id=? AND connector_id=? AND external_id=?",
        (user_id, connector_id, external_id),
    ).fetchone()
    if existing:
        return monitored_event(conn, str(existing[0]), user_id=user_id)
    event_type, confidence, classified_by = classify_email(subject, body, classify_monitored_message, decisions)
    event_id = f"event-{uuid4().hex}"
    timestamp = utc_now()
    payload = {
        "subject": subject[:1_000], "body_preview": body[:2_000], "sender": sender[:500], "classified_by": classified_by,
    }
    with conn:
        conn.execute(
            """
            INSERT INTO monitored_events(
                id, user_id, connector_id, external_id, event_type, confidence,
                payload_json, status, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (event_id, user_id, connector_id, external_id, event_type, confidence, json.dumps(payload), timestamp),
        )
    return monitored_event(conn, event_id, user_id=user_id)


def monitored_event(conn: sqlite3.Connection, event_id: str, *, user_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM monitored_events WHERE id=? AND user_id=?", (event_id, user_id)).fetchone()
    if not row:
        raise ConnectionNotFoundError(event_id)
    result = dict(row)
    result["payload"] = json.loads(row["payload_json"] or "{}")
    result.pop("payload_json", None)
    return result


def list_monitored_events(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    ids = [row[0] for row in conn.execute("SELECT id FROM monitored_events WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()]
    return [monitored_event(conn, str(value), user_id=user_id) for value in ids]


def queue_notification(conn: sqlite3.Connection, channel: str, event_key: str, payload: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    preferences = ensure_preferences(conn, user_id=user_id)
    enabled = bool(preferences.get(f"{channel}_enabled", False))
    if channel in {"sms", "voice"} and not preferences["phone_verified"]:
        enabled = False
    notification_id = f"notification-{hashlib.sha256(f'{user_id}|{channel}|{event_key}'.encode()).hexdigest()[:24]}"
    timestamp = utc_now()
    # Development and test are deliberately non-delivery providers. Enabling a
    # channel records intent, but does not contact an external system.
    status = "sandbox_suppressed" if enabled else "cancelled"
    with conn:
        conn.execute(
            """
            INSERT INTO notification_outbox(id, user_id, channel, event_key, payload_json, status, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, channel, event_key) DO NOTHING
            """,
            (notification_id, user_id, channel, event_key, json.dumps(payload), status, timestamp),
        )
    row = conn.execute("SELECT * FROM notification_outbox WHERE user_id=? AND channel=? AND event_key=?", (user_id, channel, event_key)).fetchone()
    return {**dict(row), "payload": json.loads(row["payload_json"])}


def apply_channel_opt_out(conn: sqlite3.Connection, channel: str, keyword: str, *, user_id: str) -> dict[str, Any]:
    if channel not in {"email", "push", "sms", "voice"}:
        raise ValueError("Unsupported opt-out channel")
    if keyword.strip().upper() not in {"STOP", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}:
        raise ValueError("Unrecognized opt-out keyword")
    ensure_preferences(conn, user_id=user_id)
    field = f"{channel}_enabled"
    timestamp = utc_now()
    with conn:
        conn.execute(f"UPDATE notification_preferences SET {field}=0, updated_at=? WHERE user_id=?", (timestamp, user_id))
        conn.execute(
            """UPDATE notification_outbox SET status='cancelled'
               WHERE user_id=? AND channel=?
                 AND status IN ('queued', 'sandbox_suppressed')""",
            (user_id, channel),
        )
    return {"channel": channel, "opted_out": True, "updated_at": timestamp}


# What confirming an email does to the application the student picks.
# Assessment and scheduling emails add a task, never a stage.
EVENT_STAGES = {
    "application_confirmation": "applied", "interview": "interview", "offer": "offer", "rejected": "rejected",
    "assessment": None, "scheduling": None,
}
EVENT_TASKS = {"assessment": "Complete the assessment", "scheduling": "Schedule interview"}


def decide_monitored_event(conn: sqlite3.Connection, event_id: str, decision: str, application_id: str | None, *, user_id: str) -> dict[str, Any]:
    event = monitored_event(conn, event_id, user_id=user_id)
    if event["status"] != "pending":
        raise ValueError("This monitored event was already decided")
    if decision not in {"confirm", "ignore"}:
        raise ValueError("Decision must be confirm or ignore")
    if decision == "confirm" and not application_id:
        raise ValueError("Choose an application before confirming this update")
    if (event.get("payload") or {}).get("source") == "application_mail":
        # An email the app read from Gmail: its proposals are decided with it, so it is never decided twice.
        from .application_inbox import decide_event

        decided = decide_event(conn, event, decision, application_id, user_id=user_id)
        if decided is not None:
            return decided
    return decide_event_directly(conn, event, decision, application_id, user_id=user_id)


def decide_event_directly(
    conn: sqlite3.Connection, event: dict[str, Any], decision: str, application_id: str | None, *, user_id: str,
) -> dict[str, Any]:
    """Confirm or ignore an email with no automation proposals behind it: the stage or task it points to, then the event."""
    event_id = event["id"]
    timestamp = utc_now()
    status = "ignored"
    if decision == "confirm":
        stage = EVENT_STAGES.get(event["event_type"])
        if stage:
            update_application(conn, application_id, stage=stage, user_id=user_id, source=f"monitored_event:{event_id}")
        task = EVENT_TASKS.get(event["event_type"])
        if task:
            add_application_task(
                conn, application_id, title=task, user_id=user_id, origin="monitored_event", origin_ref=event_id,
                source=f"monitored_event:{event_id}",
            )
        status = "confirmed"
        queue_notification(conn, "in_app", f"monitored:{event_id}", {"event_type": event["event_type"], "application_id": application_id}, user_id=user_id)
    with conn:
        conn.execute(
            "UPDATE monitored_events SET status=?, application_id=?, decided_at=?, decided_by='student' WHERE id=? AND user_id=?",
            (status, application_id, timestamp, event_id, user_id),
        )
    return monitored_event(conn, event_id, user_id=user_id)


def request_phone_verification(conn: sqlite3.Connection, phone: str, secret: str, *, user_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        raise ValueError("Phone number must use E.164 format")
    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge_id = f"phone-{uuid4().hex}"
    created = datetime.now(timezone.utc)
    digest = hmac.new(secret.encode(), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()
    with conn:
        conn.execute(
            "INSERT INTO phone_verifications(id, user_id, phone_e164, code_hash, expires_at, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (challenge_id, user_id, phone, digest, (created + timedelta(minutes=10)).isoformat(), created.isoformat()),
        )
    return {"id": challenge_id, "phone_e164": phone, "expires_at": (created + timedelta(minutes=10)).isoformat(), "sandbox_code": code, "delivery": "sandbox_suppressed"}


def confirm_phone(conn: sqlite3.Connection, challenge_id: str, code: str, secret: str, *, user_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM phone_verifications WHERE id=? AND user_id=?", (challenge_id, user_id)).fetchone()
    if not row or row["status"] != "pending":
        raise ConnectionNotFoundError(challenge_id)
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        with conn:
            conn.execute("UPDATE phone_verifications SET status='expired' WHERE id=?", (challenge_id,))
        raise ValueError("Verification code expired")
    expected = hmac.new(secret.encode(), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, row["code_hash"]):
        with conn:
            conn.execute("UPDATE phone_verifications SET attempts=attempts+1 WHERE id=?", (challenge_id,))
        raise ValueError("Invalid verification code")
    timestamp = utc_now()
    ensure_preferences(conn, user_id=user_id)
    with conn:
        conn.execute("UPDATE phone_verifications SET status='verified' WHERE id=?", (challenge_id,))
        conn.execute("UPDATE notification_preferences SET phone_e164=?, phone_verified=1, updated_at=? WHERE user_id=?", (row["phone_e164"], timestamp, user_id))
    return ensure_preferences(conn, user_id=user_id)
