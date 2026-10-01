"""Request models for the administrator surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class FeatureFlagRequest(BaseModel):
    enabled: bool
    description: str = Field(default="", max_length=1_000)


class OrganizationVerificationRequest(BaseModel):
    approved: bool


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
