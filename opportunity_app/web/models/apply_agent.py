"""Request models for Apply for me."""

from __future__ import annotations

from pydantic import BaseModel, Field


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
