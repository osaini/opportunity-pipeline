"""Request models for the student agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AgentThreadRequest(BaseModel):
    title: str = Field(default="Career planning", max_length=200)
    provider: Literal["openai", "anthropic", "claude-code", "codex-cli", "legacy"] | None = None


class AgentMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class AgentDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
