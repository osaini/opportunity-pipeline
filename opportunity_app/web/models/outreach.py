"""Request models for outreach targets, drafts, sends, replies, discovery, settings and the Gmail label."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from ...outreach.drafting import MAX_COMMENT_CHARS as MAX_DRAFT_COMMENT_CHARS


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
    call_prep: str | None = Field(default=None, max_length=20_000)
    contact_evidence_url: str | None = Field(default=None, max_length=500)
    # Who the call is with, when it is not the person emailed (call prep reads
    # their LinkedIn); the tracker checks the link is a linkedin.com/in/ page.
    interviewer_name: str | None = Field(default=None, max_length=200)
    interviewer_linkedin: str | None = Field(default=None, max_length=300)
    # PATCH only: the student vouches for a location nothing has checked yet.
    # It carries the location the page showed, so a place that changed between
    # the render and the click is refused instead of silently confirmed. A bare
    # bool still parses, so the refusal is an explanatory 422 from the tracker
    # rather than a shapeless validation error; the length bound belongs to the
    # string arm, because on the union it is applied to a bool too and raises.
    confirm_location: Annotated[str, Field(max_length=200)] | bool | None = None
    # True files the company under Not interested (kept, and left alone by automation); False moves it back.
    not_interested: bool | None = None


class OutreachDraftRequest(BaseModel):
    kind: Literal["initial", "follow_up"] = "initial"
    comments: str = Field(default="", max_length=MAX_DRAFT_COMMENT_CHARS)


class OutreachApprovalRequest(BaseModel):
    kind: Literal["initial", "follow_up"] = "initial"
    acknowledge_warnings: bool = False
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class OutreachThankYouSendRequest(BaseModel):
    # The thank-you the card showed; one changed since is not sent.
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    # As for OutreachSendRequest: the check a 428 answer named, once the student has looked in Gmail.
    sent_folder_check: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class OutreachSendRequest(BaseModel):
    kind: Literal["initial", "follow_up"] = "initial"
    # The approved draft the student confirmed; a draft changed since is not sent.
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The check a 428 answer named, sent back once the student has looked in
    # Gmail. It vouches for exactly the reasons that answer gave.
    sent_folder_check: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class OutreachFormSubmitRequest(BaseModel):
    # The approved draft the student confirmed; a draft changed since is not sent.
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The student looked and the earlier, unconfirmed send did not arrive.
    retry_unconfirmed: bool = False
    # Open a browser window the student can see, to solve a CAPTCHA themselves.
    in_browser: bool = False


class OutreachContactFormRequest(BaseModel):
    page_url: str = Field(min_length=8, max_length=2_000)


class OutreachManualContactRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=200)
    evidence_url: str = Field(default="", max_length=500)
    confirmed: bool = False


class OutreachReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    # The student says a text that reads like a bounce notice is a real reply.
    as_reply: bool = False


class PossibleReplyDecisionRequest(BaseModel):
    # Whether an email kept as a possible reply is one (outreach_inbox.decide_possible_reply).
    decision: Literal["reply", "not_reply"]


class OutreachAutomationRequest(BaseModel):
    auto_drafts: bool | None = None
    bounce_recovery: bool | None = None
    bounce_auto_resend: bool | None = None
    scheduled_sending: bool | None = None
    follow_up_review: bool | None = None
    form_submission: bool | None = None


class OutreachScheduleRequest(BaseModel):
    kind: Literal["initial", "follow_up"] = "initial"
    fingerprint: str = Field(min_length=1, max_length=128)


class OutreachBounceRequest(BaseModel):
    # The delivery failure notice the student pasted, if any.
    text: str = Field(default="", max_length=20_000)


class OutreachDiscoveryRequest(BaseModel):
    scopes: list[Literal["local-accelerators", "us-startups", "recently-funded"]] | None = Field(default=None, max_length=3)


class OutreachSettingsRequest(BaseModel):
    draft_provider: str | None = Field(default=None, max_length=40)
    follow_up_provider: str | None = Field(default=None, max_length=40)
    call_prep_provider: str | None = Field(default=None, max_length=40)
    thank_you_provider: str | None = Field(default=None, max_length=40)
    review_provider: str | None = Field(default=None, max_length=40)
    research_agent: str | None = Field(default=None, max_length=40)
    company_research_agent: str | None = Field(default=None, max_length=40)
    linkedin_account: str | None = Field(default=None, max_length=300)
    attachment_resume_id: str | None = Field(default=None, max_length=100)


class GmailLabelRequest(BaseModel):
    # None goes back to the default label name; an empty string turns the label off.
    value: str | None = Field(default=None, max_length=100)


class RecontactChoice(BaseModel):
    target_id: str = Field(min_length=1, max_length=100)
    to: str = Field(min_length=3, max_length=320)


class RecontactApplyRequest(BaseModel):
    choices: list[RecontactChoice] = Field(min_length=1, max_length=500)
    redraft: bool = False
