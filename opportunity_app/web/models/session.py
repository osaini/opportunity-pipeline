"""Request and response models for sign-in, registration and recovery."""

from __future__ import annotations

from pydantic import BaseModel, Field


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
