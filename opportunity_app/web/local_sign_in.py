"""The owner's browser session cookies, and opening the app signed in on a computer that skips sign-in.

With ``PIPELINE_SKIP_SIGN_IN=1`` in ``.env`` (``AppConfig.skip_sign_in``, which holds only for a loopback-only server outside
production), each page of the student app that this computer's browser opens sets the owner's session, so there is no sign-in
gate. Three things keep that to this computer's own browser:

* the connection comes from a loopback address, so another machine is never signed in;
* the ``Host`` is a loopback name, so a page on another site that re-points its own DNS name at 127.0.0.1 is not signed in
  under that name (``TrustedHostMiddleware`` refuses it first; this checks again);
* a browser already signed in to another account (a student session cookie) is left as it is.

What it does not do: any program running on this computer can open the page and get the owner's cookie, as any program that can
read ``.env`` can already get the owner token. That is the trade the setting makes, so it is off unless the student turns it on.
"""

from __future__ import annotations

import os

from fastapi import Request, Response

from .context import LAUNCH_SESSION_SECONDS, LOOPBACK_HOSTS, SESSION_COOKIE, USER_SESSION_COOKIE, AppContext

LOOPBACK_CLIENTS = ("127.0.0.1", "::1")


def set_owner_session_cookies(response: Response, ctx: AppContext, max_age: int) -> None:
    """Sign the browser in as the owner: the session and CSRF cookies, and no student session beside them."""
    secure = os.environ.get("PIPELINE_ENV") == "production"
    response.delete_cookie(USER_SESSION_COOKIE, path="/")
    response.set_cookie(
        key=SESSION_COOKIE,
        value=ctx.config.expected_session,
        httponly=True,
        samesite="strict",
        secure=secure,
        max_age=max_age,
        path="/",
    )
    response.set_cookie(
        key="pipeline_csrf", value=ctx.config.csrf_token, httponly=False, samesite="strict",
        secure=secure, max_age=max_age, path="/",
    )


def skips_sign_in(request: Request, ctx: AppContext) -> bool:
    """True when this request is this computer's own browser and sign-in is turned off for it."""
    if not ctx.config.skip_sign_in:
        return False
    if request.client is None or request.client.host not in LOOPBACK_CLIENTS:
        return False
    if request.url.hostname not in LOOPBACK_HOSTS:
        return False
    return not request.cookies.get(USER_SESSION_COOKIE)


def sign_in_this_computer(request: Request, response: Response, ctx: AppContext) -> None:
    """Open the page signed in when sign-in is skipped. Each visit renews the 30-day session."""
    if skips_sign_in(request, ctx):
        set_owner_session_cookies(response, ctx, LAUNCH_SESSION_SECONDS)
