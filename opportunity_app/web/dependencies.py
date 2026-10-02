"""Authentication and database-connection dependencies for the web routes.

Each reads the app it serves from ``request.app.state.ctx`` (``get_ctx``), so one set of module-level dependencies serves every
app in the process. Roles stay distinct, and so do the connection dependencies, whose small differences are deliberate:

* ``repository`` opens read-only and maps a missing file as well as a SQLite open failure to 503.
* ``writable_connection`` and ``extension_connection`` map only a SQLite open failure to 503.
* ``employer_connection`` and ``admin_connection`` map nothing: a failure to open is a 500, as it has always been, and
  ``ensure_actor`` runs between the open and the ``try`` that closes.

The 503 covers the OPEN only. A generator dependency receives whatever its handler raises at the ``yield``, so mapping around the
yield would turn a handler's own ``sqlite3.OperationalError`` (a locked database, a missing table) into a 503 that tells the student
to run the migration command. tests/test_web_app_context.py pins that.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, status

from pipeline_core import OpportunityRepository

from ..accounts.auth import constant_time_equal, resolve_user_token
from ..accounts.employer import ensure_actor
from ..applications.extension import resolve_extension_token
from ..core.database import connect_product
from ..core.schema import LOCAL_USER_ID
from .context import SESSION_COOKIE, USER_SESSION_COOKIE, AppContext

DATABASE_UNAVAILABLE = "Product database unavailable; run the migration command first"


def get_ctx(request: Request) -> AppContext:
    """The context of the app serving this request."""
    return request.app.state.ctx


def _bearer_value(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value


def require_auth(
    ctx: AppContext = Depends(get_ctx),
    authorization: Annotated[str | None, Header()] = None,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    user_session_cookie: Annotated[str | None, Cookie(alias=USER_SESSION_COOKIE)] = None,
) -> str:
    bearer = _bearer_value(authorization)
    if bearer is not None:
        if constant_time_equal(bearer, ctx.config.access_token):
            return LOCAL_USER_ID
        if ctx.config.database_present():
            with closing(connect_product(ctx.config.database_target, read_only=True)) as conn:
                resolved_user = resolve_user_token(conn, bearer)
            if resolved_user:
                return resolved_user
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    cookie_ok = session_cookie is not None and constant_time_equal(
        session_cookie, ctx.config.expected_session
    )
    if not cookie_ok and user_session_cookie:
        if ctx.config.database_present():
            with closing(connect_product(ctx.config.database_target, read_only=True)) as conn:
                resolved_user = resolve_user_token(conn, user_session_cookie)
            if resolved_user:
                return resolved_user
    if not cookie_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return LOCAL_USER_ID


def require_browser_session(
    request: Request,
    authenticated_user: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> str:
    """The student's own signed-in browser, not a script holding an access token (spec 4.6).

    The owner's bearer token lives in ``.env`` and is used by local tooling and scheduled agents, which must not be
    able to record the student's consent. So this refuses any request that carries an ``Authorization`` header, needs
    a session cookie, and for a write checks the ``X-CSRF-Token`` header against the session's own token whether or
    not an ``Origin`` header is present (the general middleware checks only when there is one).
    """
    if request.headers.get("Authorization"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This needs your signed-in browser, not an access token")
    owner_cookie = constant_time_equal(request.cookies.get(SESSION_COOKIE, ""), ctx.config.expected_session)
    user_session = request.cookies.get(USER_SESSION_COOKIE, "")
    if not owner_cookie and not user_session:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This needs your signed-in browser session")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        expected = ctx.config.csrf_token if owner_cookie else ctx.config.user_csrf_token(user_session)
        supplied = request.headers.get("X-CSRF-Token", "")
        cookie_csrf = request.cookies.get("pipeline_csrf", "")
        if not supplied or not constant_time_equal(supplied, expected) or not constant_time_equal(cookie_csrf, expected):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")
    return authenticated_user


def require_owner(authenticated_user: str = Depends(require_auth)) -> str:
    if authenticated_user != LOCAL_USER_ID:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can refresh the pipeline")
    return authenticated_user


def _open_tracked(ctx: AppContext, unavailable: tuple[type[BaseException], ...], **options: bool) -> sqlite3.Connection:
    """Open a connection and remember it so shutdown can close it. Only a failure to OPEN maps to 503, and only for `unavailable`."""
    try:
        conn = connect_product(ctx.config.database_target, **options)
        ctx.runtime.open_connections.add(conn)
    except unavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=DATABASE_UNAVAILABLE,
        ) from exc
    return conn


def _release(ctx: AppContext, conn: sqlite3.Connection) -> None:
    conn.close()
    ctx.runtime.open_connections.discard(conn)


def repository(
    authenticated_user: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> Iterator[OpportunityRepository]:
    conn = _open_tracked(ctx, (FileNotFoundError, sqlite3.OperationalError), read_only=True)
    try:
        yield OpportunityRepository(conn, user_id=authenticated_user)
    finally:
        _release(ctx, conn)


def writable_connection(
    _authenticated_user: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> Iterator[sqlite3.Connection]:
    conn = _open_tracked(ctx, (sqlite3.OperationalError,))
    try:
        yield conn
    finally:
        _release(ctx, conn)


def require_extension_auth(
    ctx: AppContext = Depends(get_ctx),
    authorization: Annotated[str | None, Header()] = None,
    origin: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    bearer = _bearer_value(authorization)
    if not bearer or not origin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Extension authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not ctx.config.database_present():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=DATABASE_UNAVAILABLE,
        )
    with closing(connect_product(ctx.config.database_target)) as conn:
        device = resolve_extension_token(conn, bearer, origin)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Extension token is invalid, revoked, or bound to another origin",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return device


def extension_connection(
    device: dict[str, str] = Depends(require_extension_auth),
    ctx: AppContext = Depends(get_ctx),
) -> Iterator[tuple[sqlite3.Connection, dict[str, str]]]:
    conn = _open_tracked(ctx, (sqlite3.OperationalError,))
    try:
        yield conn, device
    finally:
        _release(ctx, conn)


def require_employer(
    ctx: AppContext = Depends(get_ctx),
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    bearer = _bearer_value(authorization)
    if bearer is not None and constant_time_equal(bearer, ctx.config.employer_token):
        return "employer-user"
    if bearer is not None and (constant_time_equal(bearer, ctx.config.access_token) or constant_time_equal(bearer, ctx.config.admin_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Employer role required")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Employer authentication required")


def require_admin(
    ctx: AppContext = Depends(get_ctx),
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    bearer = _bearer_value(authorization)
    if bearer is not None and constant_time_equal(bearer, ctx.config.admin_token):
        return "admin-user"
    if bearer is not None and (constant_time_equal(bearer, ctx.config.access_token) or constant_time_equal(bearer, ctx.config.employer_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required")


def employer_connection(
    actor: str = Depends(require_employer),
    ctx: AppContext = Depends(get_ctx),
) -> Iterator[tuple[sqlite3.Connection, str]]:
    # No 503 mapping and no tracking helper's try: a failure to open is a 500, and ensure_actor runs before the
    # try that closes (the same as before the split).
    conn = connect_product(ctx.config.database_target)
    ctx.runtime.open_connections.add(conn)
    ensure_actor(conn, actor, "employer")
    try:
        yield conn, actor
    finally:
        _release(ctx, conn)


def admin_connection(
    actor: str = Depends(require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> Iterator[tuple[sqlite3.Connection, str]]:
    conn = connect_product(ctx.config.database_target)
    ctx.runtime.open_connections.add(conn)
    ensure_actor(conn, actor, "admin")
    try:
        yield conn, actor
    finally:
        _release(ctx, conn)
