"""Sign-in, registration, account recovery and sign-out."""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from contextlib import closing
from pathlib import Path
from typing import Annotated, Any

from fastapi import Cookie, Depends, HTTPException, Request, Response, status

from ..overrides import shared_router
from ...auth import (
    authenticate_email_password,
    authenticate_password,
    complete_recovery,
    constant_time_equal,
    feature_flag_enabled,
    issue_user_token,
    register_owner,
    register_student,
    request_recovery,
    revoke_user_token,
)
from ...profile import get_profile
from ...database import connect_product, is_postgres_target
from ...schema import LOCAL_USER_ID
from ...notifications import build_provider as build_notification_provider
from ..context import AppContext, LAUNCH_SESSION_SECONDS, LAUNCH_TICKET_SECONDS, SESSION_COOKIE, USER_SESSION_COOKIE
from ..dependencies import get_ctx, require_auth
from ..models.session import (
    LaunchTicketResponse,
    RecoveryCompleteRequest,
    RecoveryRequest,
    RegistrationRequest,
    SessionRequest,
    SessionResponse,
)


router = shared_router()
sign_out_router = shared_router()


def _display_name(conn_target: str, user_id: str) -> str:
    if is_postgres_target(conn_target) or Path(conn_target).exists():
        with closing(connect_product(conn_target, read_only=True)) as conn:
            row = conn.execute("SELECT display_name FROM users WHERE id=?", (user_id,)).fetchone()
            if row and row[0]:
                return str(row[0])
    return "Local user" if user_id == LOCAL_USER_ID else "Student"


def redeem_launch_ticket(ctx: AppContext, ticket: str) -> bool:
    with ctx.runtime.launch_tickets_lock:
        expiry = ctx.runtime.launch_tickets.pop(hashlib.sha256(ticket.encode()).hexdigest(), None)
    return expiry is not None and expiry > time.monotonic()


@router.post("/api/v1/auth/launch-ticket", response_model=LaunchTicketResponse)
def create_launch_ticket(request: Request, ctx: AppContext = Depends(get_ctx)) -> LaunchTicketResponse:
    """Mint a one-time sign-in ticket for the launcher to open the browser with.

    Only the static owner token, sent as a bearer header, may ask: the
    launcher reads it from .env on the same machine. A browser cookie is not
    enough, so a page cannot mint a ticket for itself. The ticket is good
    once, for LAUNCH_TICKET_SECONDS, and is stored only as a hash.
    """
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not constant_time_equal(credential.strip(), ctx.config.access_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Owner token required")
    ticket = secrets.token_urlsafe(32)
    now = time.monotonic()
    with ctx.runtime.launch_tickets_lock:
        for key in [key for key, expiry in ctx.runtime.launch_tickets.items() if expiry <= now]:
            del ctx.runtime.launch_tickets[key]
        ctx.runtime.launch_tickets[hashlib.sha256(ticket.encode()).hexdigest()] = now + LAUNCH_TICKET_SECONDS
    return LaunchTicketResponse(ticket=ticket, expires_in=LAUNCH_TICKET_SECONDS)


@router.post("/api/v1/session", response_model=SessionResponse)
def create_session(payload: SessionRequest, response: Response, ctx: AppContext = Depends(get_ctx)) -> SessionResponse:
    token_ok = payload.token is not None and constant_time_equal(payload.token, ctx.config.access_token)
    launched = not token_ok and payload.launch_ticket is not None and redeem_launch_ticket(ctx, payload.launch_ticket)
    authenticated_user: str | None = LOCAL_USER_ID if token_ok or launched else None
    issued_token: str | None = None
    password_ok = False
    if not token_ok and not launched and payload.email and payload.password and ctx.config.database_present():
        with closing(connect_product(ctx.config.database_target)) as conn:
            authenticated_user = authenticate_email_password(conn, payload.email, payload.password)
            if authenticated_user:
                # Password logins hand back a per-user API token. The
                # browser session cookie stays owner-scoped: it grants the
                # local owner identity and must never be derived from a
                # student login.
                if authenticated_user != LOCAL_USER_ID:
                    issued_token = issue_user_token(conn, authenticated_user)
            elif authenticate_password(conn, payload.email, payload.password):
                authenticated_user = LOCAL_USER_ID
    if not authenticated_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    secure = os.environ.get("PIPELINE_ENV") == "production"
    if authenticated_user == LOCAL_USER_ID or issued_token is None:
        # The launcher proves it runs as this computer's user, so its
        # session is remembered longer than one typed in a sign-in form.
        owner_max_age = LAUNCH_SESSION_SECONDS if launched else 60 * 60 * 12
        response.delete_cookie(USER_SESSION_COOKIE, path="/")
        response.set_cookie(
            key=SESSION_COOKIE,
            value=ctx.config.expected_session,
            httponly=True,
            samesite="strict",
            secure=secure,
            max_age=owner_max_age,
            path="/",
        )
        response.set_cookie(
            key="pipeline_csrf", value=ctx.config.csrf_token, httponly=False, samesite="strict",
            secure=secure, max_age=owner_max_age, path="/",
        )
    else:
        # A lingering owner cookie takes precedence in require_auth and would
        # hand this student the owner identity, so clear it.
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.set_cookie(
            key=USER_SESSION_COOKIE,
            value=issued_token,
            httponly=True,
            samesite="strict",
            secure=secure,
            max_age=60 * 60 * 12,
            path="/",
        )
        response.set_cookie(
            key="pipeline_csrf", value=ctx.config.user_csrf_token(issued_token), httponly=False, samesite="strict",
            secure=secure, max_age=60 * 60 * 12, path="/",
        )
    return SessionResponse(
        authenticated=True,
        user_id=authenticated_user,
        display_name=_display_name(ctx.config.database_target, authenticated_user),
        api_token=issued_token,
    )


@router.post("/api/v1/auth/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegistrationRequest, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    invite_present = payload.invite_token is not None
    if invite_present and constant_time_equal(payload.invite_token, ctx.config.access_token):
        with closing(connect_product(ctx.config.database_target)) as conn:
            try:
                result = register_owner(conn, payload.email, payload.password, payload.display_name)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            result["api_token"] = issue_user_token(conn, LOCAL_USER_ID)
        return result
    if invite_present:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="A valid owner invitation is required")
    # No invite supplied: open registration proceeds only while the admin
    # feature flag is enabled.
    if ctx.config.database_present():
        with closing(connect_product(ctx.config.database_target)) as conn:
            if not feature_flag_enabled(conn, "allow_public_signup"):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Public signup is disabled")
            try:
                result = register_student(conn, payload.email, payload.password, payload.display_name)
                get_profile(conn, user_id=result["user_id"])
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            result["api_token"] = issue_user_token(conn, result["user_id"])
        return result
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Product database unavailable")


@router.post("/api/v1/auth/recovery")
def begin_recovery(payload: RecoveryRequest, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    provider = build_notification_provider()
    deliver_code = None
    if provider.live:
        def deliver_code(recipient: str, code: str) -> dict[str, Any]:
            return provider.deliver("email", recipient, "Your access recovery code", f"Your recovery code is {code}. It expires in 15 minutes.")
    with closing(connect_product(ctx.config.database_target)) as conn:
        return request_recovery(
            conn, payload.email, ctx.config.access_token, deliver_code=deliver_code, expose_code=ctx.config.recovery_sandbox
        )


@router.post("/api/v1/auth/recovery/complete")
def finish_recovery(payload: RecoveryCompleteRequest, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with closing(connect_product(ctx.config.database_target)) as conn:
        try:
            return complete_recovery(conn, payload.challenge_id, payload.code, payload.new_password, ctx.config.access_token)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/session", response_model=SessionResponse)
def session_status(authenticated_user: str = Depends(require_auth), ctx: AppContext = Depends(get_ctx)) -> SessionResponse:
    return SessionResponse(
        authenticated=True,
        user_id=authenticated_user,
        display_name=_display_name(ctx.config.database_target, authenticated_user),
    )


@sign_out_router.delete("/api/v1/session", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    response: Response,
    user_session_cookie: Annotated[str | None, Cookie(alias=USER_SESSION_COOKIE)] = None,
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    if user_session_cookie and ctx.config.database_present():
        with closing(connect_product(ctx.config.database_target)) as conn:
            revoke_user_token(conn, user_session_cookie)
    response.delete_cookie(USER_SESSION_COOKIE, path="/")
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie("pipeline_csrf", path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
