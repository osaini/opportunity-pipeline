"""Invite-gated local-owner email authentication adapter and sandbox recovery."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .schema import LOCAL_USER_ID, utc_now


def _password_hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def constant_time_equal(left: str, right: str) -> bool:
    """Compare arbitrary Unicode credentials without compare_digest's str limit.

    surrogatepass keeps an unpaired surrogate (a JSON "\\ud800" escape) from
    raising; such a value simply fails to match any real credential.
    """

    return hmac.compare_digest(left.encode("utf-8", "surrogatepass"), right.encode("utf-8", "surrogatepass"))


def _validate_password(password: str) -> None:
    if len(password) < 12 or len(password) > 512:
        raise ValueError("Password must contain 12 to 512 characters")
    if password.lower() == password or password.upper() == password or not any(char.isdigit() for char in password):
        raise ValueError("Password must include upper- and lowercase letters and a number")


def register_owner(conn: sqlite3.Connection, email: str, password: str, display_name: str) -> dict[str, Any]:
    email = email.strip().casefold()
    if "@" not in email or len(email) > 320:
        raise ValueError("A valid email address is required")
    _validate_password(password)
    existing = conn.execute("SELECT email FROM users WHERE id=?", (LOCAL_USER_ID,)).fetchone()
    if not existing:
        raise ValueError("Migrate or initialize the local owner before registration")
    if existing["email"]:
        raise ValueError("The local owner account is already registered")
    salt = secrets.token_bytes(16)
    timestamp = utc_now()
    with conn:
        conn.execute("UPDATE users SET email=?, display_name=?, updated_at=? WHERE id=?", (email, display_name.strip() or "Local user", timestamp, LOCAL_USER_ID))
        conn.execute("INSERT INTO user_credentials(user_id, password_hash, password_salt, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                     (LOCAL_USER_ID, _password_hash(password, salt), salt.hex(), timestamp, timestamp))
    return {"registered": True, "email": email, "display_name": display_name.strip() or "Local user"}


def authenticate_password(conn: sqlite3.Connection, email: str, password: str) -> bool:
    row = conn.execute("""SELECT u.email, c.password_hash, c.password_salt FROM users u
        JOIN user_credentials c ON c.user_id=u.id WHERE u.id=?""", (LOCAL_USER_ID,)).fetchone()
    if not row or not row["email"] or not constant_time_equal(str(row["email"]).casefold(), email.strip().casefold()):
        # Keep an equivalent expensive path for nonexistent accounts.
        _password_hash(password, b"\0" * 16)
        return False
    actual = _password_hash(password, bytes.fromhex(row["password_salt"]))
    return constant_time_equal(actual, row["password_hash"])


def register_student(conn: sqlite3.Connection, email: str, password: str, display_name: str) -> dict[str, Any]:
    """Open registration into the student role; callers must gate on a feature flag."""
    email = email.strip().casefold()
    if "@" not in email or len(email) > 320:
        raise ValueError("A valid email address is required")
    _validate_password(password)
    existing = conn.execute(
        "SELECT id FROM users WHERE lower(email)=?", (email,)
    ).fetchone()
    if existing:
        raise ValueError("An account with this email already exists")
    user_id = f"user-{uuid4().hex}"
    salt = secrets.token_bytes(16)
    timestamp = utc_now()
    with conn:
        conn.execute(
            "INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES(?, ?, ?, 'student', ?, ?)",
            (user_id, email, display_name.strip() or "Student", timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO user_credentials(user_id, password_hash, password_salt, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
            (user_id, _password_hash(password, salt), salt.hex(), timestamp, timestamp),
        )
    return {"registered": True, "email": email, "display_name": display_name.strip() or "Student", "user_id": user_id}


def authenticate_email_password(conn: sqlite3.Connection, email: str, password: str) -> str | None:
    """Return the user id for any valid email+password pair, else None."""
    row = conn.execute(
        """SELECT u.id, u.email, c.password_hash, c.password_salt FROM users u
        JOIN user_credentials c ON c.user_id=u.id WHERE lower(u.email)=?""",
        (email.strip().casefold(),),
    ).fetchone()
    if not row:
        _password_hash(password, b"\0" * 16)
        return None
    actual = _password_hash(password, bytes.fromhex(row["password_salt"]))
    if constant_time_equal(actual, row["password_hash"]):
        return str(row["id"])
    return None


def feature_flag_enabled(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT enabled FROM feature_flags WHERE key=?", (key,)).fetchone()
    return bool(row and row["enabled"])


def hash_secret(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_user_token(conn: sqlite3.Connection, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with conn:
        conn.execute(
            "INSERT INTO user_api_tokens(token_hash, user_id, created_at) VALUES(?, ?, ?)",
            (hash_secret(token), user_id, utc_now()),
        )
    return token


def revoke_user_token(conn: sqlite3.Connection, token: str) -> None:
    """Revoke one token, e.g. the browser session a student signs out of."""
    with conn:
        conn.execute(
            "UPDATE user_api_tokens SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
            (utc_now(), hash_secret(token)),
        )


def resolve_user_token(conn: sqlite3.Connection, token: str) -> str | None:
    row = conn.execute(
        "SELECT user_id FROM user_api_tokens WHERE token_hash=? AND revoked_at IS NULL",
        (hash_secret(token),),
    ).fetchone()
    if not row:
        return None
    try:
        with conn:
            conn.execute(
                "UPDATE user_api_tokens SET last_used_at=? WHERE token_hash=?",
                (utc_now(), hash_secret(token)),
            )
    except Exception:  # noqa: BLE001
        # A read-only connection must still authenticate; usage tracking is
        # best-effort only. The catch stays broad on purpose: it was
        # `sqlite3.Error`, which let PostgreSQL's ReadOnlySqlTransaction
        # (SQLSTATE 25006) escape and turned every student-token request into a
        # 500. Naming backend exception types reintroduces that bug for the next
        # backend, and nothing here is worth failing authentication over.
        pass
    return str(row["user_id"])


def request_recovery(
    conn: sqlite3.Connection,
    email: str,
    secret: str,
    *,
    deliver_code: Any = None,
    expose_code: bool = False,
) -> dict[str, Any]:
    """Start a password recovery for the local owner.

    The code goes out only through ``deliver_code`` (a live mail provider). With
    none, no code is created and the answer is the same for every address: a
    code handed back in the response would let anyone who can reach the port
    and knows the owner's email reset the password. ``expose_code`` restores
    that for the throwaway sandbox server and tests only; the launcher's
    one-time sign-in is the way back in on a real copy.
    """
    unavailable = {"accepted": True, "delivery": "unavailable"}
    if deliver_code is None and not expose_code:
        return unavailable
    row = conn.execute("SELECT id FROM users WHERE id=? AND lower(email)=?", (LOCAL_USER_ID, email.strip().casefold())).fetchone()
    # The public response remains indistinguishable; sandbox code is exposed only for a matching local owner.
    response: dict[str, Any] = {"accepted": True, "delivery": "sandbox_suppressed"}
    if not row:
        return response
    challenge_id = f"recovery-{uuid4().hex}"
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)
    digest = hmac.new(secret.encode(), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()
    with conn:
        conn.execute("INSERT INTO recovery_challenges(id, user_id, code_hash, expires_at, created_at) VALUES(?, ?, ?, ?, ?)",
                     (challenge_id, LOCAL_USER_ID, digest, expires.isoformat(), utc_now()))
    if deliver_code is not None:
        result = deliver_code(email.strip(), code)
        if result and result.get("delivered"):
            return {"accepted": True, "delivery": "sent", "challenge_id": challenge_id, "expires_at": expires.isoformat()}
        if not expose_code:
            # Delivery failed: never fall back to showing the code.
            return unavailable
    response.update({"challenge_id": challenge_id, "sandbox_code": code, "expires_at": expires.isoformat()})
    return response


def complete_recovery(conn: sqlite3.Connection, challenge_id: str, code: str, new_password: str, secret: str) -> dict[str, Any]:
    _validate_password(new_password)
    row = conn.execute("SELECT * FROM recovery_challenges WHERE id=? AND user_id=?", (challenge_id, LOCAL_USER_ID)).fetchone()
    if not row or row["status"] != "pending":
        raise ValueError("Recovery challenge is invalid")
    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        with conn:
            conn.execute("UPDATE recovery_challenges SET status='expired' WHERE id=?", (challenge_id,))
        raise ValueError("Recovery challenge expired")
    expected = hmac.new(secret.encode(), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()
    if not constant_time_equal(expected, row["code_hash"]):
        with conn:
            conn.execute("UPDATE recovery_challenges SET attempts=attempts+1 WHERE id=?", (challenge_id,))
        raise ValueError("Recovery code is invalid")
    salt = secrets.token_bytes(16)
    timestamp = utc_now()
    with conn:
        conn.execute("UPDATE user_credentials SET password_hash=?, password_salt=?, updated_at=? WHERE user_id=?",
                     (_password_hash(new_password, salt), salt.hex(), timestamp, LOCAL_USER_ID))
        conn.execute("UPDATE recovery_challenges SET status='used' WHERE id=?", (challenge_id,))
    return {"recovered": True}
