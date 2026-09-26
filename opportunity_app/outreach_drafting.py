"""Grounded cold email drafts for outreach targets.

A draft may only say what the student's confirmed profile or the target's
recorded research supports. The model is asked to cite a basis for every
factual claim, and a draft whose citations point anywhere else, or that states
a number found in neither source, is retried once and then refused. A draft
that passes is still only "generated": the student reviews and approves it
before the compose link unlocks, and nothing here sends mail.

Every draft a target has had is kept as a version, so a regenerated draft that
reads worse can be swapped back for an earlier one. The student's comments
steer a regeneration but are never a source: a draft still has to cite the
profile or the research for every fact.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any, Callable
from uuid import uuid4

from .agent_providers import AgentProvider, CliAgentProvider, complete_text, default_provider, provider_catalog
from .outreach import (
    AWAITING_REPLY, DRAFT_KINDS, _cancel_schedules, _log, draft_checks, get_target, home_terms, location_usable, mentions_home, near_home, student_home,
    user_regions,
)
from .preparation import confirmed_facts
from .schema import utc_now

ProviderFactory = Callable[[str, str], AgentProvider]

# Profile facts a cold email may draw on. Contact details, compensation, and
# work authorization stay out of a first email.
DRAFT_FACT_FIELDS = (
    "name", "school", "degree", "graduation_year", "summary", "skills", "interest_keywords",
    "preferred_role_types", "available_terms",
)
# Entries in these fields may carry "outreach": "lead", "support" (the default),
# or "omit". A cold email opens with a lead entry and never sees an omitted one.
PROOF_FIELDS = ("experience", "projects", "awards", "activities")
LINK_FIELDS = ("portfolio", "linkedin", "github")
RESEARCH_FIELDS = ("company", "website", "summary", "fit_rationale", "activity_signal", "contact_name", "contact_role")
# A cold email carries an opening, a two-sentence them + me = success bridge, and
# an ask, which lands near 150 words; a draft is refused 30 words past this.
MAX_WORDS = {"initial": 150, "follow_up": 80}
MAX_COMMENT_CHARS = 2_000
# Where each kind keeps its claims and provenance, beside DRAFT_KINDS' text fields.
DRAFT_META = {
    "initial": ("draft_claims_json", "draft_generated_by"),
    "follow_up": ("follow_up_claims_json", "follow_up_generated_by"),
}
# The bridge's one claim about what the student would contribute may rest on inference.
INFERENCE_BASIS = "inference"
FILLER_PHRASES = (
    "i would love to learn", "i'd love to learn", "i am especially interested", "i'm especially interested",
    "passionate", "i read that", "i came across", "my experience includes", "to whom it may concern",
    # Stock phrasing that reads as machine written in a cold email.
    "hope this email finds you", "i am writing to", "i'm writing to", "i am reaching out", "i'm reaching out",
    "thrilled", "excited to", "delve", "leverage", "showcas", "cutting-edge", "innovative", "aligns with",
    "align with", "is relevant to", "valuable", "not only",
    # Scale comparisons turn the bridge into a template.
    "smaller version of", "harder version of", "the same work",
    # A manner in place of a concrete contribution.
    "in a meaningful way",
    # College essay words that read as a cover letter in a cold email.
    "empower", "impactful", "uplift",
    # Saying the contrast outright instead of letting it show.
    "further than i could", "than i could alone", "than i could on my own", "couldn't take on by myself",
)
# Build docs, test data, and designs from an internship belong to that employer.
# Describing the work is fine; offering to hand it over is not.
OFFER_TO_SHARE_WORK = re.compile(
    r"\b(send|share|forward|attach|show)\w*\b[^.?!\n]{0,40}\b(docs?|documentation|data|designs?|drawings?|files?|cad|code|schematics?|notes)\b",
    re.IGNORECASE,
)

INSTRUCTIONS = """You write cold emails for one university student to a small company that may have no internship posting.

The student's own results carry the email. A founder or a shared jobs inbox should see in the first two lines that this student has already built real things, with numbers.

Follow this formula, in this order:
1. Subject: the student's most relevant proof plus the company, under 12 words, in the shape "[proof] at [school], interested in interning at [company]".
2. Greeting: the contact's first name when contact_name is given, otherwise "Hi" and the company team, using the name people call the company without Inc, Corp, Corporation, or LLC.
3. Opening, two sentences, in this shape:
   "I'm a [major] student at [school] [location_line] and [a or an] [role] at [primary_experience], where I [what the student built or did there]. I [result], [result], and [result]."
   - Who the student is, written the way a person says it: their major and school as they would say them aloud, not the degree's formal title.
   - If location_line is not empty, put it right after the school's name, exactly as written, parentheses included, and cite it with basis "profile:break_location". It tells a company near the student's home, before anything else, that they can be there in person. If location_line is empty, leave it out, and say nothing anywhere in the email about where the student lives or could work.
   - Their current role and the primary_experience entry, joined with "where I" to the thing the student built or did there.
   - The second sentence gives three results from the entry as one parallel list of verbs ("I cut..., expanded..., and extended..."), or two when the entry has only two. Prefer before-and-after results ("from 42 kg to 31 kg"), which show how much the student changed something, over totals and counts, and among those pick the ones that matter most to this company. Copy every number exactly as the input writes it.
   The whole email is built on this one experience. Never bring in another project, employer, or product of the student's by name anywhere in the email, because the reader has not met it and it would only confuse them. Say what the student did ("I cut its weight"), not what a platform or project did.
4. The bridge, about 50 words in two sentences, written the way a strong "why us" application essay reads: the student should clearly want this company, and the company should clearly want the student. The shape is fixed; the wording is not.
   a. Them, the first sentence: name the company's specific product or effort from the research the way you would name a program at a school (a product by name, a named study or pilot, a specific platform or service), never the company in general and never praise. Set it against what the student has built so far in the primary experience, so the reader sees this company as the next step past it: higher stakes, harder conditions, or a product that goes where the student's could not. Let the contrast between the two show that; never say it outright. Either side can come first, and the verbs are free; do not default to opening with "I've built". The student wants this company because it takes their own work somewhere new.
   b. Me = success, the second sentence: what the student would bring from the primary experience, the company's specific work it goes to, the concrete contribution, and how they would work there. In shape only: [the primary experience's work] to [their product or work], [a concrete action], [with their team].
      - What they bring: only work the primary experience's entry describes, named in the entry's own terms. Pick the part this company's product needs most. A tool or method from the student's skills may be named only as part of that same work, when the entry describes the work it was used for. Never pitch the student as a documentation writer; offer docs or guides only when documentation is itself what this company needs.
      - Their work: a product or effort the research names.
      - Contribution: a concrete action, in the kind of verbs the primary experience's entry and the student's skills already use for their own work, so a software student offers software work, a lab student offers lab work, and a builder offers building. When the entry gives no clear verbs, use plain, specific verbs for doing the work itself rather than supporting it. Avoid stock pairs like "design and test", and never a manner like "in a meaningful way" or "efficiently". This is the email's one inference; cite it with basis "inference". Keep it modest, never a promise or a claim to solve their problem.
      - With their team: end on one short clause about working with the people there, named in terms of this company's own team or work, such as the people running its pilot or the two founders building its product, and never the generic "alongside your engineers". One clause, never "team player" language.
   End the bridge on a statement, never a question. The ask is the email's only question.
   Keep it plain and grounded. No numbers in the bridge, and do not repeat the opening's results. No stock connectors that could drop into any email, like "that's the work I want to do", "is where I learned", or "I'd get to". Do not compare scale ("a smaller version of", "a harder version of", "the same work"), and never write "is relevant to" or "aligns with". No mission statements about helping communities or the world; this is an internship, not a cause. If the research names nothing concrete, work from the kind of product it describes rather than invent specifics.
5. Nothing else. The email stays on the primary experience, with no second proof and no list of skills.
6. The ask: would they consider the student as an intern for the earliest term in available_terms. The opening already said where the student is based, so do not repeat it here. If preferred_role_types includes part_time, add that the student is open to part-time work too, without listing every arrangement. Then close with the 15 minute call, and give it a purpose: "If you have 15 minutes, I'd like to hear how you..." followed by one short, specific thing only this company could tell the student about the work the bridge names. Write it as a statement, not a second question. When the research is too thin to name something sharp, just say the student is happy to talk for 15 minutes. Never offer to send or share documents, data, designs, or code from the student's work; they may belong to an employer.
7. Sign-off: the student's name on one line, then the sender address and every entry in links, joined with " | ".

An example of the shape, for a different, invented student and company whose field is unrelated to this student's. Match its length and directness, not its content, its field, its verbs, or its phrasing:
Subject: Battery pack builder at Georgia Tech, interested in interning at Voltworks
Hi Dana,
I'm an electrical engineering student at Georgia Tech (live in the Seattle area) and battery lead on our Formula SAE team, where I designed and built the car's pack. I cut pack mass from 42 kg to 31 kg, kept the cells under 45 C through a full endurance run, and brought charge time from 90 min to 40 min.
I've built a pack that only had to last one endurance run, and Voltworks is sealing packs inside boat hulls for years at a time. I'd bring the thermal work from our Formula SAE pack to your hull packs, wiring in thermocouples and bench testing cooling layouts alongside your engineers.
Would you consider me as an intern for summer 2027? I'm open to part-time work too. If you have 15 minutes, I'd like to hear how you get the heat out through the hull.
Sam Rivera
sam@gatech.edu | https://samrivera.dev

Rules:
- Use only facts in the JSON input. Never invent achievements, numbers, dates, mutual connections, deadlines, or anything about the company that the research does not say.
- Plain text. No markdown, no em dashes or en dashes, no [placeholders].
- Stay near max_words. Confident and direct, never gushing or apologetic.
- Sound like a student writing to a person, not a cover letter: use contractions (I'm, I've), mix short and longer sentences, and use plain verbs (is, has, built, cut). Write at the reading level of a plain text message, not a punchy, dramatic pitch.
- Never write "I would love to learn", "I am especially interested", "passionate", "I read that", "I came across", "I'm writing to", "I'm reaching out", "excited", "innovative", "cutting-edge", "leverage", "aligns with", or "valuable".
- No trailing -ing phrases that comment on a fact ("..., showing my ability to..."), no "not only X but Y", no lists of three adjectives, and no praise for the company.
- The sign-off's address and links are not claims; leave them out of claims.
- List every factual claim you make with its basis: "profile:<field>" for a student fact, "research:<field>" for a company fact, one of the research source URLs, or "inference" for the one connecting inference.
- unverified_research is unconfirmed deep-search text. It may inform the connecting sentence only when cited with its matching "unverified:<field>" basis.

Reply with exactly one JSON object and nothing else:
{"subject": "...", "body": "...", "claims": [{"text": "...", "basis": "..."}]}"""

FOLLOW_UP_INSTRUCTIONS = """You write a short follow-up to a cold email a university student already sent.

Rules:
- Use only facts in the JSON input. Never invent anything.
- Plain text, no markdown, no em dashes or en dashes, no [placeholders].
- Politely restate the ask in fewer words than the original. Do not repeat the whole original email.
- Sound like a person: contractions, plain words, no "just circling back", "hope this email finds you well", or "I'm reaching out".
- Sign off with the student's name.
- List every factual claim with its basis, exactly as in the original: "profile:<field>", "research:<field>", or a source URL.
- unverified_research is unconfirmed deep-search text and must use its matching "unverified:<field>" basis.

Reply with exactly one JSON object and nothing else:
{"subject": "...", "body": "...", "claims": [{"text": "...", "basis": "..."}]}"""


class DraftRejected(ValueError):
    """The model's draft cited or stated something the inputs do not support."""


class DraftVersionNotFoundError(LookupError):
    pass


def sender_account() -> str:
    return os.environ.get("PIPELINE_OUTREACH_ACCOUNT", "").strip()


def resolve_provider(requested: str | None = None) -> tuple[str, str]:
    """Pick the provider and model: explicit, then PIPELINE_OUTREACH_PROVIDER, then the agent default."""
    provider = (requested or os.environ.get("PIPELINE_OUTREACH_PROVIDER", "") or default_provider()).strip()
    if provider == "legacy":
        return "legacy", ""
    record = next((item for item in provider_catalog() if item["id"] == provider), None)
    if record is None:
        raise ValueError("Draft provider must be openai, anthropic, claude-code, codex-cli, or legacy")
    return provider, str(record["model"])


def _entry_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("organization") or entry.get("title") or entry.get("name") or "").strip()
    return str(entry).strip()


def outreach_proof(facts: dict[str, Any]) -> tuple[dict[str, list[Any]], list[str]]:
    """The experience, projects, awards, and activities outreach may use, and the entries to lead with.

    An entry marked "omit" is dropped here, so no model ever sees it.
    """
    proof: dict[str, list[Any]] = {}
    lead: list[str] = []
    for field in PROOF_FIELDS:
        value = facts.get(field)
        entries = value if isinstance(value, list) else [value] if value else []
        kept = []
        for entry in entries:
            use = entry.get("outreach", "support") if isinstance(entry, dict) else "support"
            if use == "omit":
                continue
            if use == "lead" and _entry_name(entry):
                lead.append(_entry_name(entry))
            kept.append({key: item for key, item in entry.items() if key != "outreach"} if isinstance(entry, dict) else entry)
        if kept:
            proof[field] = kept
    return proof, lead


def location_line(
    facts: dict[str, Any],
    target: dict[str, Any],
    regions: list[dict[str, Any]] | None = None,
) -> str:
    """The sentence saying the student lives near the company, or "" when none belongs.

    Every company where the student lives gets one: in their home region, or in
    their home city when that is not one of their regions (outreach.student_home).
    When the school is in the same region, the line says year-round. An
    unrecognized location gets none rather than a guess. Neither does a location
    only the deep search reported, until the company's site or a filing states
    it or the research is confirmed. ``regions`` defaults to the local owner's;
    pass ``user_regions`` for others.
    """
    if not location_usable(target):
        return ""
    home = student_home(facts, regions)
    if not near_home(str(target.get("location") or ""), home, regions):
        return ""
    return f"(live in {home['phrase']}{' year-round' if home['year_round'] else ''})"


def _inputs(conn: sqlite3.Connection, target: dict[str, Any], user_id: str, kind: str) -> dict[str, Any]:
    facts = confirmed_facts(conn, user_id)
    student = {field: facts[field] for field in DRAFT_FACT_FIELDS if field in facts}
    if not student.get("name"):
        raise ValueError("Confirm your name in your profile before generating a draft")
    proof, lead = outreach_proof(facts)
    student.update(proof)
    contact = facts["contact"] if isinstance(facts.get("contact"), dict) else {}
    research = {field: target[field] for field in RESEARCH_FIELDS if target.get(field)}
    if target.get("research_confidence") == "unverified":
        confirmed_research = {field: value for field, value in research.items() if field in {"company", "website"}}
        unverified_research = {field: value for field, value in research.items() if field not in {"company", "website"}}
    else:
        confirmed_research = research
        unverified_research = {}
    payload = {
        "student": student,
        "lead_with": lead,
        # The first lead entry carries the whole email; experience comes before projects.
        "primary_experience": lead[0] if lead else "",
        "sender_address": sender_account(),
        "links": [str(contact[field]).strip() for field in LINK_FIELDS if str(contact.get(field) or "").strip()],
        "company_research": confirmed_research,
        "unverified_research": unverified_research,
        "source_urls": target["source_urls"],
        "max_words": MAX_WORDS[kind],
        "suggested_ask": "whether they would consider an intern, or a 15 minute call",
    }
    if kind == "initial":
        payload["location_line"] = location_line(facts, target, user_regions(conn, user_id))
        if payload["location_line"]:
            student["break_location"] = facts["break_location"]
    if kind == "follow_up":
        payload["original_email"] = {"subject": target["email_subject"], "body": target["email_body"]}
        payload["sent_on"] = target.get("sent_at")
    return payload


def _allowed_bases(inputs: dict[str, Any]) -> set[str]:
    bases = {f"profile:{field}" for field in inputs["student"]}
    bases |= {f"research:{field}" for field in inputs["company_research"]}
    bases |= {f"unverified:{field}" for field in inputs["unverified_research"]}
    bases |= set(inputs["source_urls"]) | {INFERENCE_BASIS}
    if inputs["company_research"].get("website"):
        bases.add(inputs["company_research"]["website"])
    return bases


def _opening(body: str) -> str:
    """The email's introduction, where the student says who they are.

    That is the first paragraph, and the second as well when the greeting sits
    alone in the first: a model may or may not leave a blank line after it.
    """
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    if not paragraphs:
        return ""
    if "\n" not in paragraphs[0] and paragraphs[0].endswith(","):
        return "\n".join(paragraphs[:2])
    return paragraphs[0]


_NUMBER = re.compile(r"(?<![\w@.])\d[\d,.]*%?")
_ADDRESS = re.compile(r"\S+@\S+|https?://\S+")


def _numbers(text: str) -> set[str]:
    return {match.rstrip(".,") for match in _NUMBER.findall(text)} - {""}


def _entry_names(entry: Any) -> set[str]:
    """Every name a reader would recognize an entry by."""
    names = {_entry_name(entry)}
    if isinstance(entry, dict):
        names |= {str(entry.get(key) or "").strip() for key in ("organization", "name")}
    return {name for name in names if len(name) >= 3}


def _primary_entries(inputs: dict[str, Any]) -> list[Any]:
    primary = inputs.get("primary_experience")
    return [
        entry for field in PROOF_FIELDS for entry in inputs["student"].get(field, [])
        if primary and _entry_name(entry) == primary
    ]


def _states_a_lead_result(body: str, inputs: dict[str, Any]) -> bool:
    """Whether the body gives a number from the primary experience, not one that only belongs to the company."""
    research = {**inputs["company_research"], **inputs["unverified_research"]}
    lead_numbers = _numbers(json.dumps(_primary_entries(inputs), ensure_ascii=False)) - _numbers(json.dumps(research, ensure_ascii=False))
    return bool(lead_numbers & _numbers(_ADDRESS.sub(" ", body)))


def _other_entries_named(body: str, inputs: dict[str, Any]) -> list[str]:
    """Names of the student's other projects and employers, which the reader has not met."""
    primary = set().union(*(_entry_names(entry) for entry in _primary_entries(inputs)))
    if not primary:
        return []
    named: list[str] = []
    for field in PROOF_FIELDS:
        for entry in inputs["student"].get(field, []):
            for name in sorted(_entry_names(entry) - primary):
                if any(name.lower() in own.lower() for own in primary):
                    continue
                if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", body, re.IGNORECASE) and name not in named:
                    named.append(name)
    return named


def _unsupported_numbers(body: str, inputs: dict[str, Any]) -> list[str]:
    """Numbers in the draft that appear nowhere in its inputs."""
    haystack = json.dumps(inputs, ensure_ascii=False)
    without_addresses = _ADDRESS.sub(" ", body)
    found = []
    for match in _NUMBER.findall(without_addresses):
        token = match.rstrip(".,")
        if token and token not in haystack and token not in found:
            found.append(token)
    return found


_FIELD_BASIS = re.compile(r"^((?:profile|research|unverified):\w+)[\[.]")


def _field_basis(basis: str) -> str:
    """Reduce a basis that points inside a field, like profile:experience[0].title, to the field itself."""
    match = _FIELD_BASIS.match(basis)
    return match.group(1) if match else basis


def validate_draft(
    raw: str,
    inputs: dict[str, Any],
    kind: str,
    regions: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Parse a model reply and list every reason it cannot be stored as-is."""
    try:
        parsed = CliAgentProvider.extract_json(raw)
    except ValueError as exc:
        raise DraftRejected("The model did not return a draft") from exc
    subject = str(parsed.get("subject") or "").strip()
    body = str(parsed.get("body") or "").replace("\r\n", "\n").strip()
    claims = parsed.get("claims") if isinstance(parsed.get("claims"), list) else []
    problems: list[str] = []
    if not subject or not body:
        problems.append("the reply is missing a subject or body")
    allowed = _allowed_bases(inputs)
    clean_claims = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        basis = _field_basis(str(claim.get("basis") or "").strip())
        text = str(claim.get("text") or "").strip()[:500]
        if basis not in allowed:
            problems.append(f"the claim {text[:80]!r} cites {basis or 'nothing'}, which is not in the inputs")
        clean_claims.append({"text": text, "basis": basis})
    if body and not clean_claims:
        problems.append("it cites no basis for any of its claims")
    numbers = _unsupported_numbers(body, inputs)
    if numbers:
        problems.append("it states numbers found in neither your profile nor the research: " + ", ".join(numbers))
    if kind == "initial":
        if inputs.get("primary_experience") and not _states_a_lead_result(body, inputs):
            problems.append("it gives no concrete number from its primary lead_with entry (" + inputs["primary_experience"] + ")")
        others = _other_entries_named(body, inputs)
        if others:
            problems.append(
                "it names " + ", ".join(others) + ", which the reader has not met; keep the email on " + inputs["primary_experience"]
            )
        if inputs.get("location_line"):
            home = student_home(inputs["student"], regions)
            line = inputs["location_line"]
            opening = " ".join(_opening(body).split()).casefold()
            if " ".join(line.split()).casefold() not in opening:
                where = "later on" if mentions_home(body, home_terms(home)) else "nowhere"
                problems.append(
                    f"it leaves out location_line: it says where you live {where} instead of the opening's {line!r}, "
                    "which goes right after the school's name, exactly as written"
                )
        inferences = sum(claim["basis"] == INFERENCE_BASIS for claim in clean_claims)
        if inferences > 1:
            problems.append(f"it rests {inferences} claims on inference, but only the bridge's one claim about what you'd contribute may")
    filler = [phrase for phrase in FILLER_PHRASES if phrase in body.lower()]
    if filler:
        problems.append("it uses filler the formula bans: " + ", ".join(repr(phrase) for phrase in filler))
    offer = OFFER_TO_SHARE_WORK.search(body)
    if offer:
        problems.append(f"it offers to hand over work material ({offer.group(0)!r}), which may belong to an employer")
    checks = draft_checks(subject, body)
    if kind == "initial" and body.count("?") > 1:
        problems.append("it asks more than one question; the ask is the only question, and the call carries the rest")
    if checks["dash_count"]:
        problems.append("it uses em or en dashes")
    if checks["placeholders"]:
        problems.append("it leaves placeholders: " + ", ".join(checks["placeholders"]))
    if checks["word_count"] > MAX_WORDS[kind] + 30:
        problems.append(f"it runs {checks['word_count']} words, over the {MAX_WORDS[kind]} word target")
    return {"subject": subject, "body": body, "claims": clean_claims}, problems


def template_draft(inputs: dict[str, Any], kind: str) -> dict[str, Any]:
    """Deterministic fallback when no model provider is configured."""
    student = inputs["student"]
    research = inputs["company_research"]
    company = research.get("company", "your team")
    first_name = str(research.get("contact_name", "")).split(" ")[0]
    greeting = f"Hi {first_name}," if first_name else f"Hi {company} team,"
    claims = [{"text": f"I'm {student['name']}", "basis": "profile:name"}]
    contact_line = " | ".join(part for part in (inputs.get("sender_address", ""), *inputs.get("links", [])) if part)
    signature = "\n".join(part for part in (student["name"], contact_line) if part)
    if kind == "follow_up":
        original = inputs["original_email"]
        subject = original["subject"] if original["subject"].lower().startswith("re:") else f"Re: {original['subject']}"
        body = (
            f"{greeting}\n\nI wanted to follow up on my note below about internship opportunities at {company}. "
            f"I would still welcome the chance to talk if the timing works.\n\nThank you,\n{signature}"
        )
        return {"subject": subject, "body": body, "claims": claims}
    study = ""
    nearby = f" {inputs['location_line']}" if inputs.get("location_line") else ""
    if nearby:
        claims.append({"text": inputs["location_line"], "basis": "profile:break_location"})
    if student.get("degree") and student.get("school"):
        study = f", studying {student['degree']} at {student['school']}{nearby}"
        claims += [{"text": student["degree"], "basis": "profile:degree"}, {"text": student["school"], "basis": "profile:school"}]
    elif nearby:
        study = nearby
    lead = next(
        (
            (field, entry) for field in PROOF_FIELDS for entry in student.get(field, [])
            if isinstance(entry, dict) and _entry_name(entry) in inputs["lead_with"] and entry.get("highlights")
        ),
        None,
    )
    skills = [str(skill) for skill in student.get("skills", [])][:3] if isinstance(student.get("skills"), list) else []
    skill_sentence = ""
    if lead:
        field, entry = lead
        highlight = str(entry["highlights"][0]).rstrip(".")
        skill_sentence = f" At {_entry_name(entry)}, I {highlight[:1].lower()}{highlight[1:]}."
        claims.append({"text": highlight, "basis": f"profile:{field}"})
    elif skills:
        skill_sentence = f" I work with {', '.join(skills)}."
        claims.append({"text": ", ".join(skills), "basis": "profile:skills"})
    reason = ""
    if research.get("summary"):
        reason = f" I read about {company}'s work and would like to learn more about it."
        claims.append({"text": f"{company}'s work", "basis": "research:summary"})
    body = (
        f"{greeting}\n\nI'm {student['name']}{study}.{reason}{skill_sentence}\n\n"
        f"Would {company} consider taking on an intern? I would welcome a 15 minute call to learn about your team.\n\n"
        f"Thank you,\n{signature}"
    )
    subject = f"Internship inquiry from {student['name']}"
    return {"subject": subject, "body": body, "claims": claims}


def revision_request(target: dict[str, Any], kind: str, comments: str) -> str:
    """The student's comments on the current draft, appended after the grounded inputs.

    Kept out of the inputs on purpose: the grounding checks compare the reply
    against the inputs, and neither the comments nor an edited draft is a source.
    """
    subject_field, body_field, _ = DRAFT_KINDS[kind]
    parts = [
        "The student reviewed the current draft and asked for changes. Follow their comments wherever they do not "
        "break a rule. The comments and the current draft are not sources: take every fact from the JSON input above, "
        "and never state a number, achievement, or company fact that appears only in the comments or the current draft."
    ]
    if target.get(body_field):
        parts.append(f"Current draft:\nSubject: {target.get(subject_field, '')}\n\n{target[body_field]}")
    parts.append(f"The student's comments:\n{comments}")
    return "\n\n" + "\n\n".join(parts)


def _insert_version(
    conn: sqlite3.Connection, target_id: str, user_id: str, kind: str, *, source: str,
    subject: str, body: str, claims_json: str, generated_by: str, comments: str, created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO outreach_draft_versions(id, target_id, user_id, kind, source, subject, body, claims_json, generated_by, comments, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (f"draft-version-{uuid4().hex}", target_id, user_id, kind, source, subject, body, claims_json, generated_by, comments, created_at),
    )


def _keep_current_draft(conn: sqlite3.Connection, target_id: str, user_id: str, kind: str) -> None:
    """Store the draft in the editor before it is replaced, unless a version already holds that text.

    This is what keeps a hand-edited draft, or one written before versions were
    kept, from being lost to a regeneration.
    """
    subject_field, body_field, _ = DRAFT_KINDS[kind]
    claims_field, generated_field = DRAFT_META[kind]
    row = conn.execute(
        f"SELECT {subject_field}, {body_field}, {claims_field}, {generated_field}, updated_at "
        "FROM outreach_targets WHERE id=? AND user_id=?",
        (target_id, user_id),
    ).fetchone()
    if not row or not (row[0] or row[1]):
        return
    kept = conn.execute(
        "SELECT 1 FROM outreach_draft_versions WHERE target_id=? AND user_id=? AND kind=? AND subject=? AND body=?",
        (target_id, user_id, kind, row[0], row[1]),
    ).fetchone()
    if kept:
        return
    _insert_version(
        conn, target_id, user_id, kind, source="saved", subject=row[0], body=row[1],
        claims_json=row[2] or "[]", generated_by=row[3] or "", comments="", created_at=row[4],
    )


def draft_versions(conn: sqlite3.Connection, target_id: str, *, user_id: str, kind: str) -> list[dict[str, Any]]:
    """Every stored draft of one kind, oldest first, marking the one in the editor now."""
    if kind not in DRAFT_KINDS:
        raise ValueError("kind must be initial or follow_up")
    target = get_target(conn, target_id, user_id=user_id)
    subject_field, body_field, _ = DRAFT_KINDS[kind]
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, kind, source, subject, body, claims_json, generated_by, comments, created_at
        FROM outreach_draft_versions WHERE target_id=? AND user_id=? AND kind=?
        ORDER BY created_at, id
        """,
        (target_id, user_id, kind),
    ).fetchall()
    versions = []
    for row in rows:
        version = dict(row)
        version["claims"] = json.loads(version.pop("claims_json") or "[]")
        version["is_current"] = version["subject"] == target[subject_field] and version["body"] == target[body_field]
        versions.append(version)
    return versions


def restore_draft_version(conn: sqlite3.Connection, target_id: str, version_id: str, *, user_id: str) -> dict[str, Any]:
    """Put an earlier draft back in the editor, keeping the one it replaces.

    The restored draft goes back to "generated": it needs approval again, like
    any draft whose words changed.
    """
    target = get_target(conn, target_id, user_id=user_id)
    conn.row_factory = sqlite3.Row
    version = conn.execute(
        "SELECT * FROM outreach_draft_versions WHERE id=? AND target_id=? AND user_id=?",
        (version_id, target_id, user_id),
    ).fetchone()
    if not version:
        raise DraftVersionNotFoundError(version_id)
    kind = version["kind"]
    subject_field, body_field, status_field = DRAFT_KINDS[kind]
    claims_field, generated_field = DRAFT_META[kind]
    if target[subject_field] == version["subject"] and target[body_field] == version["body"]:
        return target
    timestamp = utc_now()
    assignments: dict[str, Any] = {
        subject_field: version["subject"], body_field: version["body"], status_field: "generated",
        claims_field: version["claims_json"], generated_field: version["generated_by"],
    }
    if kind == "initial":
        assignments.update(draft_generated_at=version["created_at"], draft_approved_at=None)
        if target["status"] == "not_started":
            assignments["status"] = "drafted"
    label = "draft" if kind == "initial" else "follow-up"
    with conn:
        _keep_current_draft(conn, target_id, user_id, kind)
        conn.execute(
            f"UPDATE outreach_targets SET {', '.join(f'{column}=?' for column in assignments)}, updated_at=? WHERE id=? AND user_id=?",
            [*assignments.values(), timestamp, target_id, user_id],
        )
        if target[status_field] == "approved":
            _log(conn, target_id, user_id, "approval_withdrawn", detail=f"An earlier {label} was restored")
            _cancel_schedules(conn, target_id, user_id, [kind], f"An earlier {label} was restored after you scheduled it")
        _log(conn, target_id, user_id, "draft_restored" if kind == "initial" else "follow_up_restored",
             detail=f"Restored the {label} from {version['created_at']}")
        if assignments.get("status"):
            _log(conn, target_id, user_id, "status", from_status=target["status"], to_status="drafted")
    return get_target(conn, target_id, user_id=user_id)


def generate_draft(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    provider_factory: ProviderFactory,
    kind: str = "initial",
    provider: str | None = None,
    comments: str = "",
) -> dict[str, Any]:
    if kind not in DRAFT_KINDS:
        raise ValueError("kind must be initial or follow_up")
    comments = comments.replace("\r\n", "\n").strip()
    if len(comments) > MAX_COMMENT_CHARS:
        raise ValueError(f"Keep comments under {MAX_COMMENT_CHARS:,} characters")
    target = get_target(conn, target_id, user_id=user_id)
    if kind == "follow_up":
        if target["status"] not in AWAITING_REPLY:
            raise ValueError("A follow-up needs an email marked sent first")
        if not target["email_body"]:
            raise ValueError("A follow-up needs the original email text")
    inputs = _inputs(conn, target, user_id, kind)
    regions = user_regions(conn, user_id)
    provider_id, model = resolve_provider(provider)

    if provider_id == "legacy":
        if comments:
            raise ValueError("The template drafter cannot follow comments. Clear them, or configure a model provider.")
        draft = template_draft(inputs, kind)
        generated_by = "template"
    else:
        agent = provider_factory(provider_id, model)
        instructions = INSTRUCTIONS if kind == "initial" else FOLLOW_UP_INSTRUCTIONS
        content = json.dumps(inputs, ensure_ascii=False, indent=2)
        if comments:
            content += revision_request(target, kind, comments)
        raw = complete_text(agent, instructions, content)
        draft, problems = validate_draft(raw, inputs, kind, regions)
        if problems:
            retry = (
                f"{content}\n\nYour previous draft was rejected because " + "; ".join(problems)
                + ". Write it again following every rule."
            )
            raw = complete_text(agent, instructions, retry)
            draft, problems = validate_draft(raw, inputs, kind, regions)
        if problems:
            raise DraftRejected("The generated draft was not grounded in your profile and research: " + "; ".join(problems))
        generated_by = f"{provider_id}:{model}"

    subject_field, body_field, status_field = DRAFT_KINDS[kind]
    claims_field, generated_field = DRAFT_META[kind]
    timestamp = utc_now()
    claims_json = json.dumps(draft["claims"], ensure_ascii=kind != "follow_up")
    assignments: dict[str, Any] = {
        subject_field: draft["subject"], body_field: draft["body"], status_field: "generated",
        claims_field: claims_json, generated_field: generated_by,
    }
    if kind == "initial":
        assignments.update(draft_generated_at=timestamp, draft_approved_at=None)
        if target["status"] == "not_started":
            assignments["status"] = "drafted"
    with conn:
        _keep_current_draft(conn, target_id, user_id, kind)
        _insert_version(
            conn, target_id, user_id, kind, source="generated", subject=draft["subject"], body=draft["body"],
            claims_json=claims_json, generated_by=generated_by, comments=comments, created_at=timestamp,
        )
        conn.execute(
            f"UPDATE outreach_targets SET {', '.join(f'{column}=?' for column in assignments)}, updated_at=? WHERE id=? AND user_id=?",
            [*assignments.values(), timestamp, target_id, user_id],
        )
        if target[status_field] == "approved":
            _log(conn, target_id, user_id, "approval_withdrawn", detail=f"The {kind.replace('_', '-')} draft was regenerated")
            _cancel_schedules(conn, target_id, user_id, [kind], "The draft was regenerated after you scheduled it")
        _log(conn, target_id, user_id, "draft_generated" if kind == "initial" else "follow_up_generated", detail=generated_by)
        if assignments.get("status"):
            _log(conn, target_id, user_id, "status", from_status=target["status"], to_status="drafted")
    return get_target(conn, target_id, user_id=user_id)
