"""Request models for the automation ledger."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AutomationFeatureKey = Annotated[str, Field(min_length=1, max_length=64)]


AutomationNoticeId = Annotated[str, Field(min_length=1, max_length=100)]


class AutomationSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Each key is an automation feature; an unknown key is refused with 422.
    modes: dict[AutomationFeatureKey, Literal["off", "shadow", "on"]] | None = Field(default=None, max_length=50)
    paused: bool | None = None


class AutomationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["right", "wrong"]


class AutomationApproveRequest(BaseModel):
    """Optional: the application the student chose instead of the proposed one."""

    model_config = ConfigDict(extra="forbid")

    subject_id: str | None = Field(default=None, min_length=1, max_length=200)


class AutomationNoticesReadRequest(BaseModel):
    """Either the ids of the notices to mark read, or ``all`` for every unread notice; not both."""

    model_config = ConfigDict(extra="forbid")

    ids: list[AutomationNoticeId] | None = Field(default=None, max_length=100)
    all: bool = False

    @model_validator(mode="after")
    def ids_or_all(self) -> "AutomationNoticesReadRequest":
        if self.all == (self.ids is not None):
            raise ValueError("Send either ids or all: true")
        return self
