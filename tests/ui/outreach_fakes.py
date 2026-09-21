"""Offline stand-ins for everything the outreach pipeline would reach out to.

The browser suite must never call a model, browse a company website, or run a
real web search. These fakes answer instead: a draft writer that only uses the
facts it is given, two tiny company websites, a DNS answer, an SEC EDGAR
search that knows one Form D, and a deep search that proposes one company. The
proposed company has no posting in the fixture feed, so it is not rejected as
one to apply to instead.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import httpx

from opportunity_app.agent_providers import ProviderReply
from opportunity_app.outreach_contacts import SafeFetcher
from opportunity_app.outreach_discovery import DiscoveryManager
from opportunity_app.outreach_recontact import RecontactManager
from opportunity_app.system_status import SystemStatus
from opportunity_app.boards import BoardTracker
from opportunity_app.outreach_settings import OutreachSettings

COMPOSE_ACCOUNT = "student@school.example"

SITES = {
    "bovi.example": {
        "/robots.txt": "User-agent: *\nAllow: /\n",
        "/": '<nav><a href="/team">Team</a></nav><p>Dairy robotics. Write to <a href="mailto:hello@bovi.example">hello@bovi.example</a></p>',
        "/team": "<div><h3>Jane Doe</h3><p>Co-Founder &amp; CTO</p></div><p>jane@bovi.example</p>"
                 "<div><h3>Sam Lee</h3><p>Head of Talent</p></div>",
    },
    "kestrel-robotics.example": {
        "/robots.txt": "",
        "/": '<nav><a href="/about">About us</a></nav><p>Warehouse robots</p>',
        "/about": "<h3>Rita Moreno</h3><p>Founder and CEO</p><p>Reach us at careers@kestrel-robotics.example</p>"
                  "<p>Kestrel Robotics is headquartered in San Carlos, California.</p>",
    },
    # Names one place and never says the company is based there.
    "wren-motion.example": {
        "/robots.txt": "",
        "/": '<nav><a href="/about">About us</a></nav><p>Motion control for small machine shops.</p>',
        "/about": "<h3>Ada Reyes</h3><p>Founder</p><p>Write to hello@wren-motion.example</p><footer><p>Austin, TX</p></footer>",
    },
}
FORM_D_SEARCH = {"hits": {"hits": [{
    "_id": "0009999999-26-000001:primary_doc.xml",
    "_source": {
        "ciks": ["0009999999"], "display_names": ["Kestrel Robotics, Inc.  (CIK 0009999999)"],
        "adsh": "0009999999-26-000001", "file_date": "2026-03-05", "form": "D", "biz_locations": ["San Carlos, CA"],
    },
}]}}
FORM_D_XML = """<?xml version="1.0"?>
<edgarSubmission>
  <primaryIssuer><entityName>Kestrel Robotics, Inc.</entityName>
    <issuerAddress><city>SAN CARLOS</city><stateOrCountry>CA</stateOrCountry></issuerAddress></primaryIssuer>
  <offeringData><offeringSalesAmounts><totalOfferingAmount>5000000</totalOfferingAmount>
    <totalAmountSold>3200000</totalAmountSold></offeringSalesAmounts></offeringData>
</edgarSubmission>"""


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "cloudflare-dns.com":
        return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 15, "data": "10 mx.example."}]})
    pages = SITES.get(request.url.host.removeprefix("www."))
    body = pages.get(request.url.path) if pages else None
    if body is None:
        return httpx.Response(404)
    kind = "text/plain" if request.url.path.endswith(".txt") else "text/html"
    return httpx.Response(200, text=body, headers={"content-type": kind})


def _sec_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "efts.sec.gov":
        if "Kestrel" in str(request.url):
            return httpx.Response(200, json=FORM_D_SEARCH)
        return httpx.Response(200, json={"hits": {"hits": []}})
    if request.url.path.endswith("/primary_doc.xml"):
        return httpx.Response(200, text=FORM_D_XML, headers={"content-type": "application/xml"})
    return httpx.Response(404)


def form_d_client() -> SafeFetcher:
    client = httpx.Client(transport=httpx.MockTransport(_sec_handler), follow_redirects=False)
    return SafeFetcher(client, resolve=lambda _host: ["93.184.216.34"])


def contact_client() -> SafeFetcher:
    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    return SafeFetcher(client, resolve=lambda _host: ["93.184.216.34"])


class FakeDraftProvider:
    """Writes a short draft from the JSON it receives, citing each fact it uses."""

    name = "anthropic"
    model = "ui-fake"

    def create(self, *, instructions, messages, tools, max_output_tokens):
        content = messages[-1]["content"]
        inputs = json.loads(re.split(r"\n\n(?:Your previous draft|The student reviewed)", content)[0])
        comments = content.split("The student's comments:\n", 1)[1] if "The student's comments:\n" in content else ""
        student = inputs["student"]
        research = inputs["company_research"]
        unverified = inputs["unverified_research"]
        first_name = research.get("contact_name", "").split(" ")[0]
        greeting = f"Hi {first_name}," if first_name else f"Hi {research['company']} team,"
        claims = [{"text": student["name"], "basis": "profile:name"}]
        if "original_email" in inputs:
            subject = f"Re: {inputs['original_email']['subject']}"
            body = f"{greeting}\n\nFollowing up on my note about a 15 minute call.\n\nThank you,\n{student['name']}"
        else:
            reason = ""
            summary = research.get("summary") or unverified.get("summary")
            if summary and "shorter" not in comments.lower():
                reason = f" Your work on {summary.rstrip('.').lower()} is why I am writing."
                claims.append({"text": summary, "basis": "research:summary" if research.get("summary") else "unverified:summary"})
            claims.append({"text": "The student's work connects to this company", "basis": "inference"})
            subject = f"Internship question for {research['company']}"
            body = (
                f"{greeting}\n\nI'm {student['name']}, an engineering student.{reason}\n\n"
                f"Would you be open to a 15 minute call about interning with your team?\n\nThank you,\n{student['name']}"
            )
        return ProviderReply(text=json.dumps({"subject": subject, "body": body, "claims": claims}))


def provider_factory(_provider: str, _model: str) -> FakeDraftProvider:
    return FakeDraftProvider()


class FakeTypeSafeClient:
    """A deterministic Jev-shaped response for browser and sandbox tests."""

    configured = True
    model = "jev-1.13.0"

    def evaluate(self, *, state, questions):
        answers = {}
        for question_id, question in questions.items():
            if question["type"] == "score":
                last = len(question["criteria"]) - 1
                answers[question_id] = {
                    "type": "score",
                    "score": float(last),
                    "legend": {str(index): value for index, value in enumerate(question["criteria"])},
                    "probabilities": {str(index): (1.0 if index == last else 0.0) for index in range(last + 1)},
                    "confidence": 0.96,
                }
            else:
                options = list(question["criteria"])
                answers[question_id] = {
                    "type": "choice",
                    "choice": options[0],
                    "probabilities": {option: (1.0 if option == options[0] else 0.0) for option in options},
                    "confidence": 0.94,
                }
        return {"model": self.model, "answers": answers, "usage": {"input_tokens": 321, "output_tokens": 0}}


def deep_search_reply(prompt: str) -> str:
    """Each scope is its own search; only the US startups search finds a company.

    The same runner is asked where a new company is based; here their own sites
    say, so that search finds nothing to add.
    """
    if "finding where each of these companies is based" in prompt:
        return json.dumps({"companies": []})
    if "## What to look for\n- us-startups (" not in prompt:
        return json.dumps({"companies": []})
    return json.dumps({"companies": [{
        "company": "Kestrel Robotics",
        "website": "https://kestrel-robotics.example",
        "scope": "us-startups",
        "summary": "Warehouse robots",
        "fit_rationale": "Hands-on mechanical design for mobile robots",
        "activity_signal": "Announced a seed round (August 2026)",
        "priority": "P1",
        "source_urls": ["https://kestrel-robotics.example/about"],
        "contact_name": "Rita Moreno",
        "contact_role": "Founder and CEO",
        "contact_source_url": "https://kestrel-robotics.example/about",
    }]})


def discovery_manager(platform_path: Path, report_dir: Path) -> DiscoveryManager:
    return DiscoveryManager(
        platform_path,
        runner=deep_search_reply,
        client_factory=contact_client,
        provider_factory=provider_factory,
        report_dir=report_dir,
        contact_delay=0,
        form_d_fetcher_factory=form_d_client,
        # The fake sites have text; a real browser has nothing to render here.
        renderer_factory=lambda: None,
    )


def recontact_manager(platform_path: Path) -> RecontactManager:
    # No web search and no mail-server checks: guesses stay weak, which is the
    # case the review screen has to label most carefully.
    return RecontactManager(
        platform_path,
        email_search=False,
        client_factory=contact_client,
        provider_factory=provider_factory,
        draft_provider="anthropic",
        contact_delay=0,
    )


class FakeScheduler:
    """Task Scheduler as the status panel sees it; installing only flips a flag."""

    def __init__(self) -> None:
        self.installed = {"daily", "outreach", "autostart"}

    def jobs(self) -> dict[str, dict]:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            job: {"installed": job in self.installed, "state": "Ready" if job in self.installed else "",
                  "last_run_at": stamp if job in self.installed else None, "next_run_at": None,
                  "last_result": 0 if job in self.installed else None}
            for job in ("daily", "outreach", "autostart")
        }

    def install(self, job: str) -> int:
        self.installed.add(job)
        return 0


SCHEDULER = FakeScheduler()
STATUS: SystemStatus | None = None
STATUS_BOARDS = [
    {"kind": "greenhouse", "company": "Acme Boards", "token": "acme-boards"},
    {"kind": "lever", "company": "Orbit Boards", "site": "orbit-boards"},
]


def system_status(root: Path) -> SystemStatus:
    """A healthy status panel by default, so screenshots carry no attention marker.

    Tests that need a problem take a board down with fail_board() or remove a
    job from SCHEDULER.installed, and put it back afterwards.
    """
    root.mkdir(parents=True, exist_ok=True)
    sources = root / "sources.json"
    sources.write_text(json.dumps({"ats_sources": STATUS_BOARDS, "discovery_title_terms": []}), encoding="utf-8")
    legacy = root / "pipeline.db"
    stamp = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(legacy)) as conn:
        conn.execute(
            "CREATE TABLE fetch_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL, started_at TEXT NOT NULL,"
            " finished_at TEXT, outcome TEXT NOT NULL, fetched_count INTEGER NOT NULL DEFAULT 0, error TEXT)"
        )
        conn.executemany(
            "INSERT INTO fetch_runs(source_key, started_at, finished_at, outcome, fetched_count) VALUES (?, ?, ?, 'success', 5)",
            [("greenhouse:acme-boards", stamp, stamp), ("lever:orbit-boards", stamp, stamp)],
        )
        conn.commit()
    daily = root / "daily-run.json"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    daily.write_text(json.dumps({"runDate": now[:10], "startedAt": now, "finishedAt": now, "exitCode": 0}), encoding="utf-8")
    global STATUS
    STATUS = SystemStatus(legacy_path=legacy, sources_path=sources, daily_state_path=daily, scheduler=SCHEDULER, cache_seconds=0)
    return STATUS


def fail_board(source_key: str, error: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(STATUS.legacy_path)) as conn:
        conn.execute(
            "INSERT INTO fetch_runs(source_key, started_at, finished_at, outcome, error) VALUES (?, ?, ?, 'error', ?)",
            (source_key, stamp, stamp, error),
        )
        conn.commit()


def heal_board(source_key: str) -> None:
    with closing(sqlite3.connect(STATUS.legacy_path)) as conn:
        conn.execute("DELETE FROM fetch_runs WHERE source_key=? AND outcome='error'", (source_key,))
        conn.commit()


# Greenhouse names the company that owns a board; Ashby does not. Everything
# else finds nothing, as an unknown company would.
BOARD_PROBES = {
    "greenhouse": lambda slug: {"board_name": "Kestrel Robotics", "titles": ["Robotics Intern", "Controls Engineer"], "field": "token"}
    if slug == "kestrelrobotics" else None,
    "ashby": lambda slug: {"board_name": None, "titles": ["Autonomy Intern"], "field": "board"} if slug == "wrenmotion" else None,
    "lever": lambda slug: None,
}


BOARDS_LOCAL: Path | None = None


def _board_lookup(companies, config, terms):
    import pipeline

    with mock.patch.dict(pipeline.DISCOVERY_VENDORS, BOARD_PROBES):
        return pipeline.discover_ats(companies, config, terms)


def board_tracker(root: Path) -> BoardTracker:
    root.mkdir(parents=True, exist_ok=True)
    catalog = root / "sources.json"
    catalog.write_text(json.dumps({"ats_sources": STATUS_BOARDS, "discovery_title_terms": ["intern"]}), encoding="utf-8")
    global BOARDS_LOCAL
    BOARDS_LOCAL = root / "sources.local.json"
    return BoardTracker(sources_path=catalog, local_path=BOARDS_LOCAL, lookup=_board_lookup)


def outreach_settings(root: Path) -> OutreachSettings:
    """Settings that write a throwaway .env; the student's own is never touched."""
    global SETTINGS_ENV
    SETTINGS_ENV = root / "settings.env"
    return OutreachSettings(env_path=SETTINGS_ENV, attachment_dir=root / "outreach-attachment", resume_storage=root / "resumes")


SETTINGS_ENV: Path | None = None
