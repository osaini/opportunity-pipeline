"""Research on one company, from the web, for the student's call with them.

The deep search saves a sentence about what a company does. That is enough to
decide whether to write to them, and too little to talk to them: a student on a
call wants to understand the company's work. What the product is and who buys
it, what they say sets them apart and who they compete with, how it works and
what they build it with, where they are expanding and hiring, who built it and
what they worked on before, and where the company stands.

A headless CLI with web search and fetch does the reading (the research agent
chosen in Outreach settings) and answers with facts, each tied to the page that
states it and the words on that page that state it. Nothing it says is taken on
its word. A fact is kept in two steps.

First, Python opens every cited page, and the fact goes on only when:

- the page loads, and is not a search results page, LinkedIn, or a data broker;
- the quote is on the page, word for word once case and punctuation are set
  aside (a quote trimmed with "..." is matched piece by piece, in order);
- every number (with its unit: 4.5M is not 4.5 billion, 5 kg is not 5 lb) and
  every name in the fact is in the quote or the lines around it, and so are
  most of its other words;
- the page names the company in its visible text, or its link to the company's
  website does, or it names a person that another kept fact about the company's
  team ties to it (a founder's paper or thesis rarely names the company).

Word checks can prove the words are there, not what they mean. So second, a
separate read of the page's own passage (a fresh call that never sees the
agent's reasoning, and is not a different model unless the provider it uses
says so) takes the fact beside a passage Python itself cut
from the page (the quote's paragraph, the lines around it, and the top of the
page) and says whether the passage states exactly that: the same company,
product, or person, the same numbers on the same things, nothing swapped, and
the same "not" (JUDGE_INSTRUCTIONS). Only a fact it confirms is kept as checked.
Without that second read (no model, or it failed), a fact whose words passed is
kept marked "not checked", like one from a page nobody could read, and nothing
is built on it.

A competitor fact is about another company, so its passage must name that
competitor (or the page is the competitor's own site). Whether the two compete
is the research agent's judgment unless the quote's own sentence names both and
uses a word of competition, and the second read, which is asked about exactly
that, finds the two selling against each other (not a customer, partner, or
investor). The brief says which (the fact's note).

What passes is "quote found on the page, and a second read confirms the fact
says what it says". It is not a judgment that the page is right.

The company's own site sometimes turns automated readers away (403, 429). A
fact from its own site is then kept marked "not checked" wherever it is shown,
and is never handed to the model that writes call prep questions. From any
other site that turns readers away, the fact is left out. Redirects are
followed only to pages this check would read, and a page counts as the
company's own site by where it ended up, not by the link that was cited. A fact that fails a
check is left out too, with the reason kept for the student to see. A new run
that checks fewer facts than a brief still fresh never replaces it. What the
research looked for and could not find is kept as the agent's list of gaps:
not facts, but questions worth asking on the call.

The brief is stored on the target with when and by which agent it was written,
and when research last started. Call prep (outreach_call_prep.py) is built from
it and refreshes it first when it is missing or older than FRESH_FOR, but never
more than once a day for one company. The Research this company button and
``outreach_cli research`` write it for any company.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from .agent_providers import cli_available, cli_binary, complete_text
from .operations import enqueue_job
from .contact_names import website_domain
from .outreach import CALL_PREP_STATUSES, OutreachNotFoundError, log_event, company_key, get_target
from .outreach_agents import RUNNERS, Runner
from .outreach_config import COMPANY_RESEARCH_ENV, RESEARCH_ENV, resolve_provider
from .preparation import confirmed_facts
from .quote_check import SECTION_IDS, Judge, check_brief
from .timestamps import parse_app_instant, utc_now
from .web_fetch import SafeFetcher

JOB_TYPE = "outreach_company_research"
MAX_ATTEMPTS = 3
ACTIVE_JOB_STATES = {"queued", "running", "retry"}
# Codex's read-only sandbox can still read files on this computer, and research reads pages nobody vetted.
# Company research therefore never runs on Codex unless the student says, in .env, that they accept that.
ALLOW_CODEX_ENV = "PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX"
# One company, read closely: longer than a location search, far shorter than a deep search.
RUNNER_TIMEOUT_SECONDS = 20 * 60
# Call prep researches again when the brief is older than this.
FRESH_FOR = timedelta(days=30)
# A try that failed or kept nothing waits this long before call prep tries again.
RETRY_AFTER = timedelta(days=1)
# A research job asked for while call prep, running now, has just started one for the same company is not queued.
CALL_PREP_RESEARCH_WINDOW = timedelta(hours=1)

PROMPT = """You are researching one company for a university student who has a call with them soon. The student wants to understand the company's actual work and its market, not a sales pitch: what they build and who buys it, what they say sets them apart and who they compete with, how it works and what they build it with, where they are expanding and hiring, who built it, and where the company stands. Write only about the company and its competitors. Do not write anything about the student.

## Company
{company}
Website: {website}
On file so far: {summary}
Pages already on file:
{urls}
{contact}
## The student
{student}
Dig deepest into the work closest to the student's field, but cover the whole company.

## Where to look
Start with the company's own site: product pages, specifications, customer and case study pages, documentation, blog posts, and careers pages and job posts (these name the tools and methods they use and the roles they need). Then look beyond it: patents (patents.google.com), papers and theses by the founders and staff (arXiv, Google Scholar, university pages; they go in team, since they speak for the person, not the company), grant awards (sbir.gov, nsf.gov, nih.gov), GitHub, conference talks, accelerator and investor pages, funding announcements, news, and the sites of the companies that sell something similar to the same customers.

## Sections
- product: what they sell or are building, and its specifications as published (size, speed, capacity, accuracy, price, and the like).
- customers: who they sell to: the industries, kinds of customers, and use cases they name, customers by name, and case studies. This is their ideal customer as the pages describe it.
- edge: what the company says sets it apart (the first, the only, faster, cheaper, more precise), in its own words, and what technology it credits for that.
- competitors: companies that sell something similar to the same customers. Each fact is about one competitor, from a page that names it (a comparison or news page that names both companies is best, the competitor's own site is fine), and says what that competitor sells or how it differs. Put the competitor's name in "competitor", and quote a sentence that names it.
- technology: how it works: the approach, the science or engineering behind it, methods, materials, software, and any patents or papers behind it.
- engineering: what they build it with: languages, frameworks, tools, equipment, platforms, and methods, from job posts, docs, and GitHub; and what open roles say the team needs. Only facts that name a tool, a method, or a skill belong here; leave it empty rather than fill it with funding or history.
- growth: where they say they are expanding: new markets, products, customers, locations, production, and what new funding will pay for.
- hiring: open roles and what they say the team needs, with where and when they are posted. Many openings in one area are a sign of where they are short-staffed.
- team: founders and leads, their roles, and their backgrounds (degrees, labs, earlier companies, earlier work), as the pages state them.
- traction: funding rounds (amount, lead investor, date), grants, pilots, partners, and awards.
- news: dated milestones from about the last two years: launches, deployments, hires, and announcements.

## Rules
- Use web search and fetch. Only report what a page you actually opened in this session says. Never answer from memory.
- Each fact has one source_url: the page that states it. Never cite a search results page, LinkedIn, Crunchbase, PitchBook, ZoomInfo, or another login or data-broker site. For a paper or patent, cite its HTML page (the arXiv abstract page, the Google Patents page), not the PDF.
- quote: the words on that page that state the fact, copied exactly, 8 to 40 words. When you fetch a page, ask for the exact sentences word for word. The student's software opens the page and looks for the quote word for word; a fact whose quote is not on the page, or that says more than its quote and the lines around it, is thrown away.
- text: the fact as a short note the student will copy out by hand, under 18 words: fragments are fine ("X2 arm: $4,500; swappable grippers, 3 kg payload"), and every specific stays. Copy numbers, units, and names exactly as the page writes them, and do not add anything the quote does not say. Prefer concrete detail (specifications, methods, materials, languages and tools, customers and investors by name, grant titles) over marketing language.
- person: the full name of the one person a fact is about (a founder's background, a hire), as the page writes it; empty otherwise. The quote must be the sentence or heading that names that person, not a sentence beside it. A fact from a page that is not about the company (a paper, a thesis, a lab page, an earlier company's page) belongs in team only, its text must name that person, and that person must also appear in another team fact from a page that does name the company. Anything else in a section other than team must come from a page that names the company.
- Up to 5 facts per section. Fewer true, specific facts are better than more vague ones. Leave a section empty rather than guess.
- gaps: up to 8 things about the company's work the student would want to know that no page you found states, as short phrases with no numbers (for example "which suppliers they use for key parts"). The student will ask about them on the call.
- Plain text, no markdown, no em dashes or en dashes.

## Output
Reply with exactly one JSON object and nothing else:
{{"facts": [{{"section": "one of: {sections}", "text": "", "source_url": "https://...", "quote": "", "person": "", "competitor": ""}}], "gaps": [""]}}"""


CLAUDE_AGENT, CODEX_AGENT = "claude-code", "codex-cli"
class ResearchUnavailable(RuntimeError):
    """No research agent is set up on this computer, or in this app."""


def research_agent() -> str:
    """The CLI that researches companies: its own setting, else the deep search's, else Claude Code."""
    for env in (COMPANY_RESEARCH_ENV, RESEARCH_ENV):
        chosen = os.environ.get(env, "").strip()
        if chosen in RUNNERS:
            return chosen
    return "claude-code"


def _codex_allowed() -> bool:
    return os.environ.get(ALLOW_CODEX_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def available_agent(preferred: str | None = None) -> tuple[str, str]:
    """The agent to run and a note when it is not the one chosen, because that one is not installed.

    Company research reads web pages, which are not trusted, and Codex's read-only
    sandbox can still read files on this computer. Claude Code runs here with only
    web search and fetch, so it is preferred, and Codex runs only when the student
    has allowed it (ALLOW_CODEX_ENV) and Claude Code is not installed.
    """
    chosen = preferred or research_agent()
    claude_here = cli_available(cli_binary(CLAUDE_AGENT))
    if chosen == CODEX_AGENT and claude_here:
        return CLAUDE_AGENT, f"{CODEX_AGENT} can read files on this computer, so {CLAUDE_AGENT} (web search and fetch only) did the research"
    candidates = [chosen, *(other for other in RUNNERS if other != chosen)]
    for agent in candidates:
        if agent == CODEX_AGENT and not _codex_allowed():
            continue
        if cli_available(cli_binary(agent)):
            return agent, "" if agent == chosen else f"{chosen} is not installed here, so {agent} did the research"
    if not claude_here and cli_available(cli_binary(CODEX_AGENT)):
        raise ResearchUnavailable(
            "Only Codex CLI is installed here, and it can read files on this computer while it reads web pages. "
            f"Install Claude Code, or set {ALLOW_CODEX_ENV}=1 in .env to accept that."
        )
    raise ResearchUnavailable(
        "No research agent is set up on this computer. Install and sign in to Claude Code or Codex CLI."
    )


def research_runner(agent: str) -> Runner:
    run = RUNNERS[agent]
    return lambda prompt: run(prompt, timeout=RUNNER_TIMEOUT_SECONDS)


def _student_line(conn: sqlite3.Connection, user_id: str) -> str:
    """The student's field from their confirmed profile, so each student's research leans their way."""
    facts = confirmed_facts(conn, user_id)
    parts = []
    if facts.get("degree"):
        parts.append(f"Studies {facts['degree']}.")
    interests = [str(term) for term in facts.get("interest_keywords") or []][:8]
    if interests:
        parts.append(f"Interested in {', '.join(interests)}.")
    return " ".join(parts) or "A university student."


def build_prompt(target: dict[str, Any], student: str) -> str:
    urls = [url for url in [target.get("website"), *(target.get("source_urls") or [])] if url]
    contact = ""
    if target.get("contact_name"):
        role = f", {target['contact_role']}" if target.get("contact_role") else ""
        contact = f"The student will probably talk to {target['contact_name']}{role}. Cover their background in team.\n"
    return PROMPT.format(
        company=target["company"],
        website=target.get("website") or "(not on file; find it)",
        summary=target.get("summary") or "(nothing yet)",
        urls="\n".join(f"- {url}" for url in dict.fromkeys(urls)) or "- (none)",
        contact=contact,
        student=student,
        sections=", ".join(SECTION_IDS),
    )


def brief_of(target: dict[str, Any]) -> dict[str, Any]:
    """The stored brief, or an empty one."""
    brief = target.get("tech_brief") or {}
    return brief if isinstance(brief, dict) else {}


def company_changed(started: dict[str, Any], company: Any, website: Any) -> bool:
    """Whether the company or website now on file is not the one a run started from.

    Judged as outreach._research_reset judges it: a change of case, punctuation, or
    legal ending is the same company, and so is a first website for one that had none.
    """
    if company_key(str(company or "")) != company_key(str(started.get("company") or "")):
        return True
    before = website_domain(started.get("website") or "")
    return bool(before) and website_domain(str(website or "")) != before


def _checked_facts(brief: dict[str, Any]) -> list[dict[str, Any]]:
    return [fact for fact in brief.get("facts") or [] if isinstance(fact, dict) and fact.get("checked")]


def brief_is_fresh(target: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether the brief is recent and has a checked fact. One that kept none, or only facts not checked, is worth trying again."""
    written = parse_app_instant(target.get("tech_brief_at")) if target.get("tech_brief_at") else None
    if written is None or not _checked_facts(brief_of(target)):
        return False
    return (now or datetime.now(timezone.utc)) - written < FRESH_FOR


def research_due(target: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether call prep should research the company first.

    Not when the brief is fresh, not when a try started in the last day (a
    retry of the same job, or a try that failed or kept nothing), and not when
    a research job is already on its way.
    """
    now = now or datetime.now(timezone.utc)
    if brief_is_fresh(target, now):
        return False
    tried = parse_app_instant(target.get("tech_brief_tried_at")) if target.get("tech_brief_tried_at") else None
    if tried is not None and now - tried < RETRY_AFTER:
        return False
    return (target.get("tech_brief_job") or {}).get("state") not in ACTIVE_JOB_STATES


def research_company(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    runner: Runner,
    fetcher: SafeFetcher,
    renderer: Any = None,
    agent: str = "",
    note: str = "",
    judge: Judge | None = None,
) -> dict[str, Any]:
    """Research one company now and store what its pages back up. Returns the target.

    A run that checks fewer facts than a brief still fresh never replaces it
    (a site that turned the readers away this time leaves facts "not checked"):
    the earlier brief stays, and the error says why. Once the earlier brief is
    stale, a new one replaces it however thin, so old facts do not linger.
    """
    target = get_target(conn, target_id, user_id=user_id)
    if not target["company"]:
        raise ValueError("The company needs a name before it can be researched")
    with conn:
        conn.execute("UPDATE outreach_targets SET tech_brief_tried_at=? WHERE id=? AND user_id=?", (utc_now(), target_id, user_id))
    try:
        raw = runner(build_prompt(target, _student_line(conn, user_id)))
        brief = check_brief(raw, target, fetcher=fetcher, renderer=renderer, judge=judge)
    except Exception as exc:
        record_error(conn, target_id, user_id=user_id, error=exc)
        raise
    timestamp = utc_now()
    kept = len(brief["facts"])
    summary = f"{kept} fact{'s' if kept != 1 else ''} kept, {len(brief['refused'])} left out"
    with conn:
        row = conn.execute(
            "SELECT tech_brief_json, company, website FROM outreach_targets WHERE id=? AND user_id=?", (target_id, user_id),
        ).fetchone()
        if row is None:
            raise OutreachNotFoundError(target_id)
        if company_changed(target, row[1], row[2]):
            # Renamed while the agent worked: what it checked describes the old company, and the rename already cleared the old brief.
            conn.execute(
                "UPDATE outreach_targets SET tech_brief_error=?, updated_at=? WHERE id=? AND user_id=?",
                ("The company or its website changed while it was being researched, so that research was not kept.", timestamp, target_id, user_id),
            )
            log_event(conn, target_id, user_id, "tech_brief_failed", detail="The company changed during research")
            return get_target(conn, target_id, user_id=user_id)
        earlier = json.loads(row[0] or "{}")
        new_checked, old_checked = len(_checked_facts(brief)), len(_checked_facts(earlier))
        earlier_fresh = brief_is_fresh({"tech_brief_at": target.get("tech_brief_at"), "tech_brief": earlier})
        if new_checked < old_checked and (earlier_fresh or not new_checked):
            conn.execute(
                "UPDATE outreach_targets SET tech_brief_error=?, updated_at=? WHERE id=? AND user_id=?",
                (
                    f"The new research checked {new_checked} fact{'s' if new_checked != 1 else ''} "
                    f"({kept - new_checked} not checked, {len(brief['refused'])} left out), fewer than the {old_checked} "
                    "in the brief on file, so the earlier brief stays.",
                    timestamp, target_id, user_id,
                ),
            )
            log_event(conn, target_id, user_id, "tech_brief_failed", detail=f"Kept none: {summary}")
            return get_target(conn, target_id, user_id=user_id)
        if note:
            brief["note"] = note
        conn.execute(
            """
            UPDATE outreach_targets
            SET tech_brief_json=?, tech_brief_at=?, tech_brief_by=?, tech_brief_error='', updated_at=?
            WHERE id=? AND user_id=?
            """,
            (json.dumps(brief, ensure_ascii=False), timestamp, agent, timestamp, target_id, user_id),
        )
        log_event(conn, target_id, user_id, "tech_brief_written", detail=summary)
    return get_target(conn, target_id, user_id=user_id)


def record_error(conn: sqlite3.Connection, target_id: str, *, user_id: str, error: Exception) -> None:
    """Keep why the last try failed on the company, where the Research tab and call prep show it."""
    message = " ".join(str(error).split())[:1_000] or type(error).__name__
    with conn:
        conn.execute(
            "UPDATE outreach_targets SET tech_brief_error=?, updated_at=? WHERE id=? AND user_id=?",
            (message, utc_now(), target_id, user_id),
        )
        log_event(conn, target_id, user_id, "tech_brief_failed", detail=message[:500])


def text_model(provider_factory: Callable[[str, str], Any], provider: str | None, fallback: str) -> Judge:
    """The model for the second read: the call prep writer (outreach_config.resolve_provider), or,
    when that is the no-AI template, the research agent's own CLI, which research needs anyway.

    The second read is a separate call on the page's own passage, not a different model: when the
    writer is the same provider as the research agent, the same model reads it, and the notes say
    "a separate read", never "a model that did not write it"."""
    from .agent_providers import provider_catalog

    provider_id, model = resolve_provider(provider, purpose="call_prep")
    if provider_id == "legacy":
        record = next((item for item in provider_catalog() if item["id"] == fallback), None)
        provider_id, model = fallback, str((record or {}).get("model") or "")
    agent = provider_factory(provider_id, model)
    return lambda instructions, content: complete_text(agent, instructions, content, max_output_tokens=3_000)


def web_researcher(
    fetcher_factory: Callable[[], SafeFetcher], renderer_factory: Callable[[], Any] = lambda: None,
    provider_factory: Callable[[str, str], Any] | None = None, provider: str | None = None,
) -> Callable[[sqlite3.Connection, str, str], dict[str, Any]]:
    """Research one company with the agent chosen in settings, looked up each time so a change needs no restart.

    ``provider_factory`` builds the model for the second read; without it every
    fact is kept "not checked".
    """

    def research(conn: sqlite3.Connection, target_id: str, user_id: str) -> dict[str, Any]:
        try:
            agent, note = available_agent()
        except ResearchUnavailable as exc:
            with conn:
                conn.execute("UPDATE outreach_targets SET tech_brief_tried_at=? WHERE id=? AND user_id=?", (utc_now(), target_id, user_id))
            record_error(conn, target_id, user_id=user_id, error=exc)
            raise
        with ExitStack() as stack:
            fetcher = stack.enter_context(fetcher_factory())
            renderer = renderer_factory()
            if renderer is not None:
                stack.enter_context(renderer)
            judge = text_model(provider_factory, provider, agent) if provider_factory is not None else None
            return research_company(
                conn, target_id, user_id=user_id, runner=research_runner(agent), fetcher=fetcher,
                renderer=renderer, agent=agent, note=note, judge=judge,
            )

    def problem() -> str:
        """Why this researcher cannot run on this computer now (no research CLI), or ""."""
        try:
            available_agent()
        except ResearchUnavailable as exc:
            return str(exc)
        return ""

    # The app asks before it offers the Research button or queues a job that could only fail.
    research.problem = problem  # type: ignore[attr-defined]
    return research


def queue_research(conn: sqlite3.Connection, target_id: str, *, user_id: str, reason: str) -> dict[str, Any]:
    """Queue a background job to research one company, unless one is already on its way.

    The job is recorded on the company only if no other job got there first, so
    two tabs pressing at once leave one job, and the other is cancelled unrun.
    """
    target = get_target(conn, target_id, user_id=user_id)
    if not company_key(target["company"]):
        raise ValueError("The company needs a name before it can be researched")
    job = target.get("tech_brief_job")
    if job and job["state"] in ACTIVE_JOB_STATES:
        return target
    tried = parse_app_instant(target.get("tech_brief_tried_at")) if target.get("tech_brief_tried_at") else None
    if (
        (target.get("call_prep_job") or {}).get("state") == "running"
        and tried is not None and datetime.now(timezone.utc) - tried < CALL_PREP_RESEARCH_WINDOW
    ):
        # Call prep is researching this company right now, on this very try: a second run would search it again.
        return target
    queued = enqueue_job(
        conn, JOB_TYPE, {"target_id": target_id, "user_id": user_id},
        f"company-research:{target_id}:{uuid4().hex}", max_attempts=MAX_ATTEMPTS,
    )
    with conn:
        claimed = conn.execute(
            """
            UPDATE outreach_targets SET tech_brief_job_id=?, updated_at=?
            WHERE id=? AND user_id=? AND (tech_brief_job_id IS NULL OR tech_brief_job_id NOT IN (
                SELECT id FROM job_queue WHERE state IN ('queued', 'running', 'retry')
            ))
            """,
            (queued["id"], utc_now(), target_id, user_id),
        ).rowcount
        if claimed:
            log_event(conn, target_id, user_id, "tech_brief_queued", detail=reason)
        else:
            conn.execute("UPDATE job_queue SET state='cancelled', updated_at=? WHERE id=?", (utc_now(), queued["id"]))
    return get_target(conn, target_id, user_id=user_id)


def due_for_research(conn: sqlite3.Connection, *, user_id: str, only_replied: bool, now: datetime | None = None) -> list[str]:
    """Targets whose brief is missing, stale, or empty: those that replied, or every one."""
    rows = conn.execute(
        "SELECT id, status, tech_brief_at, tech_brief_json FROM outreach_targets WHERE user_id=? AND not_interested_at IS NULL "
        "ORDER BY company COLLATE NOCASE",
        (user_id,),
    ).fetchall()
    return [
        row[0] for row in rows
        if (not only_replied or row[1] in CALL_PREP_STATUSES)
        and not brief_is_fresh({"tech_brief_at": row[2], "tech_brief": json.loads(row[3] or "{}")}, now)
    ]
