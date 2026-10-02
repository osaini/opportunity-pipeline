"""The per-app context: everything a request needs that belongs to one app instance and not to the process.

``create_app`` used to be one closure over about forty values, so every route and dependency was a closure and FastAPI re-analysed
all of them for each new app. The values now live in an ``AppContext`` stored on ``app.state.ctx``; routes and dependencies are
module-level and reach it through ``Depends(get_ctx)``, which is what lets the route table be built once per process.

The context has three parts:

* ``AppConfig``   immutable settings resolved once: the database target, storage folders, the tokens, the rate limit.
* ``AppServices`` the factories, managers and workers the routes call. Each is the caller's override or the production default.
* ``AppRuntime``  the state that changes while the app runs: launch tickets, rate windows, metrics, traces, open connections and
                  the asset-version and Apply schema caches.

Nothing per-app is a module global. A second app built in the same process gets its own context and shares nothing mutable with
the first, except apply_runs' agent factory, which has always been "the last app built wins" and is left as it was.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from .. import APPLY_ROOT, DEFAULT_PLATFORM_DB, DEFAULT_PROFILE, STATIC_DIR
from ..apply import preflight as apply_preflight, runs as apply_runs
from ..integrations.agent_providers import AgentProvider, build_provider
from ..apply.schema_client import SchemaClient, default_schema_client_factory
from ..opportunities.boards import BoardTracker
from ..opportunities.captures import DEFAULT_CAPTURE_STORAGE
from ..core.database import is_postgres_target
from ..integrations.pdf import pdf_renderer
from ..opportunities.early_programs import DEFAULT_EARLY_PROGRAMS
from ..mail.classifiers import build_client as build_inbox_client, client_for as inbox_client_for
from ..inbox_watcher import InboxWatcher
from ..opportunities.legacy import load_env_file
from ..outreach_automation import AutomationWorker
from ..outreach_call_prep import CallPrepWorker, auto_queue_call_prep
from ..outreach_discovery import DiscoveryManager
from ..outreach_forms import default_submitter_factory as default_form_submitter_factory
from ..outreach_interviewer import web_interviewer
from ..outreach_recontact import RecontactManager
from ..outreach_render import default_renderer
from ..outreach_research import web_researcher
from ..outreach_settings import OutreachSettings
from ..integrations.smtp_probe import default_verifier as default_smtp_verifier
from ..student.preparation import DEFAULT_MOCK_AUDIO_STORAGE
from ..opportunities.refresh import RefreshManager
from ..student.resumes import DEFAULT_STORAGE
from ..system_status import SystemStatus
from ..integrations.typesafe_decisions import DecisionClient, build_client as build_typesafe_client
from ..integrations.web_fetch import SafeFetcher, default_fetcher as default_contact_fetcher
from ..integrations.gmail_client import default_client_factory as default_gmail_client_factory

SESSION_COOKIE = "pipeline_session"
# Students sign in to the browser with their own per-user token. It lives in a
# separate cookie so the owner-scoped session cookie is never issued to them.
USER_SESSION_COOKIE = "pipeline_user_session"
# A launcher ticket is exchanged within seconds of being minted; the session it
# opens is remembered on this computer for 30 days.
LAUNCH_TICKET_SECONDS = 60
LAUNCH_SESSION_SECONDS = 60 * 60 * 24 * 30
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


def _session_signature(access_token: str) -> str:
    return hmac.new(
        access_token.encode("utf-8"),
        b"pipeline-local-session-v1",
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True, kw_only=True)
class AppOptions:
    """What a caller may pass to ``create_app``. Every field has the default the keyword always had."""

    db_path: Path = DEFAULT_PLATFORM_DB
    access_token: str | None = None
    static_dir: Path = STATIC_DIR
    resume_storage: Path = DEFAULT_STORAGE
    capture_storage: Path = DEFAULT_CAPTURE_STORAGE
    interview_storage: Path = DEFAULT_MOCK_AUDIO_STORAGE
    apply_storage: Path | None = None
    employer_token: str | None = None
    admin_token: str | None = None
    database_url: str | None = None
    rate_limit_per_minute: int = 240
    agent_provider_factory: Callable[[str, str], AgentProvider] | None = None
    refresh_manager: RefreshManager | None = None
    outreach_discovery_manager: DiscoveryManager | None = None
    outreach_recontact_manager: RecontactManager | None = None
    system_status: SystemStatus | None = None
    board_tracker: BoardTracker | None = None
    outreach_settings: OutreachSettings | None = None
    document_pdf_renderer: Callable[[str], bytes] | None = None
    outreach_contact_client_factory: Callable[[], SafeFetcher] | None = None
    outreach_contact_delay: float = 1.0
    outreach_smtp_verifier_factory: Callable[[], Any] | None = None
    outreach_renderer_factory: Callable[[], Any] | None = None
    outreach_draft_provider: str | None = None
    outreach_provider_factory: Callable[[str, str], AgentProvider] | None = None
    outreach_gmail_client_factory: Callable[[], httpx.Client] | None = None
    outreach_form_submitter_factory: Callable[..., Any] | None = None
    apply_agent_factory: Any = None
    apply_schema_client_factory: Callable[[], SchemaClient] | None = None
    call_prep_worker: CallPrepWorker | None = None
    start_call_prep_worker: bool | None = None
    start_inbox_watcher: bool | None = None
    start_automation_worker: bool | None = None
    typesafe_client_factory: Callable[[], DecisionClient] | None = None
    inbox_client_factory: Callable[[], DecisionClient | None] | None = None
    profile_file: Path | None = None
    early_programs_file: Path | None = None
    allowed_hosts: list[str] | None = None
    recovery_sandbox: bool = False


@dataclass(frozen=True)
class AppConfig:
    """Settings resolved once when the app is built."""

    database_target: Path | str
    static_dir: Path
    resume_storage: Path
    capture_storage: Path
    interview_storage: Path
    apply_storage: Path | None
    access_token: str
    employer_token: str
    admin_token: str
    expected_session: str
    csrf_token: str
    rate_limit_per_minute: int
    profile_file: Path | None
    early_programs_file: Path | None
    recovery_sandbox: bool
    outreach_draft_provider: str | None
    outreach_contact_delay: float
    # True only for the real product database: it gates every default that reaches the network, a browser or a personal file.
    real_product_db: bool
    start_call_prep_worker: bool
    start_inbox_watcher: bool
    start_automation_worker: bool

    def user_csrf_token(self, user_session: str) -> str:
        # Bound to the student's own session, so one student's CSRF value is
        # useless against another session.
        return hmac.new(
            self.access_token.encode(),
            b"pipeline-user-csrf-v1:" + user_session.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def database_present(self) -> bool:
        """True when there is a database to open: a PostgreSQL target, or a SQLite file that exists."""
        return is_postgres_target(self.database_target) or Path(self.database_target).exists()


@dataclass(frozen=True)
class AppServices:
    """The collaborators routes call: the caller's override, or the production default."""

    agent_provider_factory: Callable[[str, str], AgentProvider]
    typesafe_client_factory: Callable[[], DecisionClient]
    inbox_client_factory: Callable[[], DecisionClient | None]
    outreach_provider_factory: Callable[[str, str], AgentProvider]
    contact_client_factory: Callable[[], SafeFetcher]
    smtp_verifier_factory: Callable[[], Any]
    renderer_factory: Callable[[], Any]
    form_submitter_factory: Callable[..., Any] | None
    gmail_client_factory: Callable[[], httpx.Client]
    apply_schema_client_factory: Callable[[], SchemaClient] | None
    apply_agent_factory: Any
    pdf_renderer: Callable[[str], bytes] | None
    refresh_manager: RefreshManager | None
    outreach_discovery_manager: DiscoveryManager | None
    outreach_recontact_manager: RecontactManager | None
    system_status: SystemStatus | None
    board_tracker: BoardTracker | None
    outreach_settings: OutreachSettings | None
    call_prep_worker: CallPrepWorker
    inbox_watcher: InboxWatcher
    automation_worker: AutomationWorker
    # Why company research cannot run on this computer right now, or "" when it can.
    research_problem: Callable[[], str]
    # A reply found in Gmail starts call prep just as a pasted one does.
    prep_after_reply: Callable[[sqlite3.Connection, str, str], None]


def _new_metrics() -> dict[str, Any]:
    return {
        "requests": 0,
        "errors": 0,
        "rate_limited": 0,
        "latency_ms_total": 0.0,
        "read_latency_ms": deque(maxlen=1_000),
        "write_latency_ms": deque(maxlen=1_000),
    }


@dataclass
class AppRuntime:
    """State that changes while the app runs. Per app, never shared."""

    # One-time sign-in tickets minted by the local launcher (launch.py), kept
    # only as hashes with their expiry. See create_launch_ticket.
    launch_tickets: dict[str, float] = field(default_factory=dict)
    launch_tickets_lock: threading.Lock = field(default_factory=threading.Lock)
    rate_windows: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    metrics: dict[str, Any] = field(default_factory=_new_metrics)
    traces: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))
    open_connections: set[Any] = field(default_factory=set)
    # name -> ((mtime_ns, size), content hash): see web/assets.py.
    asset_versions: dict[str, tuple[tuple[int, int], str]] = field(default_factory=dict)
    # (static_dir resolved, its mtime_ns, the names it lists, name -> resolves inside it): see web/assets.py.
    asset_listing: tuple[Any, int, frozenset[str], dict[str, bool]] | None = None
    apply_schema_cache: apply_preflight.SchemaCache = field(default_factory=apply_preflight.SchemaCache)


@dataclass(frozen=True)
class AppContext:
    config: AppConfig
    services: AppServices
    runtime: AppRuntime


def build_context(options: AppOptions) -> AppContext:
    """Resolve the options and the production-only defaults into one context. Reads .env, as create_app always did."""

    db_path = options.db_path
    static_dir = options.static_dir
    resume_storage = options.resume_storage
    capture_storage = options.capture_storage
    interview_storage = options.interview_storage
    apply_storage = options.apply_storage
    refresh_manager = options.refresh_manager
    outreach_discovery_manager = options.outreach_discovery_manager
    outreach_recontact_manager = options.outreach_recontact_manager
    system_status = options.system_status
    board_tracker = options.board_tracker
    outreach_settings = options.outreach_settings
    call_prep_worker = options.call_prep_worker
    profile_file = options.profile_file
    early_programs_file = options.early_programs_file
    outreach_contact_delay = options.outreach_contact_delay
    outreach_draft_provider = options.outreach_draft_provider
    start_call_prep_worker = options.start_call_prep_worker
    start_inbox_watcher = options.start_inbox_watcher
    start_automation_worker = options.start_automation_worker

    load_env_file()
    environment_database = os.environ.get("DATABASE_URL") if db_path == DEFAULT_PLATFORM_DB else None
    database_target: Path | str = options.database_url or environment_database or db_path
    if not is_postgres_target(database_target):
        database_target = Path(database_target).expanduser().resolve()
    static_dir = static_dir.expanduser().resolve()
    resume_storage = resume_storage.expanduser().resolve()
    capture_storage = capture_storage.expanduser().resolve()
    interview_storage = interview_storage.expanduser().resolve()
    resolved_token = options.access_token or os.environ.get("PIPELINE_WEB_TOKEN") or secrets.token_urlsafe(24)
    resolved_employer_token = options.employer_token or os.environ.get("PIPELINE_EMPLOYER_TOKEN") or secrets.token_urlsafe(24)
    resolved_admin_token = options.admin_token or os.environ.get("PIPELINE_ADMIN_TOKEN") or secrets.token_urlsafe(24)
    expected_session = _session_signature(resolved_token)
    csrf_token = hmac.new(resolved_token.encode(), b"pipeline-csrf-v1", hashlib.sha256).hexdigest()
    runtime = AppRuntime()
    resolved_agent_provider_factory = options.agent_provider_factory or build_provider
    resolved_typesafe_client_factory = options.typesafe_client_factory or build_typesafe_client
    resolved_inbox_client_factory = options.inbox_client_factory or build_inbox_client
    # A manual refresh fetches live sources and rewrites data/pipeline.db, so it
    # is only wired up for the real product database. Test and sandbox apps built
    # over a temporary database get it only when they pass a manager explicitly.
    if refresh_manager is None and not is_postgres_target(database_target) and database_target == DEFAULT_PLATFORM_DB.resolve():
        refresh_manager = RefreshManager(database_target)
    # The deep search browses the web and spends model quota, so like the
    # refresh it is wired up only for the real product database.
    resolved_outreach_provider_factory = options.outreach_provider_factory or resolved_agent_provider_factory
    # Owner profile edits are written back to config/profile.json, which
    # pipeline.py scores from, but only for the real product database.
    if profile_file is None and not is_postgres_target(database_target) and database_target == DEFAULT_PLATFORM_DB.resolve():
        profile_file = DEFAULT_PROFILE
    # The early-program list is a private file beside the profile, read
    # for the real product database only; tests and sandboxes pass their own.
    if early_programs_file is None and not is_postgres_target(database_target) and database_target == DEFAULT_PLATFORM_DB.resolve():
        early_programs_file = DEFAULT_EARLY_PROGRAMS
    if outreach_discovery_manager is None and not is_postgres_target(database_target) and database_target == DEFAULT_PLATFORM_DB.resolve():
        outreach_discovery_manager = DiscoveryManager(
            database_target, provider_factory=resolved_outreach_provider_factory,
            verifier_factory=default_smtp_verifier, email_search=True, draft_provider=outreach_draft_provider,
        )
    resolved_contact_client_factory = options.outreach_contact_client_factory or default_contact_fetcher
    # Asking mail servers about guesses and rendering pages in a browser both
    # reach past the company's plain HTML, so like the refresh they are wired
    # up by default only for the real product database.
    real_product_db = not is_postgres_target(database_target) and database_target == DEFAULT_PLATFORM_DB.resolve()
    # Apply for me's screenshots (data/private/apply). Only the real app has any, so a test or sandbox that does not
    # name a folder never retention-purges or deletes from the real one.
    if apply_storage is None and real_product_db:
        apply_storage = APPLY_ROOT
    if apply_storage is not None:
        apply_storage = apply_storage.expanduser().resolve()
    resolved_smtp_verifier_factory = options.outreach_smtp_verifier_factory or (default_smtp_verifier if real_product_db else (lambda: None))
    resolved_renderer_factory = options.outreach_renderer_factory or (default_renderer if real_product_db else (lambda: None))
    # Contact forms are sent from a real browser, so only the real app opens one.
    resolved_form_submitter_factory = options.outreach_form_submitter_factory or (default_form_submitter_factory if real_product_db else None)
    # Apply for me reads Greenhouse's public listing and opens a browser, so like contact forms it is wired only for
    # the real app. A sandbox or a test may opt in with fakes (no network, no browser); without them the check and
    # the start routes answer 503 at once, which is also what the fuzzer sees.
    resolved_apply_schema_client_factory = options.apply_schema_client_factory or (default_schema_client_factory if real_product_db else None)
    resolved_apply_agent_factory = options.apply_agent_factory or (apply_runs.PlaywrightProbe() if real_product_db else None)
    apply_runs.configure_agent_factory(resolved_apply_agent_factory)
    # The status panel reads this machine's scheduler, daily-run state and
    # data/pipeline.db, which only describe the real product database.
    if system_status is None and real_product_db:
        system_status = SystemStatus()
    # Adding a board writes config/sources.local.json, which the daily run reads.
    if board_tracker is None and real_product_db:
        board_tracker = BoardTracker()
    # Outreach settings are written to this machine's .env, so only the real
    # product database edits them; a scratch app would rewrite the student's file.
    if outreach_settings is None and real_product_db:
        outreach_settings = OutreachSettings(resume_storage=resume_storage)
    # None when Playwright is not installed; documents then download as Markdown only.
    resolved_pdf_renderer = options.document_pdf_renderer or pdf_renderer()
    # Like the deep search, looking again for people browses the web and spends
    # model quota, so it is wired up by default only for the real product database.
    if outreach_recontact_manager is None and real_product_db:
        outreach_recontact_manager = RecontactManager(
            database_target, client_factory=resolved_contact_client_factory,
            renderer_factory=resolved_renderer_factory, verifier_factory=resolved_smtp_verifier_factory,
            provider_factory=resolved_outreach_provider_factory, draft_provider=outreach_draft_provider,
            contact_delay=outreach_contact_delay,
        )

    # Call prep is written by a background thread from durable jobs, so it
    # survives a restart or a sleeping laptop. Tests run the jobs themselves.
    # Researching a company browses the web and spends the research agent's
    # quota, so like the deep search it is wired up only for the real database.
    if call_prep_worker is None:
        call_prep_worker = CallPrepWorker(
            database_target, provider_factory=resolved_outreach_provider_factory, provider=outreach_draft_provider,
            researcher=web_researcher(
                resolved_contact_client_factory, resolved_renderer_factory, resolved_outreach_provider_factory, outreach_draft_provider,
            ) if real_product_db else None,
            interviewer=web_interviewer(resolved_outreach_provider_factory, outreach_draft_provider) if real_product_db else None,
        )
    if start_call_prep_worker is None:
        start_call_prep_worker = real_product_db

    def research_problem() -> str:
        """Why company research cannot run on this computer right now, or "" when it can.

        The researcher says (outreach_research.web_researcher's ``problem``): with
        no research CLI installed, offering it would queue a job that can only fail.
        """
        return call_prep_worker.research_problem() if call_prep_worker.can_research else ""

    resolved_gmail_client_factory = options.outreach_gmail_client_factory or default_gmail_client_factory

    def prep_after_reply(conn: sqlite3.Connection, target_id: str, user_id: str) -> None:
        # A reply found in Gmail starts call prep just as a pasted one does.
        if auto_queue_call_prep(conn, target_id, user_id=user_id, reason="Reply found in Gmail"):
            call_prep_worker.wake()

    def inbox_decisions_for(conn: sqlite3.Connection, user_id: str) -> Any:
        return inbox_client_for(conn, resolved_inbox_client_factory, user_id=user_id)

    # Bounces and replies are read from Gmail in the background, so they land
    # even while the page is closed. Tests and sandboxes run the checks themselves.
    inbox_watcher = InboxWatcher(
        database_target, client_factory=resolved_gmail_client_factory,
        decisions_for=inbox_decisions_for, on_reply=prep_after_reply,
    )
    if start_inbox_watcher is None:
        start_inbox_watcher = real_product_db
    # Drafts and contact searches the student switched on, off the request path.
    automation_worker = AutomationWorker(
        database_target, fetcher_factory=resolved_contact_client_factory, renderer_factory=resolved_renderer_factory,
        verifier_factory=resolved_smtp_verifier_factory, provider_factory=resolved_outreach_provider_factory,
        draft_provider=outreach_draft_provider, contact_delay=outreach_contact_delay,
        gmail_client_factory=resolved_gmail_client_factory, form_submitter_factory=resolved_form_submitter_factory,
        # A reply auto-close's fresh look finds is handled as the InboxWatcher handles one.
        decisions_for=inbox_decisions_for, on_reply=prep_after_reply, apply_root=apply_storage,
    )
    if start_automation_worker is None:
        start_automation_worker = real_product_db

    return AppContext(
        config=AppConfig(
            database_target=database_target,
            static_dir=static_dir,
            resume_storage=resume_storage,
            capture_storage=capture_storage,
            interview_storage=interview_storage,
            apply_storage=apply_storage,
            access_token=resolved_token,
            employer_token=resolved_employer_token,
            admin_token=resolved_admin_token,
            expected_session=expected_session,
            csrf_token=csrf_token,
            rate_limit_per_minute=options.rate_limit_per_minute,
            profile_file=profile_file,
            early_programs_file=early_programs_file,
            recovery_sandbox=options.recovery_sandbox,
            outreach_draft_provider=outreach_draft_provider,
            outreach_contact_delay=outreach_contact_delay,
            real_product_db=real_product_db,
            start_call_prep_worker=start_call_prep_worker,
            start_inbox_watcher=start_inbox_watcher,
            start_automation_worker=start_automation_worker,
        ),
        services=AppServices(
            agent_provider_factory=resolved_agent_provider_factory,
            typesafe_client_factory=resolved_typesafe_client_factory,
            inbox_client_factory=resolved_inbox_client_factory,
            outreach_provider_factory=resolved_outreach_provider_factory,
            contact_client_factory=resolved_contact_client_factory,
            smtp_verifier_factory=resolved_smtp_verifier_factory,
            renderer_factory=resolved_renderer_factory,
            form_submitter_factory=resolved_form_submitter_factory,
            gmail_client_factory=resolved_gmail_client_factory,
            apply_schema_client_factory=resolved_apply_schema_client_factory,
            apply_agent_factory=resolved_apply_agent_factory,
            pdf_renderer=resolved_pdf_renderer,
            refresh_manager=refresh_manager,
            outreach_discovery_manager=outreach_discovery_manager,
            outreach_recontact_manager=outreach_recontact_manager,
            system_status=system_status,
            board_tracker=board_tracker,
            outreach_settings=outreach_settings,
            call_prep_worker=call_prep_worker,
            inbox_watcher=inbox_watcher,
            automation_worker=automation_worker,
            research_problem=research_problem,
            prep_after_reply=prep_after_reply,
        ),
        runtime=runtime,
    )
