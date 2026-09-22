"""Role-scoped API and web surfaces for the opportunity platform."""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import ExitStack, asynccontextmanager, closing
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

import httpx
import uvicorn
from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from pipeline import load_env_file
from pipeline_core import OpportunityFilters, OpportunityRepository

from . import DEFAULT_PLATFORM_DB, DEFAULT_PROFILE, STATIC_DIR
from .actions import (
    APPLICATION_STAGES,
    ApplicationNotFoundError,
    OpportunityNotFoundError,
    add_application_contact,
    add_application_task,
    application_analytics,
    application_detail,
    import_applications,
    list_applications,
    record_intent,
    update_application,
    update_application_task,
)
from .early_programs import (
    DEFAULT_EARLY_PROGRAMS,
    EarlyProgramNotFoundError,
    early_programs,
    set_program_status,
)
from .urgent import (
    DeadlineNotFoundError,
    clear_user_deadline,
    set_user_deadline,
    urgent_queue,
    user_deadline,
    user_deadlines_for,
    visible_opportunity,
)
from .auth import (
    authenticate_email_password,
    authenticate_password,
    complete_recovery,
    constant_time_equal,
    feature_flag_enabled,
    issue_user_token,
    register_owner,
    register_student,
    request_recovery,
    resolve_user_token,
    revoke_user_token,
)
from .document_artifacts import delete_document_artifact, ensure_document_artifact
from .extension_apply import (
    ExtensionApplyError,
    ExtensionAuthError,
    answer_is_sensitive,
    application_candidates,
    apply_context,
    artifact_path as extension_artifact_path,
    confirm_submitted as confirm_extension_submitted,
    create_pairing,
    list_devices as list_extension_devices,
    redeem_pairing,
    resolve_extension_token,
    revoke_device as revoke_extension_device,
    sync_session as sync_extension_session,
    sync_step as sync_extension_step,
)
from .captures import (
    DEFAULT_CAPTURE_STORAGE,
    MAX_CAPTURE_BYTES,
    CaptureNotFoundError,
    CaptureValidationError,
    capture_file as store_capture_file,
    capture_url,
    confirm_capture,
    get_capture,
)
from .connections import (
    ConnectionNotFoundError,
    begin_oauth,
    complete_oauth,
    apply_channel_opt_out,
    confirm_phone,
    connect_provider,
    decide_monitored_event,
    disconnect_provider,
    ensure_preferences,
    ingest_message,
    list_connectors,
    list_monitored_events,
    queue_notification,
    request_phone_verification,
    update_preferences,
)
from .profile import get_profile, is_personalized, update_profile
from .dossier import (
    DossierNotFoundError,
    create_share,
    delete_all as delete_dossier_all,
    delete_item as delete_dossier_item,
    dossier,
    read_share,
    revoke_share,
    save_item as save_dossier_item,
    share_preview,
    update_settings as update_dossier_settings,
)
from .preparation import (
    DEFAULT_MOCK_AUDIO_STORAGE,
    MAX_MOCK_AUDIO_BYTES,
    PreparationNotFoundError,
    answer_mock_question,
    approve_document,
    create_document,
    create_mock_interview,
    delete_answer,
    document_record,
    edit_document,
    interview_record,
    list_interviews as list_mock_interviews,
    list_answers,
    list_documents,
    recorded_mock_answer_path,
    save_answer,
    store_recorded_mock_answer,
)
from .market import (
    MarketNotFoundError,
    create_issue,
    create_snapshot,
    issue_record,
    public_issues,
    publish_issue,
    verify_snapshot,
)
from .employer import (
    EmployerNotFoundError,
    add_candidate_from_share,
    admin_overview,
    approve_candidate_message,
    candidate_record,
    confirm_interview,
    create_moderation_item,
    create_organization,
    create_requisition,
    decide_candidate,
    draft_candidate_message,
    ensure_actor,
    import_requisitions,
    # employer.interview_record intentionally not imported: it would shadow
    # preparation.interview_record used by student mock-interview routes.
    list_candidates,
    list_requisitions,
    propose_interview,
    requisition_record,
    resolve_moderation_item,
    school_aggregate,
    set_feature_flag,
    set_source_control,
    verify_organization,
)
from .resumes import (
    DEFAULT_STORAGE,
    MAX_RESUME_BYTES,
    ResumeNotFoundError,
    ResumeValidationError,
    confirm_resume,
    delete_resume,
    list_resumes,
    resume_file_path,
    resume_record,
    store_resume,
)
from .refresh import RefreshBusy, RefreshManager, fresh_steps
from .schema import LOCAL_USER_ID, connect_product, ensure_product_schema, utc_now
from .database import is_postgres_target
from .student_agent import (
    AgentNotFoundError,
    activity_feed,
    cancel_thread,
    create_thread,
    decide_proposal,
    list_threads,
    post_message,
    thread_record,
)
from .agent_providers import AgentProvider, build_provider, default_provider, provider_catalog
from .typesafe_decisions import (
    DEFAULT_MODEL as TYPESAFE_DEFAULT_MODEL,
    DecisionClient,
    QUESTION_SET_VERSION,
    TypeSafeError,
    TypeSafeNotConfigured,
    build_client as build_typesafe_client,
    review_opportunity as review_opportunity_with_typesafe,
)
from .outreach import (
    CONTACT_CONFIDENCE,
    DraftChangedError,
    LocationConflictError,
    OUTREACH_PRIORITIES,
    OUTREACH_STATUSES,
    OutreachNotFoundError,
    approve_draft as approve_outreach_draft,
    confirm_research as confirm_outreach_research,
    create_target as create_outreach_target,
    delete_target as delete_outreach_target,
    export_csv as export_outreach_csv,
    get_target as get_outreach_target,
    import_targets as import_outreach_targets,
    list_targets as list_outreach_targets,
    log_reply as log_outreach_reply,
    outreach_summary,
    parse_import as parse_outreach_import,
    update_target as update_outreach_target,
)
from .outreach_contacts import (
    SafeFetcher,
    apply_candidate as apply_outreach_candidate,
    default_fetcher as default_contact_fetcher,
    find_contacts as find_outreach_contacts,
    list_candidates as list_outreach_candidates,
)
from .outreach_render import default_renderer
from .outreach_smtp import default_verifier as default_smtp_verifier
from .outreach_discovery import scope_definitions as discovery_scope_definitions, DiscoveryBusy, DiscoveryManager, last_runs as last_discovery_runs
from .outreach_recontact import RecontactBusy, RecontactManager, eligible_targets as recontact_eligible_targets
from .system_status import SystemStatus
from .boards import BoardLookupExpired, BoardTracker
from .outreach_settings import OutreachSettings
from .document_pdf import markdown_to_html, pdf_renderer
from .outreach_drafting import (
    MAX_COMMENT_CHARS as MAX_DRAFT_COMMENT_CHARS,
    DraftVersionNotFoundError,
    draft_versions as outreach_draft_versions,
    generate_draft as generate_outreach_draft,
    restore_draft_version as restore_outreach_draft_version,
    sender_account,
)
from .outreach_gmail import GmailAuthError, create_gmail_draft, default_client_factory as default_gmail_client_factory, gmail_drafts_status
from .operations import (
    OperationsError,
    delete_account,
    enqueue_job,
    export_account,
    queue_status,
    retry_dead_job,
    run_retention,
)
from .notifications import build_provider as build_notification_provider
from .notifications import connector_health


_ASSET_REFERENCE = re.compile(r"""/assets/([A-Za-z0-9._-]+)(?:\?v=[^"']*)?""")

LOGGER = logging.getLogger("opportunity_app")
SESSION_COOKIE = "pipeline_session"
# Students sign in to the browser with their own per-user token. It lives in a
# separate cookie so the owner-scoped session cookie is never issued to them.
USER_SESSION_COOKIE = "pipeline_user_session"
# A launcher ticket is exchanged within seconds of being minted; the session it
# opens is remembered on this computer for 30 days.
LAUNCH_TICKET_SECONDS = 60
LAUNCH_SESSION_SECONDS = 60 * 60 * 24 * 30
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


class SessionRequest(BaseModel):
    token: str | None = Field(default=None, min_length=1, max_length=512)
    email: str | None = Field(default=None, max_length=320)
    password: str | None = Field(default=None, max_length=512)
    launch_ticket: str | None = Field(default=None, min_length=1, max_length=128)


class LaunchTicketResponse(BaseModel):
    ticket: str
    expires_in: int


class SessionResponse(BaseModel):
    authenticated: bool
    user_id: str
    display_name: str
    api_token: str | None = None


class RefreshStepResponse(BaseModel):
    key: str
    label: str
    state: Literal["pending", "running", "done", "failed", "skipped"]
    done: int
    total: int
    detail: str


class RefreshStatusResponse(BaseModel):
    available: bool
    state: Literal["idle", "running", "succeeded", "failed"]
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    steps: list[RefreshStepResponse]


class RegistrationRequest(BaseModel):
    invite_token: str | None = Field(default=None, min_length=1, max_length=512)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=512)
    display_name: str = Field(min_length=1, max_length=200)


class RecoveryRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class RecoveryCompleteRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=200)
    code: str = Field(pattern=r"^\d{6}$")
    new_password: str = Field(min_length=12, max_length=512)


class OpportunityListResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
    sort: str
    # False while the caller's profile has no scoring inputs, so every score is
    # the base score and no match reasons exist yet.
    personalized: bool = True


class IntentRequest(BaseModel):
    action: Literal["seen", "saved", "passed", "apply_opened", "undo"]


class ApplicationUpdateRequest(BaseModel):
    stage: Literal["applying", "applied", "interview", "offer", "rejected", "withdrawn", "archived"] | None = None
    notes: str | None = Field(default=None, max_length=10_000)
    follow_up_at: str | None = Field(default=None, max_length=80)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)


class OutreachTargetRequest(BaseModel):
    company: str | None = Field(default=None, max_length=200)
    channel: str | None = Field(default=None, max_length=100)
    priority: Literal["P1", "P2", "P3"] | None = None
    website: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=5_000)
    fit_rationale: str | None = Field(default=None, max_length=5_000)
    activity_signal: str | None = Field(default=None, max_length=5_000)
    contact_name: str | None = Field(default=None, max_length=500)
    contact_role: str | None = Field(default=None, max_length=500)
    contact_email: str | None = Field(default=None, max_length=320)
    contact_cc: str | None = Field(default=None, max_length=320)
    contact_linkedin: str | None = Field(default=None, max_length=500)
    contact_route: str | None = Field(default=None, max_length=2_000)
    contact_confidence: Literal["confirmed", "unverified", "unknown"] | None = None
    status: Literal[
        "not_started", "drafted", "sent", "followed_up", "replied",
        "call_scheduled", "offer", "declined", "no_response", "paused",
    ] | None = None
    deadline_label: str | None = Field(default=None, max_length=200)
    deadline_date: str | None = Field(default=None, max_length=10)
    email_subject: str | None = Field(default=None, max_length=300)
    email_body: str | None = Field(default=None, max_length=20_000)
    sent_at: str | None = Field(default=None, max_length=10)
    follow_up_at: str | None = Field(default=None, max_length=10)
    notes: str | None = Field(default=None, max_length=10_000)
    source_urls: list[str] | None = Field(default=None, max_length=50)
    researched_at: str | None = Field(default=None, max_length=10)
    follow_up_subject: str | None = Field(default=None, max_length=300)
    follow_up_body: str | None = Field(default=None, max_length=20_000)
    contact_evidence_url: str | None = Field(default=None, max_length=500)
    # PATCH only: the student vouches for a location nothing has checked yet.
    # It carries the location the page showed, so a place that changed between
    # the render and the click is refused instead of silently confirmed. A bare
    # bool still parses, so the refusal is an explanatory 422 from the tracker
    # rather than a shapeless validation error; the length bound belongs to the
    # string arm, because on the union it is applied to a bool too and raises.
    confirm_location: Annotated[str, Field(max_length=200)] | bool | None = None


class OutreachDraftRequest(BaseModel):
    kind: Literal["initial", "follow_up"] = "initial"
    comments: str = Field(default="", max_length=MAX_DRAFT_COMMENT_CHARS)


class OutreachApprovalRequest(BaseModel):
    kind: Literal["initial", "follow_up"] = "initial"
    acknowledge_warnings: bool = False
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class OutreachReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class OutreachDiscoveryRequest(BaseModel):
    scopes: list[Literal["local-accelerators", "us-startups", "recently-funded"]] | None = Field(default=None, max_length=3)


class OutreachSettingsRequest(BaseModel):
    draft_provider: str | None = Field(default=None, max_length=40)
    research_agent: str | None = Field(default=None, max_length=40)
    attachment_resume_id: str | None = Field(default=None, max_length=100)


class BoardLookupRequest(BaseModel):
    company: str = Field(min_length=1, max_length=120)


class BoardAddRequest(BaseModel):
    lookup_id: str = Field(min_length=1, max_length=64)
    student_confirmed: bool = False


class RecontactChoice(BaseModel):
    target_id: str = Field(min_length=1, max_length=100)
    to: str = Field(min_length=3, max_length=320)


class RecontactApplyRequest(BaseModel):
    choices: list[RecontactChoice] = Field(min_length=1, max_length=500)
    redraft: bool = False


class ContactCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=80)


class TaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    due_at: str | None = Field(default=None, max_length=80)
    # The browser's IANA zone, so a datetime-local value is stored as an instant.
    timezone: str | None = Field(default=None, min_length=1, max_length=100)


class DeadlineRequest(BaseModel):
    deadline_on: str = Field(min_length=10, max_length=10)
    note: str = Field(default="", max_length=200)


class EarlyProgramStatusRequest(BaseModel):
    status: Literal["todo", "applied", "skipped"]


class TaskUpdateRequest(BaseModel):
    status: Literal["open", "done"]


class ProfileUpdateRequest(BaseModel):
    updates: dict[str, Any]
    confirmed_fields: list[str] = Field(default_factory=list, max_length=100)


class ResumeConfirmRequest(BaseModel):
    confirmed_data: dict[str, Any] = Field(default_factory=dict)
    profile_updates: dict[str, Any] = Field(default_factory=dict)
    confirmed_profile_fields: list[str] = Field(default_factory=list, max_length=100)


class CaptureUrlRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2_000)


class CaptureConfirmRequest(BaseModel):
    company: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=8, max_length=2_000)
    location: str = Field(default="", max_length=500)
    role_type: str = Field(default="other", max_length=80)
    description: str = Field(default="", max_length=200_000)


class DocumentCreateRequest(BaseModel):
    opportunity_id: str = Field(min_length=1, max_length=500)
    document_type: Literal["resume", "cover_letter"]
    provider: Literal["openai", "anthropic"] | None = None


class DocumentEditRequest(BaseModel):
    content: str = Field(min_length=1, max_length=500_000)
    evidence_fields: list[str] = Field(min_length=1, max_length=100)


class AnswerSaveRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2_000)
    answer: str = Field(min_length=1, max_length=50_000)
    company: str = Field(default="", max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=100)


class InterviewCreateRequest(BaseModel):
    opportunity_id: str = Field(min_length=1, max_length=500)


class MockAnswerRequest(BaseModel):
    answer_text: str = Field(default="", max_length=100_000)
    transcript: str = Field(default="", max_length=100_000)


class AgentThreadRequest(BaseModel):
    title: str = Field(default="Career planning", max_length=200)
    provider: Literal["openai", "anthropic", "claude-code", "codex-cli", "legacy"] | None = None


class AgentMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class AgentDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]


class ApplySessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    application_id: str | None = Field(default=None, max_length=500)
    page_url: str = Field(min_length=8, max_length=2_000)
    ats_type: str = Field(default="generic", max_length=100)
    fields: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    status: Literal["draft", "reviewed", "completed"] = "draft"


class ExtensionPairingRedeemRequest(BaseModel):
    code: str = Field(min_length=20, max_length=200)
    device_name: str = Field(default="Chrome Apply Mode", max_length=120)


class ExtensionSessionRequest(BaseModel):
    application_id: str | None = Field(default=None, max_length=500)
    page_url: str = Field(min_length=8, max_length=2_000)
    ats_type: str = Field(default="generic", max_length=100)
    fields: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    status: Literal["draft", "reviewed", "completed"] = "draft"


class ExtensionStepRequest(BaseModel):
    page_url: str = Field(min_length=8, max_length=2_000)
    ats_type: str = Field(default="generic", max_length=100)
    fields: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    summary: dict[str, int] = Field(default_factory=dict)
    status: Literal["scanned", "reviewed", "filled", "manual", "completed"] = "scanned"


class ExtensionAnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2_000)
    answer: str = Field(min_length=1, max_length=50_000)
    company: str = Field(default="", max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=100)


class ConnectorRequest(BaseModel):
    provider: Literal["sandbox", "google", "microsoft"] = "sandbox"


class OAuthCompleteRequest(BaseModel):
    state: str = Field(min_length=20, max_length=500)
    code: str = Field(min_length=1, max_length=4_000)


class MonitoredMessageRequest(BaseModel):
    connector_id: str = Field(min_length=1, max_length=500)
    external_id: str = Field(min_length=1, max_length=500)
    subject: str = Field(default="", max_length=2_000)
    body: str = Field(default="", max_length=100_000)
    sender: str = Field(default="", max_length=500)


class MonitoredDecisionRequest(BaseModel):
    decision: Literal["confirm", "ignore"]
    application_id: str | None = Field(default=None, max_length=500)


class NotificationPreferencesRequest(BaseModel):
    updates: dict[str, Any]


class NotificationOptOutRequest(BaseModel):
    channel: Literal["email", "push", "sms", "voice"]
    keyword: str = Field(min_length=3, max_length=20)


class PhoneRequest(BaseModel):
    phone_e164: str = Field(min_length=8, max_length=20)


class PhoneConfirmRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=6, max_length=6)


class DossierSettingsRequest(BaseModel):
    paused: bool
    retention_days: int = Field(ge=1, le=3650)


class DossierItemRequest(BaseModel):
    item_type: Literal["user_opinion", "deterministic_analysis", "ai_suggestion"]
    field_path: str = Field(min_length=1, max_length=500)
    value: Any
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class DossierShareRequest(BaseModel):
    recipient: str = Field(min_length=1, max_length=500)
    item_ids: list[str] = Field(min_length=1, max_length=200)
    expires_in_days: int = Field(default=30, ge=1, le=365)


class DossierPreviewRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=200)


class MarketSnapshotRequest(BaseModel):
    as_of: str | None = Field(default=None, max_length=80)


class MarketIssueRequest(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=200)


class OrganizationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    organization_type: Literal["employer", "school"] = "employer"


class RequisitionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=200_000)
    rubric: list[dict[str, Any]] = Field(min_length=1, max_length=100)


class CandidateShareRequest(BaseModel):
    share_token: str = Field(min_length=20, max_length=500)


class CandidateDecisionRequest(BaseModel):
    status: Literal["shortlisted", "interview", "offer", "rejected"]
    reason: str = Field(min_length=1, max_length=2_000)


class FeatureFlagRequest(BaseModel):
    enabled: bool
    description: str = Field(default="", max_length=1_000)


class OrganizationVerificationRequest(BaseModel):
    approved: bool


class RequisitionImportRequest(BaseModel):
    organization_id: str = Field(min_length=1, max_length=200)
    requisitions: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class EmployerMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)


class EmployerInterviewRequest(BaseModel):
    starts_at: str = Field(min_length=1, max_length=80)
    timezone: str = Field(min_length=1, max_length=100)
    location: str = Field(default="", max_length=500)


class SourceControlRequest(BaseModel):
    enabled: bool
    moderation_status: Literal["approved", "review", "blocked"]
    note: str = Field(default="", max_length=2_000)


class ModerationCreateRequest(BaseModel):
    target_type: str = Field(min_length=1, max_length=100)
    target_id: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=2_000)


class ModerationResolveRequest(BaseModel):
    status: Literal["resolved", "dismissed"]
    resolution: str = Field(min_length=1, max_length=2_000)


class JobCreateRequest(BaseModel):
    job_type: Literal[
        "retention", "connector_health", "notification_digest", "reminder_dispatch",
        "pipeline_fetch", "pipeline_enrich", "pipeline_score", "pipeline_liveness", "pipeline_report",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=500)
    max_attempts: int = Field(default=3, ge=1, le=20)


def _session_signature(access_token: str) -> str:
    return hmac.new(
        access_token.encode("utf-8"),
        b"pipeline-local-session-v1",
        hashlib.sha256,
    ).hexdigest()


def _bearer_value(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value


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


def create_app(
    *,
    db_path: Path = DEFAULT_PLATFORM_DB,
    access_token: str | None = None,
    static_dir: Path = STATIC_DIR,
    resume_storage: Path = DEFAULT_STORAGE,
    capture_storage: Path = DEFAULT_CAPTURE_STORAGE,
    interview_storage: Path = DEFAULT_MOCK_AUDIO_STORAGE,
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
    typesafe_client_factory: Callable[[], DecisionClient] | None = None,
    profile_file: Path | None = None,
    early_programs_file: Path | None = None,
    allowed_hosts: list[str] | None = None,
    recovery_sandbox: bool = False,
) -> FastAPI:
    """Build an isolated app instance for production and tests."""

    load_env_file()
    environment_database = os.environ.get("DATABASE_URL") if db_path == DEFAULT_PLATFORM_DB else None
    database_target: Path | str = database_url or environment_database or db_path
    if not is_postgres_target(database_target):
        database_target = Path(database_target).expanduser().resolve()
    static_dir = static_dir.expanduser().resolve()
    resume_storage = resume_storage.expanduser().resolve()
    capture_storage = capture_storage.expanduser().resolve()
    interview_storage = interview_storage.expanduser().resolve()
    resolved_token = access_token or os.environ.get("PIPELINE_WEB_TOKEN") or secrets.token_urlsafe(24)
    resolved_employer_token = employer_token or os.environ.get("PIPELINE_EMPLOYER_TOKEN") or secrets.token_urlsafe(24)
    resolved_admin_token = admin_token or os.environ.get("PIPELINE_ADMIN_TOKEN") or secrets.token_urlsafe(24)
    expected_session = _session_signature(resolved_token)
    # One-time sign-in tickets minted by the local launcher (launch.py), kept
    # only as hashes with their expiry. See create_launch_ticket.
    launch_tickets: dict[str, float] = {}
    launch_tickets_lock = threading.Lock()
    csrf_token = hmac.new(resolved_token.encode(), b"pipeline-csrf-v1", hashlib.sha256).hexdigest()

    def user_csrf_token(user_session: str) -> str:
        # Bound to the student's own session, so one student's CSRF value is
        # useless against another session.
        return hmac.new(
            resolved_token.encode(),
            b"pipeline-user-csrf-v1:" + user_session.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    rate_windows: dict[str, deque[float]] = defaultdict(deque)
    app_metrics = {
        "requests": 0,
        "errors": 0,
        "rate_limited": 0,
        "latency_ms_total": 0.0,
        "read_latency_ms": deque(maxlen=1_000),
        "write_latency_ms": deque(maxlen=1_000),
    }
    recent_traces: deque[dict[str, Any]] = deque(maxlen=200)
    open_connections: set[Any] = set()
    resolved_agent_provider_factory = agent_provider_factory or build_provider
    resolved_typesafe_client_factory = typesafe_client_factory or build_typesafe_client
    # A manual refresh fetches live sources and rewrites data/pipeline.db, so it
    # is only wired up for the real product database. Test and sandbox apps built
    # over a temporary database get it only when they pass a manager explicitly.
    if refresh_manager is None and not is_postgres_target(database_target) and database_target == DEFAULT_PLATFORM_DB.resolve():
        refresh_manager = RefreshManager(database_target)
    # The deep search browses the web and spends model quota, so like the
    # refresh it is wired up only for the real product database.
    resolved_outreach_provider_factory = outreach_provider_factory or resolved_agent_provider_factory
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
            verifier_factory=default_smtp_verifier, email_search=True,
        )
    resolved_contact_client_factory = outreach_contact_client_factory or default_contact_fetcher
    # Asking mail servers about guesses and rendering pages in a browser both
    # reach past the company's plain HTML, so like the refresh they are wired
    # up by default only for the real product database.
    real_product_db = not is_postgres_target(database_target) and database_target == DEFAULT_PLATFORM_DB.resolve()
    resolved_smtp_verifier_factory = outreach_smtp_verifier_factory or (default_smtp_verifier if real_product_db else (lambda: None))
    resolved_renderer_factory = outreach_renderer_factory or (default_renderer if real_product_db else (lambda: None))
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
    resolved_pdf_renderer = document_pdf_renderer or pdf_renderer()
    # Like the deep search, looking again for people browses the web and spends
    # model quota, so it is wired up by default only for the real product database.
    if outreach_recontact_manager is None and real_product_db:
        outreach_recontact_manager = RecontactManager(
            database_target, client_factory=resolved_contact_client_factory,
            renderer_factory=resolved_renderer_factory, verifier_factory=resolved_smtp_verifier_factory,
            provider_factory=resolved_outreach_provider_factory, draft_provider=outreach_draft_provider,
            contact_delay=outreach_contact_delay,
        )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        database_exists = is_postgres_target(database_target) or Path(database_target).exists()
        if not database_exists:
            LOGGER.warning(
                "Product database is missing. Run: python -m opportunity_app.setup init"
            )
        else:
            with closing(connect_product(database_target)) as migration_connection:
                ensure_product_schema(migration_connection)
        LOGGER.warning("Local web access token: %s", application.state.access_token)
        try:
            yield
        finally:
            for connection in list(open_connections):
                try:
                    connection.close()
                finally:
                    open_connections.discard(connection)

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
    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.state.db_path = database_target
    app.state.access_token = resolved_token
    app.state.employer_token = resolved_employer_token
    app.state.admin_token = resolved_admin_token

    # Asset URLs carry a version derived from the file's own bytes. The pages
    # used to hard-code one string ("?v=20260918-outreach-split") that a human
    # had to remember to bump, and three of the four pages carried no version
    # at all -- so styles.css was fetched at two different URLs, one versioned
    # and one not. A content hash cannot go stale: changing a file changes its
    # URL, which is what makes handing out immutable caching safe.
    _asset_versions: dict[str, tuple[tuple[int, int], str]] = {}

    def asset_version(name: str) -> str:
        path = static_dir / name
        try:
            stat = path.stat()
        except OSError:
            return "0"
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = _asset_versions.get(name)
        if cached is not None and cached[0] == signature:
            return cached[1]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        _asset_versions[name] = (signature, digest)
        return digest

    def versioned_page(name: str) -> HTMLResponse:
        """Serve a page with every /assets/ reference version-stamped."""

        html = _ASSET_REFERENCE.sub(
            lambda match: f"/assets/{match.group(1)}?v={asset_version(match.group(1))}",
            (static_dir / name).read_text(encoding="utf-8"),
        )
        return HTMLResponse(html)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
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
                # An asset requested at its current content hash cannot go
                # stale: changing the file changes the URL. Anything else --
                # no version, or an old one -- revalidates as before.
                asset = request.url.path.removeprefix("/assets/")
                if (
                    request.url.path.startswith("/assets/")
                    and asset
                    and request.query_params.get("v") == asset_version(asset)
                ):
                    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                else:
                    response.headers["Cache-Control"] = "no-cache"
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
            recent_traces.append(trace)
            LOGGER.info(json.dumps({"event": "http_request", **trace}))
            return response

        content_length = request.headers.get("Content-Length", "")
        if content_length.isdigit() and int(content_length) > 6 * 1024 * 1024:
            return finish(JSONResponse({"detail": "Request body is too large"}, status_code=413))
        now = time.monotonic()
        client_key = request.client.host if request.client else "unknown"
        window = rate_windows[client_key]
        while window and window[0] <= now - 60:
            window.popleft()
        if request.url.path != "/api/v1/health" and len(window) >= rate_limit_per_minute:
            app_metrics["rate_limited"] += 1
            return finish(JSONResponse({"detail": "Rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"}))
        window.append(now)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/api/v1/session":
            owner_cookie = request.cookies.get(SESSION_COOKIE) == expected_session
            user_session = request.cookies.get(USER_SESSION_COOKIE, "")
            cookie_authenticated = (owner_cookie or bool(user_session)) and not request.headers.get("Authorization")
            expected_csrf = csrf_token if owner_cookie else user_csrf_token(user_session)
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

    def require_auth(
        authorization: Annotated[str | None, Header()] = None,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        user_session_cookie: Annotated[str | None, Cookie(alias=USER_SESSION_COOKIE)] = None,
    ) -> str:
        bearer = _bearer_value(authorization)
        if bearer is not None:
            if constant_time_equal(bearer, resolved_token):
                return LOCAL_USER_ID
            if is_postgres_target(database_target) or Path(database_target).exists():
                with closing(connect_product(database_target, read_only=True)) as conn:
                    resolved_user = resolve_user_token(conn, bearer)
                if resolved_user:
                    return resolved_user
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        cookie_ok = session_cookie is not None and constant_time_equal(
            session_cookie, expected_session
        )
        if not cookie_ok and user_session_cookie:
            if is_postgres_target(database_target) or Path(database_target).exists():
                with closing(connect_product(database_target, read_only=True)) as conn:
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

    def repository(authenticated_user: str = Depends(require_auth)) -> Iterator[OpportunityRepository]:
        try:
            conn = connect_product(database_target, read_only=True)
            open_connections.add(conn)
        except (FileNotFoundError, sqlite3.OperationalError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Product database unavailable; run the migration command first",
            ) from exc
        try:
            yield OpportunityRepository(conn, user_id=authenticated_user)
        finally:
            conn.close()
            open_connections.discard(conn)

    def writable_connection(
        _authenticated_user: str = Depends(require_auth),
    ) -> Iterator[sqlite3.Connection]:
        try:
            conn = connect_product(database_target)
            open_connections.add(conn)
        except sqlite3.OperationalError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Product database unavailable; run the migration command first",
            ) from exc
        try:
            yield conn
        finally:
            conn.close()
            open_connections.discard(conn)

    def require_extension_auth(
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
        if not (is_postgres_target(database_target) or Path(database_target).exists()):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Product database unavailable; run the migration command first",
            )
        with closing(connect_product(database_target)) as conn:
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
    ) -> Iterator[tuple[sqlite3.Connection, dict[str, str]]]:
        try:
            conn = connect_product(database_target)
            open_connections.add(conn)
        except sqlite3.OperationalError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Product database unavailable; run the migration command first",
            ) from exc
        try:
            yield conn, device
        finally:
            conn.close()
            open_connections.discard(conn)

    def require_employer(authorization: Annotated[str | None, Header()] = None) -> str:
        bearer = _bearer_value(authorization)
        if bearer is not None and constant_time_equal(bearer, resolved_employer_token):
            return "employer-user"
        if bearer is not None and (constant_time_equal(bearer, resolved_token) or constant_time_equal(bearer, resolved_admin_token)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Employer role required")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Employer authentication required")

    def require_admin(authorization: Annotated[str | None, Header()] = None) -> str:
        bearer = _bearer_value(authorization)
        if bearer is not None and constant_time_equal(bearer, resolved_admin_token):
            return "admin-user"
        if bearer is not None and (constant_time_equal(bearer, resolved_token) or constant_time_equal(bearer, resolved_employer_token)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required")

    def employer_connection(actor: str = Depends(require_employer)) -> Iterator[tuple[sqlite3.Connection, str]]:
        conn = connect_product(database_target)
        open_connections.add(conn)
        ensure_actor(conn, actor, "employer")
        try:
            yield conn, actor
        finally:
            conn.close()
            open_connections.discard(conn)

    def admin_connection(actor: str = Depends(require_admin)) -> Iterator[tuple[sqlite3.Connection, str]]:
        conn = connect_product(database_target)
        open_connections.add(conn)
        ensure_actor(conn, actor, "admin")
        try:
            yield conn, actor
        finally:
            conn.close()
            open_connections.discard(conn)

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        if not is_postgres_target(database_target) and not Path(database_target).exists():
            return {"ok": False, "database": "missing", "version": app.version}
        try:
            with closing(connect_product(database_target, read_only=True)) as conn:
                conn.execute("SELECT 1 FROM opportunity_read_model LIMIT 1").fetchone()
        except sqlite3.Error:
            return {"ok": False, "database": "invalid", "version": app.version}
        return {"ok": True, "database": "ready", "version": app.version}

    def _display_name(conn_target: str, user_id: str) -> str:
        if is_postgres_target(conn_target) or Path(conn_target).exists():
            with closing(connect_product(conn_target, read_only=True)) as conn:
                row = conn.execute("SELECT display_name FROM users WHERE id=?", (user_id,)).fetchone()
                if row and row[0]:
                    return str(row[0])
        return "Local user" if user_id == LOCAL_USER_ID else "Student"

    @app.post("/api/v1/auth/launch-ticket", response_model=LaunchTicketResponse)
    def create_launch_ticket(request: Request) -> LaunchTicketResponse:
        """Mint a one-time sign-in ticket for the launcher to open the browser with.

        Only the static owner token, sent as a bearer header, may ask: the
        launcher reads it from .env on the same machine. A browser cookie is not
        enough, so a page cannot mint a ticket for itself. The ticket is good
        once, for LAUNCH_TICKET_SECONDS, and is stored only as a hash.
        """
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not constant_time_equal(credential.strip(), resolved_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Owner token required")
        ticket = secrets.token_urlsafe(32)
        now = time.monotonic()
        with launch_tickets_lock:
            for key in [key for key, expiry in launch_tickets.items() if expiry <= now]:
                del launch_tickets[key]
            launch_tickets[hashlib.sha256(ticket.encode()).hexdigest()] = now + LAUNCH_TICKET_SECONDS
        return LaunchTicketResponse(ticket=ticket, expires_in=LAUNCH_TICKET_SECONDS)

    def redeem_launch_ticket(ticket: str) -> bool:
        with launch_tickets_lock:
            expiry = launch_tickets.pop(hashlib.sha256(ticket.encode()).hexdigest(), None)
        return expiry is not None and expiry > time.monotonic()

    @app.post("/api/v1/session", response_model=SessionResponse)
    def create_session(payload: SessionRequest, response: Response) -> SessionResponse:
        token_ok = payload.token is not None and constant_time_equal(payload.token, resolved_token)
        launched = not token_ok and payload.launch_ticket is not None and redeem_launch_ticket(payload.launch_ticket)
        authenticated_user: str | None = LOCAL_USER_ID if token_ok or launched else None
        issued_token: str | None = None
        password_ok = False
        if not token_ok and not launched and payload.email and payload.password and (is_postgres_target(database_target) or Path(database_target).exists()):
            with closing(connect_product(database_target)) as conn:
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
                value=expected_session,
                httponly=True,
                samesite="strict",
                secure=secure,
                max_age=owner_max_age,
                path="/",
            )
            response.set_cookie(
                key="pipeline_csrf", value=csrf_token, httponly=False, samesite="strict",
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
                key="pipeline_csrf", value=user_csrf_token(issued_token), httponly=False, samesite="strict",
                secure=secure, max_age=60 * 60 * 12, path="/",
            )
        return SessionResponse(
            authenticated=True,
            user_id=authenticated_user,
            display_name=_display_name(database_target, authenticated_user),
            api_token=issued_token,
        )

    @app.post("/api/v1/auth/register", status_code=status.HTTP_201_CREATED)
    def register(payload: RegistrationRequest) -> dict[str, Any]:
        invite_present = payload.invite_token is not None
        if invite_present and constant_time_equal(payload.invite_token, resolved_token):
            with closing(connect_product(database_target)) as conn:
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
        if is_postgres_target(database_target) or Path(database_target).exists():
            with closing(connect_product(database_target)) as conn:
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

    @app.post("/api/v1/auth/recovery")
    def begin_recovery(payload: RecoveryRequest) -> dict[str, Any]:
        provider = build_notification_provider()
        deliver_code = None
        if provider.live:
            def deliver_code(recipient: str, code: str) -> dict[str, Any]:
                return provider.deliver("email", recipient, "Your access recovery code", f"Your recovery code is {code}. It expires in 15 minutes.")
        with closing(connect_product(database_target)) as conn:
            return request_recovery(
                conn, payload.email, resolved_token, deliver_code=deliver_code, expose_code=recovery_sandbox
            )

    @app.post("/api/v1/auth/recovery/complete")
    def finish_recovery(payload: RecoveryCompleteRequest) -> dict[str, Any]:
        with closing(connect_product(database_target)) as conn:
            try:
                return complete_recovery(conn, payload.challenge_id, payload.code, payload.new_password, resolved_token)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/session", response_model=SessionResponse)
    def session_status(authenticated_user: str = Depends(require_auth)) -> SessionResponse:
        return SessionResponse(
            authenticated=True,
            user_id=authenticated_user,
            display_name=_display_name(database_target, authenticated_user),
        )

    def require_owner(authenticated_user: str = Depends(require_auth)) -> str:
        if authenticated_user != LOCAL_USER_ID:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can refresh the pipeline")
        return authenticated_user

    def refresh_status_payload() -> RefreshStatusResponse:
        if refresh_manager is None:
            return RefreshStatusResponse(available=False, state="idle", steps=fresh_steps())
        return RefreshStatusResponse(available=True, **refresh_manager.status())

    @app.get("/api/v1/refresh", response_model=RefreshStatusResponse)
    def refresh_status(_owner: str = Depends(require_owner)) -> RefreshStatusResponse:
        return refresh_status_payload()

    @app.post("/api/v1/refresh", response_model=RefreshStatusResponse, status_code=status.HTTP_202_ACCEPTED)
    def start_refresh(_owner: str = Depends(require_owner)) -> RefreshStatusResponse:
        if refresh_manager is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Manual refresh is only available for the main database",
            )
        try:
            refresh_manager.start()
        except RefreshBusy as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return refresh_status_payload()

    @app.get("/api/v1/system/status")
    def system_status_report(_owner: str = Depends(require_owner)) -> dict[str, Any]:
        if system_status is None:
            return {"available": False, "jobs": [], "daily": None, "sources": None, "problems": [],
                    "can_add_boards": board_tracker is not None}
        return {**system_status.status(), "can_add_boards": board_tracker is not None}

    @app.post("/api/v1/system/schedules/{job}/install")
    def install_scheduled_job(job: Literal["daily", "outreach"], _owner: str = Depends(require_owner)) -> dict[str, Any]:
        if system_status is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Scheduling is only available for the main database")
        try:
            return {**system_status.install(job), "can_add_boards": board_tracker is not None}
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    def require_board_tracker() -> BoardTracker:
        if board_tracker is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Adding job boards is only available for the main database")
        return board_tracker

    @app.post("/api/v1/sources/lookup")
    def look_up_job_board(payload: BoardLookupRequest, _owner: str = Depends(require_owner)) -> dict[str, Any]:
        """Probe Greenhouse, Ashby and Lever for a company's board. Writes nothing."""
        try:
            return require_board_tracker().look_up(payload.company)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/sources", status_code=status.HTTP_201_CREATED)
    def add_job_board(payload: BoardAddRequest, _owner: str = Depends(require_owner)) -> dict[str, Any]:
        """Track the board a lookup found, in config/sources.local.json."""
        tracker = require_board_tracker()
        try:
            return tracker.add(payload.lookup_id, student_confirmed=payload.student_confirmed)
        except BoardLookupExpired as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.delete("/api/v1/session", status_code=status.HTTP_204_NO_CONTENT)
    def delete_session(
        response: Response,
        user_session_cookie: Annotated[str | None, Cookie(alias=USER_SESSION_COOKIE)] = None,
    ) -> Response:
        if user_session_cookie and (is_postgres_target(database_target) or Path(database_target).exists()):
            with closing(connect_product(database_target)) as conn:
                revoke_user_token(conn, user_session_cookie)
        response.delete_cookie(USER_SESSION_COOKIE, path="/")
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie("pipeline_csrf", path="/")
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @app.get("/api/v1/opportunities", response_model=OpportunityListResponse)
    def list_opportunities(
        repo: OpportunityRepository = Depends(repository),
        q: str = Query(default="", max_length=200),
        role_type: str = Query(default="", max_length=80),
        pipeline_status: str = Query(default="", alias="status", max_length=80),
        intent_state: Literal["", "undecided", "saved", "passed"] = "",
        exclude_passed: bool = False,
        region: str = Query(default="", max_length=120),
        source: str = Query(default="", max_length=160),
        term: str = Query(default="", max_length=80),
        graduation_year: int | None = Query(default=None, ge=2024, le=2100),
        remote_mode: Literal["", "remote", "hybrid", "onsite", "unknown"] = "",
        min_hourly_pay: float | None = Query(default=None, ge=0, le=1000),
        posted_since: str = Query(default="", max_length=40),
        deadline_before: str = Query(default="", max_length=40),
        sort: Literal["score", "newest", "discovered", "company", "deadline"] = "score",
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        include_inactive: bool = False,
        include_duplicates: bool = False,
    ) -> OpportunityListResponse:
        filters = OpportunityFilters(
            query=q,
            role_type=role_type,
            status=pipeline_status,
            intent_state=intent_state,
            exclude_passed=exclude_passed,
            region=region,
            source=source,
            term=term,
            graduation_year=graduation_year,
            remote_mode=remote_mode,
            min_hourly_pay=min_hourly_pay,
            posted_since=posted_since,
            deadline_before=deadline_before,
            sort=sort,
            active_only=not include_inactive,
            unique_only=not include_duplicates,
            limit=limit,
            offset=offset,
        ).normalized()
        items, total = repo.list(filters)
        if repo.user_id:
            # Decorated here, not in pipeline_core, so the CLI's contract is unchanged.
            deadlines = user_deadlines_for(
                repo.connection, [item["id"] for item in items], user_id=repo.user_id
            )
            items = [{**item, "user_deadline_on": deadlines.get(item["id"])} for item in items]
        return OpportunityListResponse(
            items=items,
            total=total,
            limit=filters.limit,
            offset=filters.offset,
            sort=filters.sort,
            personalized=is_personalized(repo.connection, user_id=repo.user_id) if repo.user_id else True,
        )

    @app.get("/api/v1/opportunities/{opportunity_id}")
    def get_opportunity(
        opportunity_id: str,
        repo: OpportunityRepository = Depends(repository),
    ) -> dict[str, Any]:
        item = repo.get(opportunity_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
        if repo.user_id:
            item = {
                **item,
                "user_deadline": user_deadline(repo.connection, opportunity_id, user_id=repo.user_id),
                "can_set_user_deadline": visible_opportunity(repo.connection, repo.user_id, opportunity_id),
            }
        return item

    @app.put("/api/v1/opportunities/{opportunity_id}/deadline")
    def put_user_deadline(
        opportunity_id: str,
        payload: DeadlineRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Record the student's own deadline for a role (never used by the purge)."""
        try:
            return set_user_deadline(
                conn, opportunity_id, user_id=user_id, deadline_on=payload.deadline_on, note=payload.note
            )
        except DeadlineNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.delete("/api/v1/opportunities/{opportunity_id}/deadline", status_code=status.HTTP_204_NO_CONTENT)
    def delete_user_deadline(
        opportunity_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        clear_user_deadline(conn, opportunity_id, user_id=user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/urgent")
    def get_urgent(
        days: int = Query(default=14, ge=1, le=60),
        repo: OpportunityRepository = Depends(repository),
    ) -> dict[str, Any]:
        """Dated things due in the next ``days`` days, plus the last 60 days overdue."""
        return urgent_queue(
            repo.connection, user_id=repo.user_id or LOCAL_USER_ID, days=days, programs_path=early_programs_file,
        )

    @app.get("/api/v1/early-programs")
    def get_early_programs(repo: OpportunityRepository = Depends(repository)) -> dict[str, Any]:
        """The student's researched early-program list, with the caller's progress."""
        return early_programs(repo.connection, user_id=repo.user_id or LOCAL_USER_ID, path=early_programs_file)

    @app.put("/api/v1/early-programs/{program_id}/status")
    def put_early_program_status(
        program_id: str,
        payload: EarlyProgramStatusRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return set_program_status(
                conn, program_id, user_id=user_id, status=payload.status, path=early_programs_file
            )
        except EarlyProgramNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found") from exc

    @app.get("/api/v1/typesafe")
    def typesafe_status(_authenticated_user: str = Depends(require_auth)) -> dict[str, Any]:
        """Report optional Jev availability without exposing credentials."""
        try:
            client = resolved_typesafe_client_factory()
        except TypeSafeError as exc:
            return {
                "configured": False,
                "model": os.environ.get("TYPESAFE_MODEL", TYPESAFE_DEFAULT_MODEL),
                "question_set_version": QUESTION_SET_VERSION,
                "external_processing": True,
                "automatic_actions": False,
                "setup_hint": str(exc),
            }
        return {
            "configured": client.configured,
            "model": client.model,
            "question_set_version": QUESTION_SET_VERSION,
            "external_processing": True,
            "automatic_actions": False,
            "setup_hint": "" if client.configured else "Set TYPESAFE_API_KEY to enable Jev reviews",
        }

    @app.post("/api/v1/opportunities/{opportunity_id}/jev-review")
    def opportunity_jev_review(
        opportunity_id: str,
        repo: OpportunityRepository = Depends(repository),
    ) -> dict[str, Any]:
        """Run an explicit, non-persisted Jev review over bounded fields."""
        item = repo.get(opportunity_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
        row = repo.connection.execute(
            "SELECT profile_json FROM profiles WHERE user_id=?", (repo.user_id,)
        ).fetchone()
        try:
            profile = json.loads(row[0] or "{}") if row else {}
        except (TypeError, json.JSONDecodeError):
            profile = {}
        if not isinstance(profile, dict):
            profile = {}
        try:
            return review_opportunity_with_typesafe(
                resolved_typesafe_client_factory(), item, profile
            )
        except TypeSafeNotConfigured as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        except TypeSafeError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
            ) from exc

    @app.post("/api/v1/opportunities/{opportunity_id}/actions")
    def opportunity_action(
        opportunity_id: str,
        payload: IntentRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 200):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Idempotency-Key must contain 1 to 200 characters",
            )
        try:
            return record_intent(
                conn,
                opportunity_id,
                payload.action,
                idempotency_key=idempotency_key, user_id=user_id)
        except OpportunityNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Opportunity not found",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

    @app.get("/api/v1/applications")
    def applications(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        items = list_applications(conn, user_id=user_id)
        return {"items": items, "total": len(items), "stages": sorted(APPLICATION_STAGES)}

    @app.get("/api/v1/applications/export")
    def export_applications(
        export_format: Literal["json", "csv"] = Query(default="json", alias="format"),
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        items = list_applications(conn, user_id=user_id)
        if export_format == "json":
            return Response(
                content=json.dumps(items, indent=2, sort_keys=True),
                media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="applications.json"'},
            )
        output = io.StringIO(newline="")
        fields = [
            "id", "opportunity_id", "company", "title", "stage", "notes",
            "applied_at", "follow_up_at", "location", "region", "url", "created_at", "updated_at",
        ]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(items)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="applications.csv"'},
        )

    @app.get("/api/v1/applications/analytics")
    def tracker_analytics(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return application_analytics(conn, user_id=user_id)

    @app.post("/api/v1/applications/import")
    async def import_application_file(
        upload: UploadFile = File(...),
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        data = await upload.read(2 * 1024 * 1024 + 1)
        original_name = (upload.filename or "applications.json").lower()
        await upload.close()
        if len(data) > 2 * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Application imports are limited to 2 MB",
            )
        try:
            text_data = data.decode("utf-8-sig")
            if original_name.endswith(".csv"):
                records = [dict(row) for row in csv.DictReader(io.StringIO(text_data))]
            else:
                parsed = json.loads(text_data)
                if isinstance(parsed, list):
                    records = parsed
                elif isinstance(parsed, dict):
                    records = parsed.get("items", [])
                else:
                    raise ValueError("Import must be a JSON list or object with an items list")
            if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
                raise ValueError("Import must contain a list of application objects")
            return import_applications(conn, records, user_id=user_id)
        except (UnicodeDecodeError, json.JSONDecodeError, csv.Error, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/applications/{application_id}")
    def get_application_detail(
        application_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return application_detail(conn, application_id, user_id=user_id)
        except ApplicationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc

    @app.post("/api/v1/applications/{application_id}/contacts", status_code=status.HTTP_201_CREATED)
    def create_contact(
        application_id: str,
        payload: ContactCreateRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return add_application_contact(conn, application_id, **payload.model_dump(), user_id=user_id)
        except ApplicationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/applications/{application_id}/tasks", status_code=status.HTTP_201_CREATED)
    def create_task(
        application_id: str,
        payload: TaskCreateRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return add_application_task(
                conn,
                application_id,
                title=payload.title,
                due_at=payload.due_at,
                timezone_name=payload.timezone,
                user_id=user_id,
            )
        except ApplicationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.patch("/api/v1/application-tasks/{task_id}")
    def patch_task(
        task_id: str,
        payload: TaskUpdateRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return update_application_task(conn, task_id, status=payload.status, user_id=user_id)
        except ApplicationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.patch("/api/v1/applications/{application_id}")
    def patch_application(
        application_id: str,
        payload: ApplicationUpdateRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        if payload.stage is None and payload.notes is None and payload.follow_up_at is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Provide at least one field to update",
            )
        try:
            return update_application(
                conn,
                application_id,
                stage=payload.stage,
                notes=payload.notes,
                follow_up_at=payload.follow_up_at,
                timezone_name=payload.timezone, user_id=user_id)
        except ApplicationNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Application not found",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    def outreach_compose_settings() -> dict[str, str]:
        """Where an approved draft opens. The app never sends; the student does."""
        provider = os.environ.get("PIPELINE_OUTREACH_COMPOSE", "mailto").strip().lower()
        account = sender_account()
        if provider not in {"gmail", "mailto"} or (provider == "gmail" and not account):
            provider = "mailto"
        return {"provider": provider, "account": account}

    def outreach_discovery_payload(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
        return {
            "available": outreach_discovery_manager is not None and user_id == LOCAL_USER_ID,
            "scopes": [{"id": key, "label": value["label"]} for key, value in discovery_scope_definitions().items()],
            "runs": last_discovery_runs(conn, user_id=user_id),
            "active": outreach_discovery_manager.status() if outreach_discovery_manager is not None else None,
        }

    def outreach_recontact_payload(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
        return {
            "available": outreach_recontact_manager is not None and user_id == LOCAL_USER_ID,
            "eligible": len(recontact_eligible_targets(conn, user_id=user_id)),
            "active": outreach_recontact_manager.status() if outreach_recontact_manager is not None else None,
        }

    def require_recontact_owner(user_id: str) -> RecontactManager:
        if user_id != LOCAL_USER_ID:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can search for contacts in bulk")
        if outreach_recontact_manager is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The bulk contact search is only available for the main database")
        return outreach_recontact_manager

    @app.get("/api/v1/outreach")
    def outreach_targets(
        status_filter: str = Query(default="", alias="status", max_length=40),
        channel: str = Query(default="", max_length=100),
        q: str = Query(default="", max_length=200),
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        if status_filter and status_filter not in OUTREACH_STATUSES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown outreach status")
        everything = list_outreach_targets(conn, user_id=user_id)
        items = (
            list_outreach_targets(conn, user_id=user_id, status=status_filter, channel=channel, query=q)
            if status_filter or channel or q.strip()
            else everything
        )
        return {
            "items": items,
            "total": len(items),
            "summary": outreach_summary(everything),
            "statuses": list(OUTREACH_STATUSES),
            "priorities": list(OUTREACH_PRIORITIES),
            "contact_confidence": list(CONTACT_CONFIDENCE),
            "compose": outreach_compose_settings(),
            "gmail_drafts": gmail_drafts_status(conn, user_id=user_id),
            "discovery": outreach_discovery_payload(conn, user_id),
            "recontact": outreach_recontact_payload(conn, user_id),
        }

    @app.post("/api/v1/outreach", status_code=status.HTTP_201_CREATED)
    def create_outreach(
        payload: OutreachTargetRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return create_outreach_target(conn, payload.model_dump(exclude_unset=True), user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/outreach/export")
    def export_outreach(
        export_format: Literal["json", "csv"] = Query(default="json", alias="format"),
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        items = list_outreach_targets(conn, user_id=user_id)
        if export_format == "json":
            for item in items:
                for derived in (
                    "draft_checks", "follow_up_checks", "follow_up_due", "revisit_due", "suggestion",
                    "draft_history_count", "follow_up_history_count",
                ):
                    item.pop(derived, None)
            return Response(
                content=json.dumps({"format": "outreach-targets-v1", "items": items}, indent=2, sort_keys=True),
                media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="outreach.json"'},
            )
        return Response(
            content=export_outreach_csv(items),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="outreach.csv"'},
        )

    @app.post("/api/v1/outreach/import")
    async def import_outreach_file(
        upload: UploadFile = File(...),
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        data = await upload.read(2 * 1024 * 1024 + 1)
        original_name = upload.filename or "outreach.json"
        await upload.close()
        if len(data) > 2 * 1024 * 1024:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Outreach imports are limited to 2 MB")
        try:
            records = parse_outreach_import(data, original_name)
            return import_outreach_targets(conn, records, user_id=user_id)
        except (UnicodeDecodeError, json.JSONDecodeError, csv.Error, ValueError, AttributeError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc) or "Unreadable import") from exc

    @app.get("/api/v1/outreach/discovery")
    def outreach_discovery_status(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return outreach_discovery_payload(conn, user_id)

    @app.post("/api/v1/outreach/discovery", status_code=status.HTTP_202_ACCEPTED)
    def start_outreach_discovery(
        payload: OutreachDiscoveryRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        if user_id != LOCAL_USER_ID:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can run the deep search")
        if outreach_discovery_manager is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The deep search is only available for the main database")
        try:
            outreach_discovery_manager.start(user_id=user_id, scopes=list(payload.scopes) if payload.scopes else None)
        except DiscoveryBusy as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return outreach_discovery_payload(conn, user_id)

    @app.get("/api/v1/outreach/settings")
    def get_outreach_settings(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_owner),
    ) -> dict[str, Any]:
        if outreach_settings is None:
            return {"available": False}
        return {"available": True, **outreach_settings.view(conn, user_id=user_id)}

    @app.put("/api/v1/outreach/settings")
    def put_outreach_settings(
        payload: OutreachSettingsRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_owner),
    ) -> dict[str, Any]:
        if outreach_settings is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Settings can only be changed for the main database")
        try:
            view = outreach_settings.update(conn, payload.model_dump(exclude_unset=True), user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return {"available": True, **view}

    @app.get("/api/v1/outreach/recontact")
    def outreach_recontact_status(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return outreach_recontact_payload(conn, user_id)

    @app.post("/api/v1/outreach/recontact", status_code=status.HTTP_202_ACCEPTED)
    def start_outreach_recontact(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Search again for people at shared-inbox targets. Reports only; changes no contact."""
        manager = require_recontact_owner(user_id)
        try:
            manager.start_report(user_id=user_id)
        except RecontactBusy as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return outreach_recontact_payload(conn, user_id)

    @app.post("/api/v1/outreach/recontact/apply", status_code=status.HTTP_202_ACCEPTED)
    def apply_outreach_recontact(
        payload: RecontactApplyRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Apply the upgrades the student ticked in the last report, without searching again."""
        manager = require_recontact_owner(user_id)
        choices = {choice.target_id: choice.to.strip() for choice in payload.choices}
        try:
            manager.start_apply(user_id=user_id, choices=choices, redraft=payload.redraft)
        except RecontactBusy as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return outreach_recontact_payload(conn, user_id)

    @app.post("/api/v1/outreach/{target_id}/draft")
    def draft_outreach(
        target_id: str,
        payload: OutreachDraftRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return generate_outreach_draft(
                conn, target_id, user_id=user_id, kind=payload.kind,
                provider_factory=resolved_outreach_provider_factory, provider=outreach_draft_provider,
                comments=payload.comments,
            )
        except OutreachNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"The draft model failed: {exc}") from exc

    @app.get("/api/v1/outreach/{target_id}/drafts")
    def outreach_draft_history(
        target_id: str,
        kind: Literal["initial", "follow_up"] = Query(default="initial"),
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Every stored draft of one kind, oldest first, so a worse regeneration can be undone."""
        try:
            return {"items": outreach_draft_versions(conn, target_id, user_id=user_id, kind=kind)}
        except OutreachNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc

    @app.post("/api/v1/outreach/{target_id}/drafts/{version_id}/restore")
    def restore_outreach_draft(
        target_id: str,
        version_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return restore_outreach_draft_version(conn, target_id, version_id, user_id=user_id)
        except OutreachNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc
        except DraftVersionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That earlier draft was not found") from exc

    @app.post("/api/v1/outreach/{target_id}/approve")
    def approve_outreach(
        target_id: str,
        payload: OutreachApprovalRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return approve_outreach_draft(
                conn, target_id, user_id=user_id, kind=payload.kind, fingerprint=payload.fingerprint,
                acknowledge_warnings=payload.acknowledge_warnings,
            )
        except OutreachNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc
        except DraftChangedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/outreach/{target_id}/gmail-draft")
    def gmail_draft_for_outreach(
        target_id: str,
        payload: OutreachDraftRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Create the approved draft in the student's Gmail Drafts. Nothing is sent."""
        try:
            return create_gmail_draft(
                conn, target_id, user_id=user_id, kind=payload.kind,
                client_factory=outreach_gmail_client_factory or default_gmail_client_factory,
            )
        except OutreachNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc
        except GmailAuthError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        except (RuntimeError, httpx.HTTPError) as exc:
            detail = str(exc) if isinstance(exc, RuntimeError) else "Could not reach Gmail"
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc

    @app.post("/api/v1/outreach/{target_id}/confirm-research")
    def confirm_research_for_outreach(
        target_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return confirm_outreach_research(conn, target_id, user_id=user_id)
        except OutreachNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc

    @app.get("/api/v1/outreach/{target_id}/contacts")
    def outreach_contacts(
        target_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return {"candidates": list_outreach_candidates(conn, target_id, user_id=user_id)}
        except OutreachNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc

    @app.post("/api/v1/outreach/{target_id}/find-contacts")
    def find_contacts_for_outreach(
        target_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            with ExitStack() as stack:
                fetcher = stack.enter_context(resolved_contact_client_factory())
                verifier = resolved_smtp_verifier_factory()
                renderer = resolved_renderer_factory()
                return find_outreach_contacts(
                    conn, target_id, user_id=user_id, fetcher=fetcher, delay=outreach_contact_delay,
                    verifier=stack.enter_context(verifier) if verifier is not None else None,
                    renderer=stack.enter_context(renderer) if renderer is not None else None,
                )
        except OutreachNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/outreach/{target_id}/contacts/{candidate_id}/apply")
    def apply_outreach_contact(
        target_id: str,
        candidate_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return apply_outreach_candidate(conn, target_id, candidate_id, user_id=user_id)
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact candidate not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/outreach/{target_id}/reply")
    def log_outreach_reply_route(
        target_id: str,
        payload: OutreachReplyRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return log_outreach_reply(conn, target_id, payload.text, user_id=user_id)
        except OutreachNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/outreach/{target_id}")
    def outreach_detail(
        target_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return get_outreach_target(conn, target_id, user_id=user_id, include_events=True)
        except OutreachNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc

    @app.patch("/api/v1/outreach/{target_id}")
    def update_outreach(
        target_id: str,
        payload: OutreachTargetRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return update_outreach_target(conn, target_id, payload.model_dump(exclude_unset=True), user_id=user_id)
        except OutreachNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc
        # LocationConflictError subclasses ValueError, so this ordering is what
        # makes it a 409 rather than being swallowed as an invalid request.
        except LocationConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.delete("/api/v1/outreach/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
    def remove_outreach(
        target_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        if not delete_outreach_target(conn, target_id, user_id=user_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/profile")
    def profile(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return get_profile(conn, user_id=user_id)
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found") from exc

    @app.put("/api/v1/profile")
    def put_profile(
        payload: ProfileUpdateRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return update_profile(
                conn, payload.updates, payload.confirmed_fields, user_id=user_id, profile_file=profile_file
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/profile/export")
    def export_profile(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        try:
            profile_data = get_profile(conn, user_id=user_id)["profile"]
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found") from exc
        return Response(
            content=json.dumps(profile_data, indent=2, sort_keys=True),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="profile.json"'},
        )

    @app.get("/api/v1/account/export")
    def export_full_account(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        return Response(
            content=json.dumps(export_account(conn, user_id=user_id), indent=2, sort_keys=True),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="opportunity-account.json"'},
        )

    @app.delete("/api/v1/account")
    def permanently_delete_account(
        confirmation: Annotated[str | None, Header(alias="X-Confirm-Delete")] = None,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        if confirmation != "DELETE":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Send X-Confirm-Delete: DELETE to confirm permanent account deletion")
        return delete_account(conn, [resume_storage, capture_storage, interview_storage], user_id=user_id)

    @app.get("/api/v1/resumes")
    def resumes(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        items = list_resumes(conn, user_id=user_id)
        return {"items": items, "total": len(items)}

    @app.post("/api/v1/resumes", status_code=status.HTTP_201_CREATED)
    async def upload_resume(
        resume: UploadFile = File(...),
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        original_name = resume.filename or "resume"
        data = await resume.read(MAX_RESUME_BYTES + 1)
        await resume.close()
        try:
            return store_resume(
                conn,
                data=data,
                original_name=original_name,
                storage_root=resume_storage, user_id=user_id)
        except ResumeValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/resumes/{version_id}")
    def get_resume(
        version_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return resume_record(conn, version_id, user_id=user_id)
        except ResumeNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found") from exc

    @app.post("/api/v1/resumes/{version_id}/confirm")
    def confirm_resume_version(
        version_id: str,
        payload: ResumeConfirmRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return confirm_resume(
                conn,
                version_id,
                payload.confirmed_data,
                payload.profile_updates,
                payload.confirmed_profile_fields, user_id=user_id, profile_file=profile_file)
        except ResumeNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/resumes/{version_id}/file")
    def download_resume(
        version_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> FileResponse:
        try:
            path, filename, media_type = resume_file_path(conn, version_id, resume_storage, user_id=user_id)
        except ResumeNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found") from exc
        return FileResponse(path, media_type=media_type, filename=filename)

    @app.delete("/api/v1/resumes/{version_id}", status_code=status.HTTP_204_NO_CONTENT)
    def remove_resume(
        version_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        try:
            delete_resume(conn, version_id, resume_storage, user_id=user_id)
        except ResumeNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found") from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/opportunity-captures/url", status_code=status.HTTP_201_CREATED)
    def create_url_capture(
        payload: CaptureUrlRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return capture_url(conn, payload.url, user_id=user_id)
        except CaptureValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/opportunity-captures/file", status_code=status.HTTP_201_CREATED)
    async def create_file_capture(
        capture: UploadFile = File(...),
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        original_name = capture.filename or "capture"
        data = await capture.read(MAX_CAPTURE_BYTES + 1)
        await capture.close()
        try:
            return store_capture_file(
                conn,
                data,
                original_name,
                capture_storage, user_id=user_id)
        except CaptureValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/opportunity-captures/{capture_id}")
    def capture_detail(
        capture_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return get_capture(conn, capture_id, user_id=user_id)
        except CaptureNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capture not found") from exc

    @app.post("/api/v1/opportunity-captures/{capture_id}/confirm")
    def confirm_capture_draft(
        capture_id: str,
        payload: CaptureConfirmRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return confirm_capture(conn, capture_id, payload.model_dump(), user_id=user_id)
        except CaptureNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capture not found") from exc
        except CaptureValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/preparation/documents")
    def preparation_documents(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        items = list_documents(conn, user_id=user_id)
        return {"items": items, "total": len(items), "pdf_available": resolved_pdf_renderer is not None}

    @app.post("/api/v1/preparation/documents", status_code=status.HTTP_201_CREATED)
    def generate_preparation_document(
        payload: DocumentCreateRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            provider = None
            if payload.provider:
                metadata = next(item for item in provider_catalog() if item["id"] == payload.provider)
                if not metadata["configured"]:
                    raise ValueError(metadata["setup_hint"])
                provider = resolved_agent_provider_factory(payload.provider, str(metadata["model"]))
            return create_document(
                conn,
                payload.opportunity_id,
                payload.document_type,
                provider=provider, user_id=user_id)
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/preparation/documents/{document_id}")
    def get_preparation_document(
        document_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return document_record(conn, document_id, user_id=user_id)
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc

    @app.put("/api/v1/preparation/documents/{document_id}")
    def update_preparation_document(
        document_id: str,
        payload: DocumentEditRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            document = edit_document(conn, document_id, payload.content, payload.evidence_fields, user_id=user_id)
            delete_document_artifact(conn, document_id, resume_storage, user_id=user_id)
            return document
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/preparation/documents/{document_id}/approve")
    def approve_preparation_document(
        document_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            document = approve_document(conn, document_id, user_id=user_id)
            artifact = ensure_document_artifact(conn, document_id, resume_storage, user_id=user_id)
            public_artifact = {
                "id": artifact["id"],
                "filename": artifact["filename"],
                "media_type": artifact["media_type"],
                "byte_size": artifact["byte_size"],
                "sha256": artifact["sha256"],
                "created_at": artifact["created_at"],
            }
            return {**document, "artifact": public_artifact}
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.delete("/api/v1/preparation/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_preparation_document(
        document_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        try:
            document_record(conn, document_id, user_id=user_id)
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc
        delete_document_artifact(conn, document_id, resume_storage, user_id=user_id)
        with conn:
            conn.execute(
                "DELETE FROM generated_documents WHERE id=? AND user_id=?",
                (document_id, user_id),
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/preparation/documents/{document_id}/download")
    def download_preparation_document(
        document_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        try:
            document = document_record(conn, document_id, user_id=user_id)
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc
        filename = f"{document['document_type']}-v{document['version']}.md"
        return Response(
            content=document["content"],
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/v1/preparation/documents/{document_id}/pdf")
    def download_preparation_document_pdf(
        document_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        try:
            document = document_record(conn, document_id, user_id=user_id)
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc
        if resolved_pdf_renderer is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PDF export needs Playwright: pip install -r requirements-optional.txt, then python -m playwright install chromium",
            )
        label = "Cover letter" if document["document_type"] == "cover_letter" else "Resume"
        try:
            pdf = resolved_pdf_renderer(markdown_to_html(document["content"], title=f"{label} v{document['version']}"))
        except Exception as exc:  # noqa: BLE001 - a missing browser build is the usual cause
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"The PDF could not be rendered ({type(exc).__name__}). If Chromium is missing, run: python -m playwright install chromium",
            ) from exc
        filename = f"{document['document_type']}-v{document['version']}.pdf"
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/v1/preparation/answers")
    def answer_library(
        q: str = Query(default="", max_length=200),
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        items = list_answers(conn, q, user_id=user_id)
        return {"items": items, "total": len(items)}

    @app.post("/api/v1/preparation/answers", status_code=status.HTTP_201_CREATED)
    def create_library_answer(
        payload: AnswerSaveRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return save_answer(conn, **payload.model_dump(), user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.delete("/api/v1/preparation/answers/all", status_code=status.HTTP_204_NO_CONTENT)
    def delete_all_library_answers(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        delete_answer(conn, user_id=user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.put("/api/v1/preparation/answers/{answer_id}")
    def update_library_answer(
        answer_id: str,
        payload: AnswerSaveRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return save_answer(conn, answer_id=answer_id, **payload.model_dump(), user_id=user_id)
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Answer not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.delete("/api/v1/preparation/answers/{answer_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_library_answer(
        answer_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        if not delete_answer(conn, answer_id, user_id=user_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Answer not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/preparation/interviews", status_code=status.HTTP_201_CREATED)
    def start_mock_interview(
        payload: InterviewCreateRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return create_mock_interview(conn, payload.opportunity_id, user_id=user_id)
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc

    @app.get("/api/v1/preparation/interviews")
    def mock_interviews(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        items = list_mock_interviews(conn, user_id=user_id)
        return {"items": items, "total": len(items)}

    @app.get("/api/v1/preparation/interviews/{interview_id}")
    def get_mock_interview(
        interview_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return interview_record(conn, interview_id, user_id=user_id)
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found") from exc

    @app.post("/api/v1/preparation/questions/{question_id}/answers", status_code=status.HTTP_201_CREATED)
    def submit_mock_answer(
        question_id: str,
        payload: MockAnswerRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return answer_mock_question(conn, question_id, payload.answer_text, payload.transcript, user_id=user_id)
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/preparation/questions/{question_id}/recorded-answers", status_code=status.HTTP_201_CREATED)
    async def submit_recorded_mock_answer(
        question_id: str,
        audio: Annotated[UploadFile, File()],
        answer_text: Annotated[str, Form()] = "",
        transcript: Annotated[str, Form()] = "",
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        if len(answer_text) > 100_000 or len(transcript) > 100_000:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Reviewed answer text is too large")
        audio_data = await audio.read(MAX_MOCK_AUDIO_BYTES + 1)
        try:
            return store_recorded_mock_answer(
                conn,
                question_id,
                answer_text,
                transcript,
                audio_data,
                audio.content_type or "",
                interview_storage,
                user_id=user_id,
            )
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/preparation/answers/{answer_id}/audio")
    def download_recorded_mock_answer(
        answer_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> FileResponse:
        try:
            path, media_type = recorded_mock_answer_path(conn, answer_id, interview_storage, user_id=user_id)
        except PreparationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found") from exc
        return FileResponse(path, media_type=media_type, filename=f"mock-interview{path.suffix}")

    @app.get("/api/v1/agent/providers")
    def agent_providers(
        _authenticated_user: str = Depends(require_auth),
    ) -> dict[str, Any]:
        items = provider_catalog()
        return {"items": items, "total": len(items)}

    @app.get("/api/v1/agent/threads")
    def agent_threads(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        items = list_threads(conn, user_id=user_id)
        return {"items": items, "total": len(items)}

    @app.post("/api/v1/agent/threads", status_code=status.HTTP_201_CREATED)
    def start_agent_thread(
        payload: AgentThreadRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            provider = payload.provider or default_provider()
            return create_thread(conn, payload.title, provider, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/agent/activity")
    def agent_activity(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        items = activity_feed(conn, user_id=user_id)
        return {"items": items, "total": len(items)}

    @app.get("/api/v1/agent/threads/{thread_id}")
    def get_agent_thread(
        thread_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return thread_record(conn, thread_id, user_id=user_id)
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent thread not found") from exc

    @app.post("/api/v1/agent/threads/{thread_id}/messages", status_code=status.HTTP_201_CREATED)
    def send_agent_message(
        thread_id: str,
        payload: AgentMessageRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return post_message(
                conn,
                thread_id,
                payload.content,
                provider_factory=resolved_agent_provider_factory, user_id=user_id)
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent thread not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/agent/threads/{thread_id}/cancel")
    def cancel_agent_thread(
        thread_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return cancel_thread(conn, thread_id, user_id=user_id)
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active agent thread not found") from exc

    @app.post("/api/v1/agent/proposals/{proposal_id}/decision")
    def decide_agent_action(
        proposal_id: str,
        payload: AgentDecisionRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return decide_proposal(conn, proposal_id, payload.decision, user_id=user_id)
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposed action not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post("/api/v1/extension/pairings", status_code=status.HTTP_201_CREATED)
    def create_extension_pairing(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return create_pairing(conn, user_id=user_id)

    @app.post("/api/v1/extension/pairings/redeem")
    def redeem_extension_pairing(
        payload: ExtensionPairingRedeemRequest,
        request: Request,
    ) -> dict[str, Any]:
        origin = request.headers.get("Origin", "")
        if not (is_postgres_target(database_target) or Path(database_target).exists()):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Product database unavailable")
        try:
            with closing(connect_product(database_target)) as conn:
                return redeem_pairing(conn, payload.code, origin, payload.device_name)
        except ExtensionAuthError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    @app.get("/api/v1/extension/devices")
    def extension_devices(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        items = list_extension_devices(conn, user_id=user_id)
        return {"items": items, "total": len(items)}

    @app.delete("/api/v1/extension/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_extension_device(
        device_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        if not revoke_extension_device(conn, device_id, user_id=user_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Extension device not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/extension/application-candidates")
    def extension_application_candidates(
        page_url: str = Query(min_length=8, max_length=2_000),
        context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
    ) -> dict[str, Any]:
        conn, device = context
        try:
            return application_candidates(conn, page_url, user_id=device["user_id"])
        except ExtensionApplyError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/extension/apply-context")
    def extension_apply_context(
        application_id: str = Query(min_length=1, max_length=500),
        context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
    ) -> dict[str, Any]:
        conn, device = context
        warnings: list[str] = []
        approved = conn.execute(
            """
            SELECT d.id FROM generated_documents d
            LEFT JOIN generated_document_artifacts a ON a.document_id=d.id
            WHERE d.user_id=? AND d.status='approved' AND a.id IS NULL
            """,
            (device["user_id"],),
        ).fetchall()
        for row in approved:
            try:
                ensure_document_artifact(conn, str(row["id"]), resume_storage, user_id=device["user_id"])
            except (RuntimeError, ValueError) as exc:
                warnings.append(str(exc))
        try:
            result = apply_context(conn, application_id, user_id=device["user_id"])
        except ApplicationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
        result["artifact_warnings"] = sorted(set(warnings))
        return result

    @app.get("/api/v1/extension/artifacts/{artifact_id}/file")
    def download_extension_artifact(
        artifact_id: str,
        application_id: str = Query(min_length=1, max_length=500),
        context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
    ) -> FileResponse:
        conn, device = context
        try:
            path, filename, media_type, sha256 = extension_artifact_path(
                conn,
                artifact_id,
                application_id,
                resume_storage,
                user_id=device["user_id"],
            )
        except ApplicationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
        except ExtensionApplyError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type=media_type,
            filename=filename,
            headers={"X-Artifact-SHA256": sha256, "Cache-Control": "no-store"},
        )

    @app.put("/api/v1/extension/sessions/{session_id}")
    def put_extension_session(
        session_id: str,
        payload: ExtensionSessionRequest,
        context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
    ) -> dict[str, Any]:
        conn, device = context
        try:
            return sync_extension_session(
                conn, session_id, payload.model_dump(), user_id=device["user_id"]
            )
        except ApplicationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
        except ExtensionAuthError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ExtensionApplyError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.put("/api/v1/extension/sessions/{session_id}/steps/{step_key}")
    def put_extension_step(
        session_id: str,
        step_key: str,
        payload: ExtensionStepRequest,
        context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
    ) -> dict[str, Any]:
        conn, device = context
        try:
            return sync_extension_step(
                conn,
                session_id,
                step_key,
                payload.model_dump(),
                user_id=device["user_id"],
            )
        except ExtensionApplyError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/extension/sessions/{session_id}/confirm-submitted")
    def confirm_extension_submission(
        session_id: str,
        context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
    ) -> dict[str, Any]:
        conn, device = context
        try:
            return confirm_extension_submitted(conn, session_id, user_id=device["user_id"])
        except ApplicationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Apply session not found") from exc

    @app.post("/api/v1/extension/answers", status_code=status.HTTP_201_CREATED)
    def save_extension_answer(
        payload: ExtensionAnswerRequest,
        context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
    ) -> dict[str, Any]:
        conn, device = context
        if answer_is_sensitive(payload.question):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Sensitive or consequential answers cannot enter the reusable library",
            )
        try:
            return save_answer(conn, **payload.model_dump(), user_id=device["user_id"])
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/apply-sessions")
    def apply_sessions(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT * FROM application_form_sessions WHERE user_id=? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
        items = [{**dict(row), "fields": json.loads(row["fields_json"])} for row in rows]
        for item in items:
            item.pop("fields_json", None)
        return {"items": items, "total": len(items)}

    @app.post("/api/v1/apply-sessions")
    def sync_apply_session(
        payload: ApplySessionRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(payload.page_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Apply sessions require an HTTP or HTTPS page URL",
            )
        if payload.application_id:
            owned = conn.execute(
                "SELECT 1 FROM applications WHERE id=? AND user_id=?",
                (payload.application_id, user_id),
            ).fetchone()
            if not owned:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
        safe_fields: list[dict[str, Any]] = []
        allowed = {
            "key", "label", "type", "provenance", "confidence",
            "requires_review", "filled", "reason",
        }
        for field in payload.fields:
            field_type = str(field.get("type", "")).lower()
            label = str(field.get("label", ""))[:500]
            if field_type in {"submit", "button", "image"} or "submit application" in label.lower():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Final-submit controls are prohibited from Apply Mode sessions",
                )
            safe_fields.append({key: value for key, value in field.items() if key in allowed})
        timestamp = utc_now()
        existing_session = conn.execute(
            "SELECT user_id FROM application_form_sessions WHERE id=?", (payload.session_id,)
        ).fetchone()
        if existing_session and existing_session["user_id"] != user_id:
            # Session ids are client-generated; never let one user overwrite
            # or read another user's session by replaying its id.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Apply session belongs to another user")
        with conn:
            conn.execute(
                """
                INSERT INTO application_form_sessions(
                    id, user_id, application_id, page_url, ats_type,
                    fields_json, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    application_id=excluded.application_id,
                    page_url=excluded.page_url,
                    ats_type=excluded.ats_type,
                    fields_json=excluded.fields_json,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (payload.session_id, user_id, payload.application_id, payload.page_url, payload.ats_type, json.dumps(safe_fields), payload.status, timestamp, timestamp),
            )
            if payload.application_id:
                conn.execute(
                    """
                    INSERT INTO application_events(
                        application_id, event_type, from_stage, to_stage,
                        detail_json, created_at
                    ) VALUES(?, 'apply_session_synced', NULL, NULL, ?, ?)
                    """,
                    (payload.application_id, json.dumps({"session_id": payload.session_id, "status": payload.status}), timestamp),
                )
        return {
            "id": payload.session_id,
            "application_id": payload.application_id,
            "page_url": payload.page_url,
            "ats_type": payload.ats_type,
            "fields": safe_fields,
            "status": payload.status,
            "updated_at": timestamp,
            "final_submit_available": False,
        }

    @app.get("/api/v1/connections")
    def connections(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        items = list_connectors(conn, user_id=user_id)
        return {"items": items, "total": len(items)}

    @app.post("/api/v1/connections", status_code=status.HTTP_201_CREATED)
    def connect_account(
        payload: ConnectorRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return connect_provider(conn, payload.provider, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    @app.get("/api/v1/connections/oauth/{provider}/start")
    def start_oauth_connection(
        provider: Literal["google", "microsoft", "gmail_drafts"],
        request: Request,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        origin = os.environ.get("PIPELINE_PUBLIC_ORIGIN") or str(request.base_url).rstrip("/")
        redirect_uri = f"{origin}/connections/oauth/{provider}/callback"
        try:
            return begin_oauth(conn, provider, redirect_uri, user_id=user_id, login_hint=sender_account() if provider == "gmail_drafts" else "")
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    @app.post("/api/v1/connections/oauth/{provider}/complete")
    async def finish_oauth_connection(
        provider: Literal["google", "microsoft", "gmail_drafts"],
        payload: OAuthCompleteRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await complete_oauth(conn, provider, payload.state, payload.code, os.environ.get("PIPELINE_CONNECTION_KEY", ""), user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.delete("/api/v1/connections/{connector_id}")
    def disconnect_account(
        connector_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return disconnect_provider(conn, connector_id, user_id=user_id)
        except ConnectionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found") from exc

    @app.get("/api/v1/monitored-events")
    def monitored_events(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        items = list_monitored_events(conn, user_id=user_id)
        return {"items": items, "total": len(items)}

    @app.post("/api/v1/monitored-events", status_code=status.HTTP_201_CREATED)
    def ingest_monitored_message(
        payload: MonitoredMessageRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return ingest_message(conn, **payload.model_dump(), user_id=user_id)
        except ConnectionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/connections/webhook", status_code=status.HTTP_201_CREATED)
    async def verified_connector_webhook(
        request: Request,
        signature: Annotated[str | None, Header(alias="X-Webhook-Signature")] = None,
    ) -> dict[str, Any]:
        secret = os.environ.get("PIPELINE_WEBHOOK_SECRET", "")
        if not secret:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Webhook receiver is not configured")
        body = await request.body()
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not signature or not constant_time_equal(signature, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")
        try:
            payload = MonitoredMessageRequest.model_validate_json(body)
            with closing(connect_product(database_target)) as conn:
                # Webhook callers authenticate by HMAC, not session; the event
                # belongs to whichever user owns the referenced connector.
                owner = conn.execute(
                    "SELECT user_id FROM connector_accounts WHERE id=?", (payload.connector_id,)
                ).fetchone()
                if not owner:
                    raise ConnectionNotFoundError(payload.connector_id)
                return ingest_message(conn, **payload.model_dump(), user_id=str(owner["user_id"]))
        except ConnectionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found") from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/monitored-events/{event_id}/decision")
    def decide_monitored_update(
        event_id: str,
        payload: MonitoredDecisionRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return decide_monitored_event(conn, event_id, payload.decision, payload.application_id, user_id=user_id)
        except (ConnectionNotFoundError, ApplicationNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event or application not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.get("/api/v1/notification-preferences")
    def notification_preferences(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return ensure_preferences(conn, user_id=user_id)

    @app.put("/api/v1/notification-preferences")
    def put_notification_preferences(
        payload: NotificationPreferencesRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return update_preferences(conn, payload.updates, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/notifications/opt-out")
    def notification_opt_out(
        payload: NotificationOptOutRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return apply_channel_opt_out(conn, payload.channel, payload.keyword, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/phone-verifications", status_code=status.HTTP_201_CREATED)
    def create_phone_verification(
        payload: PhoneRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return request_phone_verification(conn, payload.phone_e164, resolved_token, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/phone-verifications/confirm")
    def verify_phone(
        payload: PhoneConfirmRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return confirm_phone(conn, payload.challenge_id, payload.code, resolved_token, user_id=user_id)
        except ConnectionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Verification challenge not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/notifications/voice-check-in")
    def request_voice_check_in(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return queue_notification(
            conn,
            "voice",
            f"requested-check-in:{utc_now()[:16]}",
            {"kind": "requested_check_in", "requested_by": "user"}, user_id=user_id)

    @app.get("/api/v1/dossier")
    def career_dossier(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return dossier(conn, user_id=user_id)

    @app.get("/api/v1/dossier/export")
    def export_dossier(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        return Response(
            content=json.dumps(dossier(conn, user_id=user_id), indent=2, sort_keys=True),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="career-dossier.json"'},
        )

    @app.put("/api/v1/dossier/settings")
    def put_dossier_settings(
        payload: DossierSettingsRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return update_dossier_settings(conn, payload.paused, payload.retention_days, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/dossier/items", status_code=status.HTTP_201_CREATED)
    def create_dossier_item(
        payload: DossierItemRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return save_dossier_item(conn, **payload.model_dump(), user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.delete("/api/v1/dossier/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
    def remove_dossier_item(
        item_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        try:
            delete_dossier_item(conn, item_id, user_id=user_id)
        except DossierNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dossier item not found") from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.delete("/api/v1/dossier", status_code=status.HTTP_204_NO_CONTENT)
    def remove_all_dossier_data(
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> Response:
        delete_dossier_all(conn, user_id=user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/dossier/shares/preview")
    def preview_dossier_share(
        payload: DossierPreviewRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            items = share_preview(conn, payload.item_ids, user_id=user_id)
            return {"items": items, "total": len(items), "employer_visible_before_approval": False}
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/dossier/shares", status_code=status.HTTP_201_CREATED)
    def create_dossier_share(
        payload: DossierShareRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return create_share(conn, **payload.model_dump(), user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.delete("/api/v1/dossier/shares/{grant_id}")
    def revoke_dossier_share(
        grant_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return revoke_share(conn, grant_id, user_id=user_id)
        except DossierNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active share not found") from exc

    @app.get("/api/v1/public/dossier-shares/{share_token}")
    def public_dossier_share(share_token: str) -> dict[str, Any]:
        try:
            with closing(connect_product(database_target)) as conn:
                return read_share(conn, share_token)
        except DossierNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found, revoked, or expired") from exc

    @app.post("/api/v1/market/snapshots", status_code=status.HTTP_201_CREATED)
    def build_market_snapshot(
        payload: MarketSnapshotRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
    ) -> dict[str, Any]:
        try:
            return create_snapshot(conn, payload.as_of)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/market/snapshots/{snapshot_id}/verify")
    def verify_market_snapshot(
        snapshot_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
    ) -> dict[str, Any]:
        try:
            return verify_snapshot(conn, snapshot_id)
        except MarketNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found") from exc

    @app.post("/api/v1/market/issues", status_code=status.HTTP_201_CREATED)
    def draft_market_issue(
        payload: MarketIssueRequest,
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return create_issue(conn, **payload.model_dump(), user_id=user_id)
        except MarketNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found") from exc

    @app.get("/api/v1/market/issues/{issue_id}")
    def get_market_issue(
        issue_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
    ) -> dict[str, Any]:
        try:
            return issue_record(conn, issue_id)
        except MarketNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found") from exc

    @app.post("/api/v1/market/issues/{issue_id}/publish")
    def publish_market_issue(
        issue_id: str,
        conn: sqlite3.Connection = Depends(writable_connection),
    ) -> dict[str, Any]:
        try:
            return publish_issue(conn, issue_id)
        except MarketNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.get("/api/v1/public/market")
    def public_market_archive() -> dict[str, Any]:
        with closing(connect_product(database_target)) as conn:
            items = public_issues(conn)
        return {"items": items, "total": len(items)}

    @app.post("/api/v1/employer/organizations", status_code=status.HTTP_201_CREATED)
    def employer_create_organization(
        payload: OrganizationRequest,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            return create_organization(conn, payload.name, payload.organization_type, actor)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/employer/organizations/{organization_id}/requisitions", status_code=status.HTTP_201_CREATED)
    def employer_create_requisition(
        organization_id: str,
        payload: RequisitionRequest,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            return create_requisition(conn, organization_id, payload.title, payload.description, payload.rubric, actor)
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/employer/requisitions")
    def employer_requisitions(
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        items = list_requisitions(conn, actor)
        return {"items": items, "total": len(items)}

    @app.post("/api/v1/employer/requisitions/import", status_code=status.HTTP_201_CREATED)
    def employer_import_requisitions(
        payload: RequisitionImportRequest,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            items = import_requisitions(conn, payload.organization_id, payload.requisitions, actor)
            return {"items": items, "total": len(items), "format": "ats-json-v1"}
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/employer/requisitions/{requisition_id}")
    def employer_requisition(
        requisition_id: str,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            return requisition_record(conn, requisition_id, actor)
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found") from exc

    @app.get("/api/v1/employer/requisitions/{requisition_id}/export")
    def employer_export_requisition(
        requisition_id: str,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            return {"format": "ats-json-v1", "requisition": requisition_record(conn, requisition_id, actor)}
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found") from exc

    @app.get("/api/v1/employer/requisitions/{requisition_id}/candidates")
    def employer_candidates(
        requisition_id: str,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            items = list_candidates(conn, requisition_id, actor)
            return {"items": items, "total": len(items)}
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found") from exc

    @app.post("/api/v1/employer/requisitions/{requisition_id}/candidates", status_code=status.HTTP_201_CREATED)
    def employer_add_candidate(
        requisition_id: str,
        payload: CandidateShareRequest,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            return add_candidate_from_share(conn, requisition_id, payload.share_token, actor)
        except (EmployerNotFoundError, DossierNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition or active consent share not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/employer/candidates/{candidate_id}")
    def employer_candidate(
        candidate_id: str,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            return candidate_record(conn, candidate_id, actor)
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate or active consent not found") from exc

    @app.post("/api/v1/employer/candidates/{candidate_id}/decision")
    def employer_decide_candidate(
        candidate_id: str,
        payload: CandidateDecisionRequest,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            return decide_candidate(conn, candidate_id, payload.status, payload.reason, actor)
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate or active consent not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/v1/employer/candidates/{candidate_id}/agent-summary")
    def employer_agent_summary(
        candidate_id: str,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            candidate = candidate_record(conn, candidate_id, actor)
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate or active consent not found") from exc
        return {
            "candidate_id": candidate_id,
            "evidence_count": len(candidate["evidence"]),
            "rubric_score": candidate["score"],
            "summary": "This evidence-only summary does not make or recommend a hiring decision.",
            "human_decision_required": True,
        }

    @app.post("/api/v1/employer/candidates/{candidate_id}/messages", status_code=status.HTTP_201_CREATED)
    def employer_draft_message(
        candidate_id: str,
        payload: EmployerMessageRequest,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            return draft_candidate_message(conn, candidate_id, payload.body, actor)
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found") from exc

    @app.post("/api/v1/employer/messages/{message_id}/approve")
    def employer_approve_message(
        message_id: str,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        provider = build_notification_provider()
        deliver = None
        if provider.live:
            def deliver(message: dict[str, Any]) -> dict[str, Any]:
                return provider.deliver("email", "candidate@relay", "A recruiter message awaits your review", str(message.get("body", "")))
        try:
            return approve_candidate_message(conn, message_id, actor, deliver=deliver)
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post("/api/v1/employer/candidates/{candidate_id}/interviews", status_code=status.HTTP_201_CREATED)
    def employer_propose_interview(
        candidate_id: str,
        payload: EmployerInterviewRequest,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            return propose_interview(conn, candidate_id, payload.starts_at, payload.timezone, payload.location, actor)
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/api/v1/employer/interviews/{interview_id}/confirm")
    def employer_confirm_interview(
        interview_id: str,
        context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            return confirm_interview(conn, interview_id, actor)
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.get("/api/v1/admin/overview")
    def operations_overview(
        context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ) -> dict[str, Any]:
        conn, _actor = context
        overview = admin_overview(conn)
        requests = int(app_metrics["requests"])
        read_latencies = sorted(app_metrics["read_latency_ms"])
        write_latencies = sorted(app_metrics["write_latency_ms"])

        def p95(values: list[float]) -> float | None:
            if not values:
                return None
            index = max(0, (len(values) * 95 + 99) // 100 - 1)
            return round(float(values[index]), 3)

        read_p95 = p95(read_latencies)
        write_p95 = p95(write_latencies)
        error_rate = round(int(app_metrics["errors"]) / requests, 4) if requests else 0.0
        overview["service"] = {
            "requests": requests,
            "errors": int(app_metrics["errors"]),
            "error_rate": error_rate,
            "rate_limited": int(app_metrics["rate_limited"]),
            "average_latency_ms": round(float(app_metrics["latency_ms_total"]) / requests, 3) if requests else 0,
            "read_p95_ms": read_p95,
            "write_p95_ms": write_p95,
        }
        queue = queue_status(conn)
        overview["queue"] = queue
        alerts = []
        if error_rate > 0.02:
            alerts.append({"key": "api_error_rate", "severity": "critical", "value": error_rate, "threshold": 0.02})
        if read_p95 is not None and read_p95 > 750:
            alerts.append({"key": "read_p95_ms", "severity": "warning", "value": read_p95, "threshold": 750})
        if write_p95 is not None and write_p95 > 1_500:
            alerts.append({"key": "write_p95_ms", "severity": "warning", "value": write_p95, "threshold": 1_500})
        if queue["states"]["dead"]:
            alerts.append({"key": "dead_letter_jobs", "severity": "critical", "value": queue["states"]["dead"], "threshold": 0})
        if queue["backpressure"]:
            alerts.append({"key": "queue_backpressure", "severity": "critical", "value": True, "threshold": False})
        overview["slo"] = {
            "availability_target": 0.995,
            "read_p95_target_ms": 750,
            "write_p95_target_ms": 1_500,
            "queue_age_target_seconds": 600,
            "status": "alerting" if alerts else "within_observed_thresholds",
            "scope": "current process window; external durable telemetry required for monthly SLOs",
        }
        overview["alerts"] = alerts
        overview["recent_traces"] = list(recent_traces)[-50:]
        overview["product_analytics"] = {
            "application_events": int(conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0]),
            "apply_sessions": int(conn.execute("SELECT COUNT(*) FROM application_form_sessions").fetchone()[0]),
            "agent_turns": int(conn.execute("SELECT COUNT(*) FROM agent_turns").fetchone()[0]),
            "active_dossier_shares": int(conn.execute("SELECT COUNT(*) FROM dossier_consent_grants WHERE status='active'").fetchone()[0]),
            "contains_user_identifiers": False,
        }
        return overview

    @app.post("/api/v1/admin/jobs", status_code=status.HTTP_201_CREATED)
    def admin_enqueue_job(
        payload: JobCreateRequest,
        context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        record = enqueue_job(conn, payload.job_type, payload.payload, payload.idempotency_key, max_attempts=payload.max_attempts)
        from .employer import audit
        with conn:
            audit(conn, actor, "job_enqueued", "job", record["id"], {"job_type": payload.job_type})
        return record

    @app.post("/api/v1/admin/jobs/{job_id}/retry")
    def admin_retry_job(
        job_id: str,
        context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ) -> dict[str, Any]:
        conn, _actor = context
        try:
            return retry_dead_job(conn, job_id)
        except OperationsError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.post("/api/v1/admin/retention/run")
    def admin_run_retention(
        context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ) -> dict[str, int]:
        conn, _actor = context
        return run_retention(conn)

    @app.post("/api/v1/admin/organizations/{organization_id}/verify")
    def admin_verify_organization(
        organization_id: str,
        payload: OrganizationVerificationRequest,
        context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            return verify_organization(conn, organization_id, payload.approved, actor)
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found") from exc

    @app.put("/api/v1/admin/feature-flags/{key}")
    def admin_put_feature_flag(
        key: str,
        payload: FeatureFlagRequest,
        context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        return set_feature_flag(conn, key, payload.enabled, payload.description, actor)

    @app.get("/api/v1/admin/connector-health")
    def admin_connector_health(
        context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ) -> dict[str, Any]:
        conn, _actor = context
        return connector_health(conn)

    @app.put("/api/v1/admin/sources/{source_key:path}")
    def admin_source_control(
        source_key: str,
        payload: SourceControlRequest,
        context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        return set_source_control(conn, source_key, payload.enabled, payload.moderation_status, payload.note, actor)

    @app.post("/api/v1/admin/moderation", status_code=status.HTTP_201_CREATED)
    def admin_create_moderation(
        payload: ModerationCreateRequest,
        context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        return create_moderation_item(conn, payload.target_type, payload.target_id, payload.reason, actor)

    @app.post("/api/v1/admin/moderation/{item_id}/resolve")
    def admin_resolve_moderation(
        item_id: str,
        payload: ModerationResolveRequest,
        context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ) -> dict[str, Any]:
        conn, actor = context
        try:
            return resolve_moderation_item(conn, item_id, payload.status, payload.resolution, actor)
        except EmployerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Moderation item not found") from exc

    @app.get("/api/v1/admin/school-report")
    def admin_school_report(
        context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ) -> dict[str, Any]:
        conn, _actor = context
        return school_aggregate(conn)

    @app.get("/api/v1/facets")
    def facets(repo: OpportunityRepository = Depends(repository)) -> dict[str, list[str]]:
        return repo.facets()

    @app.get("/api/v1/stats")
    def stats(
        repo: OpportunityRepository = Depends(repository),
        conn: sqlite3.Connection = Depends(writable_connection),
        user_id: str = Depends(require_auth),
    ) -> dict[str, int]:
        # "tracked" counts any status past discovered, shortlisted roles included;
        # "applications" matches what the Applications view lists.
        applications = conn.execute("SELECT COUNT(*) FROM applications WHERE user_id=?", (user_id,)).fetchone()[0]
        return {**repo.stats(), "applications": int(applications)}

    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    @app.get("/employer", include_in_schema=False)
    @app.get("/admin", include_in_schema=False)
    def role_workspace() -> HTMLResponse:
        return versioned_page("ops.html")

    @app.get("/connections/oauth/{provider}/callback", include_in_schema=False)
    def oauth_callback_page(provider: Literal["google", "microsoft", "gmail_drafts"]) -> HTMLResponse:
        return versioned_page("oauth-callback.html")

    @app.get("/market", include_in_schema=False)
    def public_market_page() -> HTMLResponse:
        return versioned_page("market.html")

    @app.get("/", include_in_schema=False)
    @app.get("/urgent", include_in_schema=False)
    @app.get("/programs", include_in_schema=False)
    @app.get("/saved", include_in_schema=False)
    @app.get("/applications", include_in_schema=False)
    @app.get("/outreach", include_in_schema=False)
    @app.get("/prepare", include_in_schema=False)
    @app.get("/agent", include_in_schema=False)
    @app.get("/profile", include_in_schema=False)
    @app.get("/opportunities/{opportunity_id}", include_in_schema=False)
    def web_app(opportunity_id: str | None = None) -> HTMLResponse:
        return versioned_page("index.html")

    return app


app = create_app()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=DEFAULT_PLATFORM_DB)
    parser.add_argument("--database-url", help="PostgreSQL URL; overrides --db")
    parser.add_argument("--token", help="Local access token; defaults to PIPELINE_WEB_TOKEN or a generated value")
    parser.add_argument("--employer-token", help="Employer bearer token; defaults to PIPELINE_EMPLOYER_TOKEN")
    parser.add_argument("--admin-token", help="Admin bearer token; defaults to PIPELINE_ADMIN_TOKEN")
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Allow binding beyond loopback. Use only behind HTTPS and a trusted reverse proxy.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_network:
        raise SystemExit(
            "Refusing a non-loopback bind without --allow-network. "
            "The local session cookie is not configured for public HTTP."
        )
    configured_app = create_app(
        db_path=args.db,
        database_url=args.database_url,
        access_token=args.token,
        employer_token=args.employer_token,
        admin_token=args.admin_token,
        # A loopback server answers only to loopback host names, so a page on
        # another site cannot reach it by re-pointing its own DNS name here.
        allowed_hosts=None if args.allow_network else list(LOOPBACK_HOSTS),
    )
    print(f"Opportunity app: http://{args.host}:{args.port}")
    print(f"Access token: {configured_app.state.access_token}")
    print(f"Employer API token: {configured_app.state.employer_token}")
    print(f"Admin API token: {configured_app.state.admin_token}")
    uvicorn.run(configured_app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
