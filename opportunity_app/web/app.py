"""The composition root: build the context, the FastAPI app, its middleware and lifespan, and attach the shared route table.

The routes are not built here. They live on module-level routers (``web.routers``) that FastAPI analyses once, when the module is
imported; every app then lists the same route objects (``app.router.routes.extend``). ``include_router`` would copy each route and
pay the analysis again, which is what made ``create_app`` cost most of a second. The shared routes carry no reference to an app
(``dependency_overrides_provider`` is None), and read everything per-app from ``request.app.state.ctx``. Because of that,
``app.dependency_overrides`` does not reach them; nothing uses it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager, closing
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import application_inbox, outreach_thank_you  # noqa: F401  (each registers its automation handlers when imported)
from ..apply_runs import recover_stale as recover_stale_applications
from ..schema import connect_product, ensure_product_schema
from .context import AppOptions, build_context
from .middleware import security_headers
from .routers import ROUTERS_AFTER_ASSETS, ROUTERS_BEFORE_ASSETS

LOGGER = logging.getLogger("opportunity_app")

# Every route of every router, in registration order, built once per process.
ROUTES_BEFORE_ASSETS = tuple(route for router in ROUTERS_BEFORE_ASSETS for route in router.routes)
ROUTES_AFTER_ASSETS = tuple(route for router in ROUTERS_AFTER_ASSETS for route in router.routes)


@asynccontextmanager
async def lifespan(application: FastAPI):
    ctx = application.state.ctx
    config, services = ctx.config, ctx.services
    if not config.database_present():
        LOGGER.warning(
            "Product database is missing. Run: python -m opportunity_app.setup init"
        )
    else:
        with closing(connect_product(config.database_target)) as migration_connection:
            ensure_product_schema(migration_connection)
            # What a server that stopped mid-application left behind (5.2 rule 7); the worker keeps at it.
            try:
                recover_stale_applications(migration_connection)
            except Exception:  # noqa: BLE001 - starting never waits on it
                LOGGER.exception("Applications a stopped server left were not recovered")
        if config.start_call_prep_worker:
            services.call_prep_worker.start()
        if config.start_inbox_watcher:
            services.inbox_watcher.start()
        if config.start_automation_worker:
            services.automation_worker.start()
    LOGGER.warning("Local web access token: %s", application.state.access_token)
    try:
        yield
    finally:
        services.call_prep_worker.stop()
        services.inbox_watcher.stop()
        services.automation_worker.stop()
        for connection in list(ctx.runtime.open_connections):
            try:
                connection.close()
            finally:
                ctx.runtime.open_connections.discard(connection)


def create_app(**options: Any) -> FastAPI:
    """Build an isolated app instance for production and tests.

    The keywords are the fields of ``AppOptions`` (web/context.py), each with the default it always had; an unknown keyword is a
    TypeError.
    """

    settings = AppOptions(**options)
    ctx = build_context(settings)
    app = FastAPI(
        title="Opportunity Pipeline API",
        version="1.0.0",
        description="Student, employer, administrator, agent, and operations API over the opportunity pipeline.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"chrome-extension://[a-p]{32}",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token", "X-Request-ID"],
    )
    if settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.state.ctx = ctx
    app.state.db_path = ctx.config.database_target
    app.state.access_token = ctx.config.access_token
    app.state.call_prep_worker = ctx.services.call_prep_worker
    app.state.employer_token = ctx.config.employer_token
    app.state.admin_token = ctx.config.admin_token
    app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers)

    app.router.routes.extend(ROUTES_BEFORE_ASSETS)
    # Per app, and between /api/v1/stats and the page routes, where it has always been.
    app.mount("/assets", StaticFiles(directory=ctx.config.static_dir), name="assets")
    app.router.routes.extend(ROUTES_AFTER_ASSETS)
    return app
