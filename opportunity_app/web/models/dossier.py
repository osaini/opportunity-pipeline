"""Request models for the career dossier."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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
