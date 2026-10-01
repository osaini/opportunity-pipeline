"""Request models for the employer surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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


class RequisitionImportRequest(BaseModel):
    organization_id: str = Field(min_length=1, max_length=200)
    requisitions: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class EmployerMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)


class EmployerInterviewRequest(BaseModel):
    starts_at: str = Field(min_length=1, max_length=80)
    timezone: str = Field(min_length=1, max_length=100)
    location: str = Field(default="", max_length=500)
