"""The questions a student asks on every call, in their own words.

Some questions belong in every call: what the project they would work on is
for, where the company is heading, where it is short of people. Each student
phrases them their own way, often leading with something from their own work,
so they live in the student's gitignored ``config/call_prep.local.json``
(SETUP.md step "Call prep questions"), never in shipped code:

    {"questions": [
      {"lead_in": "At my last internship, my manager set the use case for our project, and it guided my design choices.",
       "ask": "What's the use case of the project I'd be working on?",
       "research": ["customers", "product"],
       "blank": "Use case of the project:"}
    ]}

- ``ask`` is the question; ``lead_in`` (optional) is said first, word for word.
- ``research`` names the research sections (outreach_research.SECTIONS) to
  have ready for it; call prep points to them, or says nothing was found.
- ``blank`` (optional) is a line under DURING THE CALL to write the answer on.

Without the file, call prep asks DEFAULT_QUESTIONS: the same four topics in
plain words, with no lead-ins, since a lead-in is the student's own story.

The shipped example file has <placeholders> in a lead-in for the student to
fill in. One left as it was is not their words: the lead-in is dropped, the
question is still asked, and the notes say the file needs finishing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import ROOT
from .outreach_research import SECTION_IDS

QUESTIONS_PATH = ROOT / "config" / "call_prep.local.json"
MAX_QUESTIONS = 8
MAX_CHARS = 400
# A <placeholder> from config/call_prep.local.example.json that was never filled in.
_PLACEHOLDER = re.compile(r"<[^<>]+>")

DEFAULT_QUESTIONS: tuple[dict[str, Any], ...] = (
    {"lead_in": "", "ask": "What's the use case of the project I'd be working on?",
     "research": ["customers", "product"], "blank": "Use case of the project:"},
    {"lead_in": "", "ask": "Where are you looking to expand?", "research": ["growth"], "blank": "Where they're expanding:"},
    {"lead_in": "", "ask": "As an intern, how could I help you ship faster?",
     "research": ["hiring", "engineering"], "blank": "How I can help them move faster:"},
    {"lead_in": "", "ask": "Where are you short-staffed?", "research": ["hiring"], "blank": "Where they're short-staffed:"},
)


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())[:MAX_CHARS]


def standing_questions(path: Path | None = None) -> tuple[list[dict[str, Any]], str]:
    """The student's questions, or the defaults, and a note when their file could not be used."""
    path = path or QUESTIONS_PATH
    if not path.exists():
        return [dict(item) for item in DEFAULT_QUESTIONS], ""
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("questions")
    except (OSError, ValueError, AttributeError):
        return [dict(item) for item in DEFAULT_QUESTIONS], f"{path.name} could not be read, so the default questions are used"
    questions = []
    unfinished = set()
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not _clean(entry.get("ask")):
            continue
        lead_in, ask, blank = _clean(entry.get("lead_in")), _clean(entry.get("ask")), _clean(entry.get("blank"))[:120]
        # Never printed or sent to a model as the student's own words.
        for name, value in (("lead-in", lead_in), ("question", ask), ("blank", blank)):
            if _PLACEHOLDER.search(value):
                unfinished.add(name)
        if _PLACEHOLDER.search(ask):
            continue
        research = entry.get("research") if isinstance(entry.get("research"), list) else []
        questions.append({
            "lead_in": "" if _PLACEHOLDER.search(lead_in) else lead_in,
            "ask": ask,
            "research": [section for section in research if section in SECTION_IDS],
            "blank": "" if _PLACEHOLDER.search(blank) else blank,
        })
    note = ""
    if unfinished:
        note = f"{path.name} still has <placeholders> in a {', a '.join(sorted(unfinished))}; fill it in or remove it"
    if not questions:
        return [dict(item) for item in DEFAULT_QUESTIONS], note or f"{path.name} lists no questions, so the default questions are used"
    return questions[:MAX_QUESTIONS], note
