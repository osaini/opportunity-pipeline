"""Deterministic opportunity attributes used by filters and detail views."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


TERM_RE = re.compile(r"\b(spring|summer|fall|autumn|winter)\s*(20\d{2})?\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(20(?:2[4-9]|3\d))\b")
HOURLY_PAY_RE = re.compile(
    r"\$\s*(\d{1,3}(?:\.\d{1,2})?)\s*(?:-|–|—|to)?\s*"
    r"(?:\$\s*)?(\d{1,3}(?:\.\d{1,2})?)?\s*(?:/|per\s+)(?:hour|hr)\b",
    re.IGNORECASE,
)
YEARLY_PAY_RE = re.compile(
    r"\$\s*(\d{2,3}(?:,\d{3})+)\s*(?:-|–|—|to)?\s*"
    r"(?:\$\s*)?(\d{2,3}(?:,\d{3})+)?\s*(?:/|per\s+)?(?:year|yr|annually|annual)?",
    re.IGNORECASE,
)


def extract_deadline(text: str) -> str | None:
    match = re.search(
        r"\b(?:apply\s+by|deadline|applications?\s+(?:close|due))\s*[:\-]?\s*"
        r"([A-Z][a-z]+\s+\d{1,2},?\s+20\d{2}|20\d{2}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/20\d{2})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    value = match.group(1).replace(",", "")
    for pattern in ("%B %d %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def extract_opportunity_metadata(title: str, location: str, description: str) -> dict[str, Any]:
    text = "\n".join((title or "", location or "", description or ""))
    lowered_location = (location or "").lower()
    lowered_text = text.lower()
    if "hybrid" in lowered_location or re.search(r"\bhybrid\b", lowered_text):
        remote_mode = "hybrid"
    elif "remote" in lowered_location or re.search(r"\b(remote|work from home)\b", lowered_text):
        remote_mode = "remote"
    elif location.strip():
        remote_mode = "onsite"
    else:
        remote_mode = "unknown"

    terms: list[str] = []
    for season, year in TERM_RE.findall(text):
        normalized = "fall" if season.lower() == "autumn" else season.lower()
        value = f"{normalized} {year}".strip()
        if value not in terms:
            terms.append(value)

    eligibility_context = " ".join(
        match.group(0)
        for match in re.finditer(
            r".{0,45}\b(?:class of|graduat(?:e|es|ing|ion)|student|year)\b.{0,45}",
            text,
            re.IGNORECASE,
        )
    )
    graduation_years = sorted({int(value) for value in YEAR_RE.findall(eligibility_context)})

    pay_min: float | None = None
    pay_max: float | None = None
    pay_period = ""
    hourly = HOURLY_PAY_RE.search(text)
    if hourly:
        pay_min = float(hourly.group(1))
        pay_max = float(hourly.group(2) or hourly.group(1))
        pay_period = "hour"
    else:
        yearly = YEARLY_PAY_RE.search(text)
        if yearly:
            pay_min = float(yearly.group(1).replace(",", ""))
            pay_max = float((yearly.group(2) or yearly.group(1)).replace(",", ""))
            pay_period = "year"

    return {
        "remote_mode": remote_mode,
        "terms": terms,
        "graduation_years": graduation_years,
        "pay_min": pay_min,
        "pay_max": pay_max,
        "pay_period": pay_period,
        "currency": "USD" if pay_min is not None else "",
        "deadline_at": extract_deadline(text),
    }
