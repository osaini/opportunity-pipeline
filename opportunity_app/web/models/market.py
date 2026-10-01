"""Request models for market snapshots and issues."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MarketSnapshotRequest(BaseModel):
    as_of: str | None = Field(default=None, max_length=80)


class MarketIssueRequest(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=200)
