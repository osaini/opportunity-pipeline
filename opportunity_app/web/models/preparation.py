"""Request models for documents, the answer library and mock interviews."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DocumentCreateRequest(BaseModel):
    opportunity_id: str = Field(min_length=1, max_length=500)
    document_type: Literal["resume", "cover_letter"]
    # Any provider the Preparation page offers, subscriptions included.
    provider: Literal["openai", "anthropic", "claude-code", "codex-cli"] | None = None


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
