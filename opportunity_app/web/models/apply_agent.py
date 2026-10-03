"""Request models for Apply for me."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApplyAnswerRequest(BaseModel):
    key: str = Field(min_length=1, max_length=200)
    answer: str | list[str] = Field(max_length=10_000)
    reusable: bool = False
    posting_confirmed: bool = False


class ApplyLabelRequest(BaseModel):
    label: str = Field(min_length=1, max_length=200)


class ApplySensitiveAnswerRequest(BaseModel):
    # The Needs you form: only the student's answer and the tick. The category, wording and options come from the form.
    key: str = Field(min_length=1, max_length=200)
    answer: str | list[str] | bool
    consent: bool = False
    any_company: bool = False
    posting_confirmed: bool = False


class ApplySensitiveEntryRequest(BaseModel):
    # The settings page: an entry added without a form in hand, so the student gives the exact question and the category.
    category: str = Field(min_length=1, max_length=40)
    question: str = Field(min_length=1, max_length=4_000)
    answer: str | list[str] = Field(default="", max_length=2_000)
    answer_kind: str = Field(default="", max_length=20)
    company: str = Field(default="", max_length=200)
    links: list[str] = Field(default_factory=list, max_length=8)
    consent: bool = False


class ApplySensitiveCategoriesRequest(BaseModel):
    categories: list[str] = Field(max_length=20)


class ApplyLookupRequest(BaseModel):
    # The text the student typed into one typeahead, to be looked up on the form. It is sent to Greenhouse's lookup service only.
    model_config = ConfigDict(str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100)


class ApplyRehearsalRequest(BaseModel):
    # The body is optional. A form that does not look like the saved role is rehearsed only when the student has said it is the right posting.
    posting_confirmed: bool = False


class ApplyReviewRequest(BaseModel):
    verdict: Literal["right", "wrong"]
    note: str = Field(default="", max_length=500)

class ApplyClaimResolveRequest(BaseModel):
    # The card's answer for an attempt that may have reached Greenhouse: It went through, or It didn't go through.
    went_through: bool
