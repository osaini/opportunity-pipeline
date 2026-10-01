"""Request models for connected accounts, monitored events, notifications and phone verification."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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
