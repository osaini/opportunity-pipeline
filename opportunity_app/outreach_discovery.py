"""Twice-weekly deep search for companies worth cold outreach.

Each scope is its own search, run one after another. A single search asked to
cover every scope stopped well short of its limit and spent most of its effort
on the broadest scope, so the narrowest scope came back with two to four
companies a run. Each later search is told what the earlier ones found, and a
scope that fails does not lose the others.

A headless Claude Code run with only web search and web fetch proposes
companies as JSON. Python does every write, and only after checking the
proposal: each company needs a working website, a summary, a fit rationale, and
at least one source URL that actually loads. Proposals that fail are recorded
with the reason and never imported. Accepted companies are added (never
overwriting an existing target), their own sites are searched for published
contacts, and a draft is generated when a confirmed address turns up. The
same crawl, and an SEC Form D lookup when one is configured, record where the
company is based with the page or filing that says so (outreach_profile.py).
Every draft still waits for the student's approval; nothing is sent.

A company is never proposed twice: tracked companies, companies the student
deleted, and companies the student already has an application with are all
rejected, and recent rejections go back into the prompt with their reasons. An
open posting is not a rejection -- the student applies to it and still writes.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
from contextlib import ExitStack, closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import httpx

from pipeline import SOURCES_LOCAL_PATH

from . import ROOT
from .agent_providers import CliAgentProvider, _cli_binary
from .outreach import (
    OUTREACH_PRIORITIES,
    _log,
    company_key,
    existing_keys,
    get_target,
    import_targets,
    local_today,
    location_usable,
    website_domain,
)
from .outreach_contacts import FetchResult, SafeFetcher, apply_choice, choose_contact, default_fetcher, find_contacts, list_candidates
from .outreach_drafting import outreach_proof
from .outreach_profile import SecUnavailableError, form_d_lookup, record_form_d, render_site_location, sec_fetcher
from .outreach_render import PlaywrightRenderer, default_renderer
from .preparation import confirmed_facts
from .schema import connect_product, utc_now

# Briefs are templates filled from the student's confirmed profile, so every
# student searches their own regions and fields. A student can replace any
# scope's brief (or label) under "outreach_scopes" in config/sources.local.json,
# for example to name the specific accelerator programs in their city.
SCOPES = {
    "local-accelerators": {
        "label": "Accelerator startups near you",
        "channel": "Local accelerators",
        "brief": (
            "Early-stage companies in the portfolios of incubators and accelerators in or near {regions}: "
            "university incubators, city and state innovation programs, and similar programs."
        ),
    },
    "us-startups": {
        "label": "US startups in your field",
        "channel": "US startups",
        "brief": (
            "Early-stage US startups whose work matches the student's interests ({interests}), from "
            "Y Combinator, Techstars, and similar programs."
        ),
    },
    "recently-funded": {
        "label": "Recently funded companies",
        "channel": "Recently funded",
        "brief": (
            "Startups in the student's field that announced a seed or Series A round in roughly the last six "
            "months and are growing their team, but have no internship posted."
        ),
    },
}
SCOPE_OVERRIDE_FIELDS = ("label", "brief")


def scope_definitions() -> dict[str, dict[str, str]]:
    """SCOPES with the student's overrides from config/sources.local.json applied."""
    try:
        overrides = json.loads(SOURCES_LOCAL_PATH.read_text(encoding="utf-8")).get("outreach_scopes") or {}
    except (OSError, ValueError, AttributeError):
        overrides = {}
    definitions = {key: dict(value) for key, value in SCOPES.items()}
    for key, override in overrides.items():
        if key in definitions and isinstance(override, dict):
            definitions[key].update(
                {field: str(override[field]) for field in SCOPE_OVERRIDE_FIELDS if override.get(field)}
            )
    return definitions


def _scope_brief(template: str, facts: dict[str, Any]) -> str:
    regions = [str(region.get("name")) for region in facts.get("regions") or [] if isinstance(region, dict) and region.get("name")]
    places = regions or [str(place) for place in facts.get("preferred_locations") or []]
    if facts.get("break_location"):
        places.append(str(facts["break_location"]))
    interests = [str(term) for term in facts.get("interest_keywords") or []][:8]
    return template.replace(
        "{regions}", ", ".join(dict.fromkeys(places)) or "the student's school and preferred locations",
    ).replace(
        "{interests}", ", ".join(interests) or "see the profile above",
    )


DEFAULT_SCOPES = tuple(SCOPES)
# Companies one scope's search may propose, and the most a caller may ask for.
MAX_PER_SCOPE = 10
MAX_TARGETS = 25
RUNNER_TIMEOUT_SECONDS = 45 * 60
SCHEDULED_MIN_GAP = timedelta(hours=48)
# Three scopes at up to 45 minutes each, plus contacts and drafts.
LOCK_STALE_AFTER = timedelta(hours=4)
REPORT_DIR = ROOT / "data"
# Pages that refuse automated checks without being gone.
UNVERIFIABLE_STATUSES = {401, 403, 405, 429, 999}

Runner = Callable[[str], str]

PROMPT = """You are researching companies a university student could cold email about an internship. The student will email them directly because these companies have no internship posting.

## Student (confirmed profile facts)
{student}

## What to look for
{scopes}

## Already tracked (do not propose these, or any company at the same website)
{existing}

## Removed by the student (never propose these)
{dismissed}

## Rejected in recent runs (propose one again only if you can fix the reason given)
{rejected}

## Rules
- Use web search and fetch to verify every company. Only propose companies you found on a real page during this session.
- Every company needs its own website and at least one source URL you actually opened: its own site, the accelerator or program portfolio page, or a funding announcement.
- location is the city and state where the company is based, as a page states it, for example "San Francisco, CA". Use its headquarters, or its engineering site when a page says that is somewhere else. Leave it empty if no page says.
- activity_signal must say what shows the company is active or growing and when, for example a funding announcement or a product launch, with the date as stated on the page.
- fit_rationale explains in one or two sentences why this student in particular fits, using only the profile facts above.
- contact_name and contact_role only when a page names that person, with contact_source_url set to that page. Never guess or include email addresses.
- deadline_label only if a page states one; otherwise leave it empty. Never invent deadlines.
- Prefer small teams (under about 100 people) whose work matches the student's degree and interests.
- Propose at most {limit} companies. Fewer well-verified companies are better than more guesses.
- priority: P1 for a strong fit with a clear recent activity signal, P2 for a good fit, P3 for a stretch.

## Output
Reply with exactly one JSON object and nothing else:
{{"companies": [{{"company": "", "website": "https://...", "scope": "one of: {scope_ids}", "location": "City, ST", "summary": "what they build", "fit_rationale": "", "activity_signal": "", "priority": "P1|P2|P3", "source_urls": ["https://..."], "contact_name": "", "contact_role": "", "contact_source_url": "", "deadline_label": ""}}]}}"""


class DiscoveryBusy(RuntimeError):
    """Another deep search is already running."""


def claude_runner(prompt: str) -> str:
    """Headless Claude Code with web search and fetch only, outside the project."""
    command = [
        _cli_binary("claude-code"), "-p", "--output-format", "text",
        "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
        "--strict-mcp-config",
    ]
    with tempfile.TemporaryDirectory(prefix="outreach-discovery-") as workdir:
        completed = subprocess.run(
            command, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=RUNNER_TIMEOUT_SECONDS, cwd=workdir,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"Claude Code exited {completed.returncode}: {detail[:400] or 'no output'}")
    return completed.stdout


def codex_runner(prompt: str) -> str:
    """Fallback: Codex CLI with web search, read-only sandbox, outside the project."""
    command = [
        _cli_binary("codex-cli"), "exec", "--skip-git-repo-check", "--sandbox", "read-only",
        "-c", "tools.web_search=true", "-",
    ]
    with tempfile.TemporaryDirectory(prefix="outreach-discovery-") as workdir:
        completed = subprocess.run(
            command, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=RUNNER_TIMEOUT_SECONDS, cwd=workdir,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"Codex exited {completed.returncode}: {detail[-400:] or 'no output'}")
    return completed.stdout


RUNNERS: dict[str, Runner] = {"claude-code": claude_runner, "codex-cli": codex_runner}


def build_prompt(conn: Any, *, user_id: str, scopes: list[str], limit: int, found: list[dict[str, Any]] = ()) -> str:
    facts = confirmed_facts(conn, user_id)
    definitions = scope_definitions()
    student_fields = (
        "school", "degree", "graduation_year", "skills", "interest_keywords", "preferred_role_types",
        "preferred_locations", "regions", "break_location", "available_terms", "remote_ok", "willing_to_relocate",
    )
    student = {field: facts[field] for field in student_fields if field in facts}
    proof, _ = outreach_proof(facts)
    student.update({field: proof[field] for field in ("projects", "experience") if field in proof})
    conn.row_factory = sqlite3.Row
    conn_rows = conn.execute("SELECT company, website FROM outreach_targets WHERE user_id=? ORDER BY company", (user_id,)).fetchall()
    tracked = [(row[0], row[1]) for row in conn_rows] + [(item["company"], item["website"]) for item in found]
    existing = "\n".join(f"- {name}{f' ({website})' if website else ''}" for name, website in tracked) or "(none yet)"
    dismissed_rows = conn.execute(
        "SELECT company, domain FROM outreach_dismissed WHERE user_id=? ORDER BY company", (user_id,),
    ).fetchall()
    dismissed = "\n".join(f"- {row[0]}{f' ({row[1]})' if row[1] else ''}" for row in dismissed_rows) or "(none)"
    rejected = "\n".join(f"- {item['company']}: {item['reason']}" for item in recent_rejections(conn, user_id=user_id)) or "(none)"
    return PROMPT.format(
        student=json.dumps(student, indent=2, ensure_ascii=False) if student else "(no confirmed facts yet; judge fit on relevance to the student's field)",
        scopes="\n".join(
            f"- {scope} ({definitions[scope]['label']}): {_scope_brief(definitions[scope]['brief'], facts)}"
            for scope in scopes
        ),
        existing=existing,
        dismissed=dismissed,
        rejected=rejected,
        limit=limit,
        scope_ids=", ".join(scopes),
    )


# Rejections that say nothing about the company itself, so there is no point
# telling the next run about them.
_UNINFORMATIVE_REJECTIONS = ("already tracked", "over the ", "scope ")
RECENT_RUNS = 4


def recent_rejections(conn: Any, *, user_id: str, runs: int = RECENT_RUNS) -> list[dict[str, str]]:
    """Companies rejected in the last few successful runs, newest reason first, one per company."""
    rows = conn.execute(
        "SELECT rejected_json FROM outreach_discovery_runs WHERE user_id=? AND status='succeeded' ORDER BY started_at DESC LIMIT ?",
        (user_id, runs),
    ).fetchall()
    tracked, _ = existing_keys(conn, user_id=user_id)
    seen: dict[str, dict[str, str]] = {}
    for row in rows:
        for item in json.loads(row[0] or "[]"):
            company, reason = str(item.get("company") or ""), str(item.get("reason") or "")
            key = company_key(company)
            if not key or key in tracked or reason.startswith(_UNINFORMATIVE_REJECTIONS):
                continue
            seen.setdefault(key, {"company": company, "reason": reason})
    return list(seen.values())[:40]


def excluded_companies(conn: Any, *, user_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """Companies the deep search must not add, by company key and by website domain, with the reason.

    Deleted companies stay deleted, and a company the student already has an
    application with is reached through that application. An open posting is
    deliberately not an exclusion: the posting and the cold email are separate
    doors, and the student wants both.
    """
    by_name: dict[str, str] = {}
    by_domain: dict[str, str] = {}
    # Rows are read by position: a PostgreSQL row is a mapping, and unpacking it yields column names.
    for row in conn.execute("SELECT company, domain FROM outreach_dismissed WHERE user_id=?", (user_id,)).fetchall():
        by_name[company_key(row[0])] = "you deleted it from outreach"
        if row[1]:
            by_domain[row[1]] = "you deleted it from outreach"
    applied = conn.execute(
        "SELECT DISTINCT o.company FROM applications a JOIN opportunities o ON o.id = a.opportunity_id WHERE a.user_id=?",
        (user_id,),
    ).fetchall()
    for row in applied:
        by_name.setdefault(company_key(row[0]), "you already have an application with them")
    # An open posting is no longer an exclusion. A posting and a cold email are
    # different doors into the same company, and a small company worth emailing
    # stays worth emailing while a listing is up; the student applies *and*
    # writes. Only a deletion or an existing application keeps a company out.
    by_name.pop("", None)
    return by_name, by_domain


def _url_loads(fetcher: SafeFetcher, url: str, cache: dict[str, FetchResult]) -> str:
    """'ok', 'unverifiable' (blocked for bots but present), or 'dead'."""
    if url in cache:
        result = cache[url]
    else:
        result = fetcher.fetch(url, same_host_only=True)
        cache[url] = result
    if result.error:
        return "dead"
    return "ok" if result.status < 400 else "unverifiable" if result.status in UNVERIFIABLE_STATUSES else "dead"


# A legal suffix at the end of a company name, with the comma that usually
# introduces it. The comma is part of the suffix, not part of the name:
# "Acme Robotics, Inc." is the same company as "Acme Robotics", and a site that
# writes the plain name must still count as mentioning it.
_LEGAL_SUFFIX_RE = re.compile(
    r"[,\s]+(?:inc\.?|incorporated|corp\.?|corporation|llc|l\.l\.c\.?|ltd\.?|limited|co\.|company)$"
)


def _mentions_company(text: str, company: str, domain: str) -> bool:
    haystack = " ".join(text.casefold().split())
    full = " ".join(company.casefold().split())
    names = {full}
    # Trailing punctuation is stripped too, so a name that ends up as
    # "acme robotics," is never what gets searched for.
    stripped = _LEGAL_SUFFIX_RE.sub("", full).strip(" ,.")
    if stripped:
        names.add(stripped)
    return any(name and name in haystack for name in names) or domain.casefold() in haystack


def validate_proposals(
    raw: str,
    *,
    scopes: list[str],
    existing_names: set[str],
    existing_domains: set[str],
    fetcher: SafeFetcher,
    limit: int,
    today: date,
    excluded_names: dict[str, str] | None = None,
    excluded_domains: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
    """Turn the model's reply into importable records, with reasons for every rejection."""
    parsed = CliAgentProvider.extract_json(raw)
    proposals = parsed.get("companies")
    if not isinstance(proposals, list):
        raise ValueError("The deep search reply had no companies list")
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    names, domains = set(existing_names), set(existing_domains)
    excluded_names, excluded_domains = excluded_names or {}, excluded_domains or {}
    checked: dict[str, FetchResult] = {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        company = " ".join(str(proposal.get("company") or "").split())[:200]

        def reject(reason: str) -> None:
            rejected.append({"company": company or "(unnamed)", "reason": reason})

        if len(accepted) >= limit:
            reject(f"over the {limit} company limit for one run")
            continue
        website = str(proposal.get("website") or "").strip()
        domain = website_domain(website)
        if not company or not website.startswith(("https://", "http://")) or not domain:
            reject("missing a company name or website")
            continue
        if company_key(company) in names or domain in domains:
            reject("already tracked")
            continue
        excluded = excluded_names.get(company_key(company)) or excluded_domains.get(domain)
        if excluded:
            reject(excluded)
            continue
        scope = str(proposal.get("scope") or "")
        if scope not in scopes:
            reject(f"scope {scope or '(none)'} was not requested")
            continue
        summary = str(proposal.get("summary") or "").strip()
        fit = str(proposal.get("fit_rationale") or "").strip()
        signal = str(proposal.get("activity_signal") or "").strip()
        if not summary or not fit:
            reject("missing a summary or fit rationale")
            continue
        urls = [str(url).strip() for url in proposal.get("source_urls") or [] if str(url).strip().startswith(("https://", "http://"))]
        website_state = _url_loads(fetcher, website, checked)
        if website_state == "dead":
            reason = checked[website].error
            reject("website points at a private or local address" if reason == "private" else "website did not load")
            continue
        results = {url: _url_loads(fetcher, url, checked) for url in dict.fromkeys(urls)}
        live = [url for url, state in results.items() if state == "ok"]
        if not live:
            private = any(checked[url].error == "private" for url in results)
            reject("a source points at a private or local address" if private else "none of its source URLs loaded")
            continue
        if not any(_mentions_company(checked[url].text, company, domain) for url in live):
            reject("no source mentions the company")
            continue
        kept_urls = [url for url, state in results.items() if state != "dead"]
        priority = str(proposal.get("priority") or "P2")
        record: dict[str, Any] = {
            "company": company,
            "website": website,
            "channel": SCOPES[scope]["channel"],
            "priority": priority if priority in OUTREACH_PRIORITIES else "P2",
            "summary": summary[:5_000],
            "fit_rationale": fit[:5_000],
            "activity_signal": signal[:5_000],
            "source_urls": kept_urls[:20],
            "researched_at": today.isoformat(),
            "contact_confidence": "unknown",
        }
        location = " ".join(str(proposal.get("location") or "").split())[:200]
        if location:
            record["location"] = location
        contact_name = " ".join(str(proposal.get("contact_name") or "").split())[:200]
        contact_source = str(proposal.get("contact_source_url") or "").strip()
        if contact_name and contact_source and _url_loads(fetcher, contact_source, checked) == "ok":
            record["_ai_contact"] = {
                "name": contact_name,
                "role": " ".join(str(proposal.get("contact_role") or "").split())[:200],
                "evidence_url": contact_source,
            }
        dropped = len(urls) - len(kept_urls)
        notes = []
        if dropped:
            notes.append(f"Deep search dropped {dropped} source URL{'s' if dropped != 1 else ''} that did not load.")
        deadline = str(proposal.get("deadline_label") or "").strip()[:200]
        if deadline:
            notes.append(f"Deep search reported a deadline (unverified): {deadline}")
        if notes:
            record["notes"] = "\n".join(notes)
        accepted.append(record)
        names.add(company_key(company))
        domains.add(domain)
    return accepted, rejected, len(proposals)


class _RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._held = False

    def __enter__(self) -> "_RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stat = self.path.stat()
            if datetime.now(timezone.utc) - datetime.fromtimestamp(stat.st_mtime, timezone.utc) > LOCK_STALE_AFTER:
                self.path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise DiscoveryBusy("A deep search is already running") from exc
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        self._held = True
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)


def last_runs(conn: Any, *, user_id: str, limit: int = 5) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM outreach_discovery_runs WHERE user_id=? ORDER BY started_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    runs = []
    for row in rows:
        item = dict(row)
        item["scopes"] = json.loads(item.pop("scopes_json") or "[]")
        item["rejected"] = json.loads(item.pop("rejected_json") or "[]")
        runs.append(item)
    return runs


def run_discovery(
    conn: Any,
    *,
    user_id: str,
    runner: Runner,
    fetcher: SafeFetcher,
    scopes: list[str] | None = None,
    max_targets: int = MAX_PER_SCOPE,
    dry_run: bool = False,
    trigger: str = "manual",
    report_dir: Path = REPORT_DIR,
    lock_path: Path | None = None,
    provider_factory: Callable[[str, str], Any] | None = None,
    draft_provider: str | None = None,
    contact_delay: float = 1.0,
    form_d_fetcher: SafeFetcher | None = None,
    renderer: PlaywrightRenderer | None = None,
    locate_runner: Runner | None = None,
    email_runner: Runner | None = None,
    verifier: Any = None,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one search per scope and import what passes the checks. max_targets is per scope.

    locate_runner, when given, searches the web for the new companies their own
    sites and EDGAR did not place (outreach_locate.py). email_runner, when given,
    searches other sites for a person's address at the new companies whose own
    site gave no confirmed one (outreach_email_search.py), before any draft is
    written. verifier puts guessed addresses to the mail server (outreach_smtp.py).
    """
    scopes = [scope for scope in (scopes or DEFAULT_SCOPES) if scope in SCOPES]
    if not scopes:
        raise ValueError(f"Choose at least one scope: {', '.join(SCOPES)}")
    max_targets = max(1, min(int(max_targets), MAX_TARGETS))
    now = now or datetime.now(timezone.utc)
    today = today or local_today(conn, user_id, now)

    if trigger == "scheduled" and not dry_run:
        latest = conn.execute(
            "SELECT MAX(started_at) FROM outreach_discovery_runs WHERE user_id=? AND status='succeeded'", (user_id,)
        ).fetchone()[0]
        if latest and datetime.fromisoformat(str(latest)) > now - SCHEDULED_MIN_GAP:
            return {"skipped": True, "reason": f"A deep search already succeeded at {latest}"}

    with _RunLock(lock_path or report_dir / "outreach-discovery.lock"):
        run_id = f"discovery-{uuid4().hex}"
        if not dry_run:
            with conn:
                conn.execute(
                    "INSERT INTO outreach_discovery_runs(id, user_id, run_trigger, status, scopes_json, started_at) VALUES(?, ?, ?, 'running', ?, ?)",
                    (run_id, user_id, trigger, json.dumps(scopes), utc_now()),
                )
        try:
            summary = _run(
                conn, run_id=run_id, user_id=user_id, runner=runner, fetcher=fetcher, scopes=scopes,
                max_targets=max_targets, dry_run=dry_run, report_dir=report_dir,
                provider_factory=provider_factory, draft_provider=draft_provider,
                contact_delay=contact_delay, form_d_fetcher=form_d_fetcher, renderer=renderer,
                locate_runner=locate_runner, email_runner=email_runner, verifier=verifier, today=today, now=now,
            )
        except Exception as exc:
            if not dry_run:
                with conn:
                    conn.execute(
                        "UPDATE outreach_discovery_runs SET status='failed', error=?, finished_at=? WHERE id=?",
                        (str(exc)[:1_000], utc_now(), run_id),
                    )
            raise
        return summary


def _run(
    conn: Any,
    *,
    run_id: str,
    user_id: str,
    runner: Runner,
    fetcher: SafeFetcher,
    scopes: list[str],
    max_targets: int,
    dry_run: bool,
    report_dir: Path,
    provider_factory: Callable[[str, str], Any] | None,
    draft_provider: str | None,
    contact_delay: float,
    form_d_fetcher: SafeFetcher | None,
    renderer: PlaywrightRenderer | None,
    locate_runner: Runner | None,
    today: date,
    now: datetime,
    email_runner: Runner | None = None,
    verifier: Any = None,
) -> dict[str, Any]:
    names, domains = existing_keys(conn, user_id=user_id)
    excluded_names, excluded_domains = excluded_companies(conn, user_id=user_id)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    scope_results: list[dict[str, Any]] = []
    failures: list[tuple[str, Exception]] = []
    proposed = 0
    for scope in scopes:
        prompt = build_prompt(conn, user_id=user_id, scopes=[scope], limit=max_targets, found=accepted)
        try:
            found, turned_down, count = validate_proposals(
                runner(prompt), scopes=[scope], existing_names=names, existing_domains=domains,
                fetcher=fetcher, limit=max_targets, today=today,
                excluded_names=excluded_names, excluded_domains=excluded_domains,
            )
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            failures.append((scope, exc))
            scope_results.append({"scope": scope, "proposed": 0, "accepted": 0, "error": str(exc)[:500]})
            continue
        accepted.extend(found)
        rejected.extend({**item, "scope": scope} for item in turned_down)
        proposed += count
        names |= {company_key(record["company"]) for record in found}
        domains |= {website_domain(record["website"]) for record in found}
        scope_results.append({"scope": scope, "proposed": count, "accepted": len(found), "error": ""})
    if len(failures) == len(scopes):
        detail = "; ".join(f"{scope}: {exc}" for scope, exc in failures)
        raise RuntimeError(f"Every search failed. {detail}"[:1_000]) from failures[0][1]
    scope_errors = "; ".join(f"The {scope_definitions()[scope]['label']} search failed: {exc}" for scope, exc in failures)[:1_000]
    report: dict[str, Any] = {
        "format": "outreach-targets-v1",
        "run_id": run_id,
        "dry_run": dry_run,
        "scopes": scopes,
        "researched_at": today.isoformat(),
        "proposed": proposed,
        "scope_results": scope_results,
        "rejected": rejected,
        "items": [{key: value for key, value in record.items() if not key.startswith("_")} for record in accepted],
    }
    imported = {"imported": 0, "skipped": 0, "errors": [], "created_ids": []}
    follow_through: list[dict[str, Any]] = []
    if not dry_run:
        contacts = {record["company"].casefold(): record.get("_ai_contact") for record in accepted}
        imported = import_targets(
            conn,
            [{key: value for key, value in record.items() if not key.startswith("_")} for record in accepted],
            user_id=user_id, origin="discovery", discovery_run_id=run_id,
        )
        for target_id in imported["created_ids"]:
            follow_through.append(_follow_through(
                conn, target_id, user_id=user_id, ai_contact=contacts.get(get_target(conn, target_id, user_id=user_id)["company"].casefold()),
                fetcher=fetcher, contact_delay=contact_delay,
                form_d_fetcher=form_d_fetcher, renderer=renderer, today=today, verifier=verifier,
                finish=email_runner is None,
            ))
        if email_runner is not None:
            report["email_search"] = _search_other_sites(
                conn, [item["target_id"] for item in follow_through], user_id=user_id,
                runner=email_runner, fetcher=fetcher, verifier=verifier,
            )
            for item in follow_through:
                _finish_contact(conn, item, user_id=user_id)
    located: dict[str, Any] = {}
    unplaced = [] if locate_runner is None else [
        item["target_id"] for item in follow_through
        if not location_usable(get_target(conn, item["target_id"], user_id=user_id))
    ]
    if unplaced:
        # Imported here: outreach_locate researches through this module's runners.
        from .outreach_locate import locate_targets

        # The same runner searches for the companies their own sites and EDGAR
        # did not place, so a new company arrives with a location or a reason.
        try:
            located = locate_targets(conn, user_id=user_id, runner=locate_runner, fetcher=fetcher, target_ids=unplaced)
        except (ValueError, httpx.HTTPError, RuntimeError, subprocess.SubprocessError) as exc:
            located = {"error": str(exc)[:500]}
    # Drafts come last: a draft written before the web search placed the
    # company near the student's home would leave out that they live there.
    for item in follow_through:
        _write_draft(conn, item, user_id=user_id, provider_factory=provider_factory, draft_provider=draft_provider)
    report["imported"] = imported["imported"]
    report["follow_through"] = follow_through
    report["located"] = located
    report_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-dry-run" if dry_run else ""
    report_path = report_dir / (
        f"outreach-discovered-{today.isoformat()}-{now.astimezone(timezone.utc):%H%M%S}-"
        f"{run_id.removeprefix('discovery-')[:8]}{suffix}.json"
    )
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if not dry_run:
        with conn:
            conn.execute(
                """
                UPDATE outreach_discovery_runs SET status='succeeded', proposed=?, imported=?, skipped=?,
                    rejected_json=?, report_path=?, error=?, finished_at=? WHERE id=?
                """,
                (proposed, imported["imported"], imported["skipped"] + len(imported["errors"]),
                 json.dumps(rejected + [{"company": error["company"], "reason": error["error"]} for error in imported["errors"]]),
                 str(report_path), scope_errors, utc_now(), run_id),
            )
    return {
        "run_id": run_id,
        "dry_run": dry_run,
        "proposed": proposed,
        "accepted": len(accepted),
        "imported": imported["imported"],
        "scope_results": scope_results,
        "rejected": rejected,
        "follow_through": follow_through,
        "located": located,
        "report_path": str(report_path),
    }


def _search_other_sites(
    conn: Any, target_ids: list[str], *, user_id: str, runner: Runner, fetcher: SafeFetcher, verifier: Any,
) -> dict[str, Any]:
    """The other-sites email search for the targets still without a person to write to."""
    # Imported here: outreach_email_search imports outreach_contacts, as this module does.
    from .outreach_email_search import needs_a_person, search_emails

    due = [target_id for target_id in target_ids if needs_a_person(conn, target_id, user_id=user_id)]
    if not due:
        return {"searched": 0, "found": 0, "results": []}
    try:
        return search_emails(conn, user_id=user_id, runner=runner, fetcher=fetcher, target_ids=due, verifier=verifier)
    except (ValueError, httpx.HTTPError, RuntimeError, subprocess.SubprocessError) as exc:
        return {"searched": len(due), "found": 0, "results": [], "error": str(exc)[:500]}


def _follow_through(
    conn: Any,
    target_id: str,
    *,
    user_id: str,
    ai_contact: dict[str, str] | None,
    fetcher: SafeFetcher,
    contact_delay: float,
    form_d_fetcher: SafeFetcher | None,
    today: date,
    renderer: PlaywrightRenderer | None = None,
    verifier: Any = None,
    finish: bool = True,
) -> dict[str, Any]:
    """Contacts, location, and Form D for one new target. Failures are recorded, never fatal.

    With finish=False the contact is not chosen yet, so a search of other sites
    can add candidates first; _finish_contact chooses it. The draft waits for
    _write_draft, after every location search has run.
    """
    outcome: dict[str, Any] = {
        "target_id": target_id, "contact": None, "cc": None, "contact_basis": None,
        "location": None, "form_d": None, "draft": None, "errors": [],
    }
    if ai_contact:
        with conn:
            conn.execute(
                """
                INSERT INTO outreach_contact_candidates(id, target_id, user_id, name, role, email, method, confidence, evidence_url, created_at)
                VALUES(?, ?, ?, ?, ?, '', 'ai_research', 'unknown', ?, ?)
                ON CONFLICT(target_id, email, name) DO NOTHING
                """,
                (f"candidate-{uuid4().hex}", target_id, user_id, ai_contact["name"], ai_contact["role"], ai_contact["evidence_url"], utc_now()),
            )
            conn.execute(
                "UPDATE outreach_targets SET contact_name=?, contact_role=?, contact_evidence_url=?, contact_route=? WHERE id=? AND user_id=?",
                (ai_contact["name"], ai_contact["role"], ai_contact["evidence_url"],
                 f"Named on {ai_contact['evidence_url']}; no address found yet", target_id, user_id),
            )
    try:
        found = find_contacts(
            conn, target_id, user_id=user_id, fetcher=fetcher, delay=contact_delay, renderer=renderer, verifier=verifier,
        )
        outcome["location"] = render_site_location(
            conn, target_id, user_id=user_id, plain=found["location"], renderer=renderer, fetcher=fetcher,
        )
    except (ValueError, LookupError, httpx.HTTPError) as exc:
        outcome["errors"].append(f"contacts: {exc}")
    if form_d_fetcher is not None:
        try:
            company = get_target(conn, target_id, user_id=user_id)["company"]
            outcome["form_d"] = record_form_d(
                conn, target_id, user_id=user_id, today=today,
                form_d=form_d_lookup(company, fetcher=form_d_fetcher, today=today),
            )
        except (SecUnavailableError, httpx.HTTPError) as exc:
            outcome["errors"].append(f"sec: {exc}")
    if finish:
        _finish_contact(conn, outcome, user_id=user_id)
    return outcome


def _finish_contact(conn: Any, outcome: dict[str, Any], *, user_id: str) -> None:
    """Apply the contact an unattended run may use (choose_contact)."""
    target_id = outcome["target_id"]
    try:
        choice = choose_contact(list_candidates(conn, target_id, user_id=user_id))
        if choice:
            apply_choice(conn, target_id, choice, user_id=user_id)
            outcome["contact"] = choice["to"]["email"]
            outcome["cc"] = choice["cc"]["email"] if choice["cc"] else None
            outcome["contact_basis"] = choice["basis"]
    except (ValueError, LookupError) as exc:
        outcome["errors"].append(f"contacts: {exc}")


def _write_draft(
    conn: Any,
    outcome: dict[str, Any],
    *,
    user_id: str,
    provider_factory: Callable[[str, str], Any] | None,
    draft_provider: str | None,
) -> None:
    """Draft to the chosen contact, then record every failure the target met on the way."""
    target_id = outcome["target_id"]
    if outcome["contact"] and provider_factory is not None:
        from .outreach_drafting import generate_draft

        try:
            generate_draft(conn, target_id, user_id=user_id, provider_factory=provider_factory, provider=draft_provider)
            outcome["draft"] = "generated"
        except (ValueError, RuntimeError) as exc:
            outcome["errors"].append(f"draft: {exc}")
    if outcome["errors"]:
        with conn:
            _log(conn, target_id, user_id, "discovery_follow_through", detail="; ".join(outcome["errors"])[:2_000])


class DiscoveryManager:
    """Runs one deep search at a time in the background for the web app."""

    def __init__(
        self,
        platform_target: Path | str,
        *,
        runner: Runner | None = None,
        locate: bool = True,
        client_factory: Callable[[], SafeFetcher] = default_fetcher,
        provider_factory: Callable[[str, str], Any] | None = None,
        report_dir: Path = REPORT_DIR,
        contact_delay: float = 1.0,
        form_d_fetcher_factory: Callable[[], SafeFetcher | None] = sec_fetcher,
        renderer_factory: Callable[[], PlaywrightRenderer | None] = default_renderer,
        verifier_factory: Callable[[], Any] = lambda: None,
        email_search: bool = False,
    ) -> None:
        self.platform_target = platform_target
        self._verifier_factory = verifier_factory
        self._email_search = email_search
        self._form_d_fetcher_factory = form_d_fetcher_factory
        self._renderer_factory = renderer_factory
        self._runner = runner
        self._locate = locate
        self._client_factory = client_factory
        self._provider_factory = provider_factory
        self._report_dir = report_dir
        self._contact_delay = contact_delay
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"state": "idle", "started_at": None, "finished_at": None, "error": None, "result": None}
        self._thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def start(self, *, user_id: str, scopes: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._state["state"] == "running":
                raise DiscoveryBusy("A deep search is already running")
            self._state = {"state": "running", "started_at": utc_now(), "finished_at": None, "error": None, "result": None}

        def run() -> None:
            result, error = None, None
            try:
                runner = self._runner or RUNNERS.get((os.environ.get("PIPELINE_OUTREACH_DISCOVERY_PROVIDER") or "claude-code"), claude_runner)
                with ExitStack() as stack:
                    conn = stack.enter_context(closing(connect_product(self.platform_target)))
                    fetcher = stack.enter_context(self._client_factory())
                    form_d = self._form_d_fetcher_factory()
                    renderer = self._renderer_factory()
                    verifier = self._verifier_factory()
                    result = run_discovery(
                        conn, user_id=user_id, runner=runner, fetcher=fetcher, scopes=scopes,
                        report_dir=self._report_dir, provider_factory=self._provider_factory,
                        contact_delay=self._contact_delay,
                        form_d_fetcher=stack.enter_context(form_d) if form_d is not None else None,
                        renderer=stack.enter_context(renderer) if renderer is not None else None,
                        locate_runner=runner if self._locate else None,
                        email_runner=runner if self._email_search else None,
                        verifier=stack.enter_context(verifier) if verifier is not None else None,
                    )
            except Exception as exc:  # noqa: BLE001 - reported to the UI
                error = str(exc)[:1_000]
            with self._lock:
                self._state.update(
                    state="failed" if error else "succeeded", error=error, result=result, finished_at=utc_now(),
                )

        self._thread = threading.Thread(target=run, name="outreach-discovery", daemon=True)
        self._thread.start()
        return self.status()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
