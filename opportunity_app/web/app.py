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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import DEFAULT_PLATFORM_DB, STATIC_DIR
from .. import bootstrap
from ..apply_runs import recover_stale as recover_stale_applications
from ..captures import DEFAULT_CAPTURE_STORAGE
from ..preparation import DEFAULT_MOCK_AUDIO_STORAGE
from ..resumes import DEFAULT_STORAGE
from ..schema import connect_product, ensure_product_schema
from .context import AppOptions, build_context
from .middleware import security_headers
from .routers import ROUTERS_AFTER_ASSETS, ROUTERS_BEFORE_ASSETS

if TYPE_CHECKING:  # only the create_app signature names these
    import httpx

    from ..agent_providers import AgentProvider
    from ..apply_schema_client import SchemaClient
    from ..boards import BoardTracker
    from ..outreach_call_prep import CallPrepWorker
    from ..outreach_discovery import DiscoveryManager
    from ..outreach_recontact import RecontactManager
    from ..outreach_settings import OutreachSettings
    from ..refresh import RefreshManager
    from ..system_status import SystemStatus
    from ..typesafe_decisions import DecisionClient
    from ..web_fetch import SafeFetcher

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


def create_app(
    *,
    db_path: Path = DEFAULT_PLATFORM_DB,
    access_token: str | None = None,
    static_dir: Path = STATIC_DIR,
    resume_storage: Path = DEFAULT_STORAGE,
    capture_storage: Path = DEFAULT_CAPTURE_STORAGE,
    interview_storage: Path = DEFAULT_MOCK_AUDIO_STORAGE,
    apply_storage: Path | None = None,
    employer_token: str | None = None,
    admin_token: str | None = None,
    database_url: str | None = None,
    rate_limit_per_minute: int = 240,
    agent_provider_factory: Callable[[str, str], AgentProvider] | None = None,
    refresh_manager: RefreshManager | None = None,
    outreach_discovery_manager: DiscoveryManager | None = None,
    outreach_recontact_manager: RecontactManager | None = None,
    system_status: SystemStatus | None = None,
    board_tracker: BoardTracker | None = None,
    outreach_settings: OutreachSettings | None = None,
    document_pdf_renderer: Callable[[str], bytes] | None = None,
    outreach_contact_client_factory: Callable[[], SafeFetcher] | None = None,
    outreach_contact_delay: float = 1.0,
    outreach_smtp_verifier_factory: Callable[[], Any] | None = None,
    outreach_renderer_factory: Callable[[], Any] | None = None,
    outreach_draft_provider: str | None = None,
    outreach_provider_factory: Callable[[str, str], AgentProvider] | None = None,
    outreach_gmail_client_factory: Callable[[], httpx.Client] | None = None,
    outreach_form_submitter_factory: Callable[..., Any] | None = None,
    apply_agent_factory: Any = None,
    apply_schema_client_factory: Callable[[], SchemaClient] | None = None,
    call_prep_worker: CallPrepWorker | None = None,
    start_call_prep_worker: bool | None = None,
    start_inbox_watcher: bool | None = None,
    start_automation_worker: bool | None = None,
    typesafe_client_factory: Callable[[], DecisionClient] | None = None,
    inbox_client_factory: Callable[[], DecisionClient | None] | None = None,
    profile_file: Path | None = None,
    early_programs_file: Path | None = None,
    allowed_hosts: list[str] | None = None,
    recovery_sandbox: bool = False,
) -> FastAPI:
    """Build an isolated app instance for production and tests.

    The parameters are spelled out here so that help(), IDEs and type checkers see them; they mirror the fields of ``AppOptions``
    (web/context.py) one for one, each with the default it always had (tests/test_web_app_context.py pins that they stay equal).
    """

    bootstrap.register_all()  # the automation, scheduler and callback registries, once per process (bootstrap.py)

    settings = AppOptions(
        db_path=db_path,
        access_token=access_token,
        static_dir=static_dir,
        resume_storage=resume_storage,
        capture_storage=capture_storage,
        interview_storage=interview_storage,
        apply_storage=apply_storage,
        employer_token=employer_token,
        admin_token=admin_token,
        database_url=database_url,
        rate_limit_per_minute=rate_limit_per_minute,
        agent_provider_factory=agent_provider_factory,
        refresh_manager=refresh_manager,
        outreach_discovery_manager=outreach_discovery_manager,
        outreach_recontact_manager=outreach_recontact_manager,
        system_status=system_status,
        board_tracker=board_tracker,
        outreach_settings=outreach_settings,
        document_pdf_renderer=document_pdf_renderer,
        outreach_contact_client_factory=outreach_contact_client_factory,
        outreach_contact_delay=outreach_contact_delay,
        outreach_smtp_verifier_factory=outreach_smtp_verifier_factory,
        outreach_renderer_factory=outreach_renderer_factory,
        outreach_draft_provider=outreach_draft_provider,
        outreach_provider_factory=outreach_provider_factory,
        outreach_gmail_client_factory=outreach_gmail_client_factory,
        outreach_form_submitter_factory=outreach_form_submitter_factory,
        apply_agent_factory=apply_agent_factory,
        apply_schema_client_factory=apply_schema_client_factory,
        call_prep_worker=call_prep_worker,
        start_call_prep_worker=start_call_prep_worker,
        start_inbox_watcher=start_inbox_watcher,
        start_automation_worker=start_automation_worker,
        typesafe_client_factory=typesafe_client_factory,
        inbox_client_factory=inbox_client_factory,
        profile_file=profile_file,
        early_programs_file=early_programs_file,
        allowed_hosts=allowed_hosts,
        recovery_sandbox=recovery_sandbox,
    )
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
