"""The one HTTP middleware: request ids and traces, security headers, the body-size and rate limits, CSRF and Unicode checks.

``security_headers`` is module-level and reads the app it runs in from ``request.app.state.ctx``, so every app shares the function
and keeps its own counters, rate windows and traces. tests/test_route_contract.py pins its name in the middleware stack.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from ..auth import constant_time_equal
from .assets import cache_control
from .context import SESSION_COOKIE, USER_SESSION_COOKIE

LOGGER = logging.getLogger("opportunity_app")


def _has_lone_surrogate(value: Any) -> bool:
    """True when decoded JSON holds an unpaired surrogate anywhere, keys included.

    json.loads accepts a "\\ud800" escape and yields a str that strict UTF-8
    encoding refuses, so it would otherwise fail deep in scrypt or the database
    driver as a 500. Surrogate pairs decode to one astral character and pass.
    """

    if isinstance(value, str):
        return any("\ud800" <= char <= "\udfff" for char in value)
    if isinstance(value, dict):
        return any(_has_lone_surrogate(key) or _has_lone_surrogate(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_lone_surrogate(item) for item in value)
    return False


def _json_body_has_lone_surrogate(body: bytes) -> bool:
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        # Malformed JSON is the route's validation error to report, not ours.
        return False
    return _has_lone_surrogate(decoded)


async def security_headers(request: Request, call_next):
    ctx = request.app.state.ctx
    app_metrics = ctx.runtime.metrics
    started = time.monotonic()
    request_id = request.headers.get("X-Request-ID", "")
    if not request_id or len(request_id) > 100 or not request_id.replace("-", "").isalnum():
        request_id = secrets.token_hex(16)
    incoming_trace = request.headers.get("traceparent", "")
    trace_match = re.fullmatch(
        r"00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}", incoming_trace
    )
    trace_id = trace_match.group(1) if trace_match else secrets.token_hex(16)
    span_id = secrets.token_hex(8)

    def finish(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: https:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        # Without this, browsers heuristically reuse a stale app.js against a
        # fresh index.html; no-cache still revalidates cheaply via ETag.
        if "cache-control" not in response.headers:
            response.headers["Cache-Control"] = cache_control(ctx, request, response.status_code)
        response.headers["X-Request-ID"] = request_id
        response.headers["traceparent"] = f"00-{trace_id}-{span_id}-01"
        elapsed = round((time.monotonic() - started) * 1000, 3)
        app_metrics["requests"] += 1
        app_metrics["latency_ms_total"] += elapsed
        latency_bucket = (
            app_metrics["read_latency_ms"]
            if request.method in {"GET", "HEAD", "OPTIONS"}
            else app_metrics["write_latency_ms"]
        )
        latency_bucket.append(elapsed)
        if response.status_code >= 500:
            app_metrics["errors"] += 1
        trace = {
            "request_id": request_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "latency_ms": elapsed,
        }
        ctx.runtime.traces.append(trace)
        LOGGER.info(json.dumps({"event": "http_request", **trace}))
        return response

    content_length = request.headers.get("Content-Length", "")
    if content_length.isdigit() and int(content_length) > 6 * 1024 * 1024:
        return finish(JSONResponse({"detail": "Request body is too large"}, status_code=413))
    now = time.monotonic()
    client_key = request.client.host if request.client else "unknown"
    window = ctx.runtime.rate_windows[client_key]
    while window and window[0] <= now - 60:
        window.popleft()
    if request.url.path != "/api/v1/health" and len(window) >= ctx.config.rate_limit_per_minute:
        app_metrics["rate_limited"] += 1
        return finish(JSONResponse({"detail": "Rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"}))
    window.append(now)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/api/v1/session":
        owner_cookie = request.cookies.get(SESSION_COOKIE) == ctx.config.expected_session
        user_session = request.cookies.get(USER_SESSION_COOKIE, "")
        cookie_authenticated = (owner_cookie or bool(user_session)) and not request.headers.get("Authorization")
        expected_csrf = ctx.config.csrf_token if owner_cookie else ctx.config.user_csrf_token(user_session)
        # Origin is present for browser fetches; command-line bearer clients are not subject to CSRF.
        if cookie_authenticated and request.headers.get("Origin"):
            supplied = request.headers.get("X-CSRF-Token", "")
            cookie_csrf = request.cookies.get("pipeline_csrf", "")
            if not supplied or not constant_time_equal(supplied, expected_csrf) or not constant_time_equal(cookie_csrf, expected_csrf):
                return finish(JSONResponse({"detail": "CSRF validation failed"}, status_code=403))
    content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and (not content_type or content_type.endswith("json")):
        if _json_body_has_lone_surrogate(await request.body()):
            return finish(JSONResponse({"detail": "Request body contains text that is not valid Unicode"}, status_code=422))
    response = await call_next(request)
    return finish(response)
