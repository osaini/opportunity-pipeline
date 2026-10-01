"""Shared fixtures for the outreach tests: contact-finding fakes, a scripted drafting model, confirmed profile facts, a fake Jev.

Not a test module: nothing here is named test*, so neither unittest nor pytest collects it."""

import json

import httpx

from opportunity_app.agent_providers import ProviderReply
from opportunity_app.timestamps import utc_now
from opportunity_app.web_fetch import SafeFetcher


# The owner token of the app the drafting tests build; test_call_prep_fields builds its app with the same token.
DRAFTING_AUTH = {"Authorization": "Bearer drafting-owner"}
USER = "local-user"


def confirm_facts(conn, **facts):
    for field, value in facts.items():
        conn.execute(
            """
            INSERT INTO profile_facts(user_id, field_path, value_json, source, confirmed, created_at, updated_at)
            VALUES(?, ?, ?, 'user', 1, ?, ?)
            ON CONFLICT(user_id, field_path) DO UPDATE SET value_json=excluded.value_json, confirmed=1
            """,
            (USER, field, json.dumps(value), utc_now(), utc_now()),
        )
    conn.commit()


class DraftingScriptedProvider:
    """Returns queued replies and records every prompt it was given."""

    name = "anthropic"
    model = "test-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def create(self, *, instructions, messages, tools, max_output_tokens):
        self.prompts.append(messages[-1]["content"])
        return ProviderReply(text=self.replies.pop(0))


def site_transport(sites, *, mx=True, dead=()):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        host = request.url.host.removeprefix("www.")
        if host == "cloudflare-dns.com":
            answer = [{"type": 15, "data": "10 mx.example."}] if mx else []
            return httpx.Response(200, json={"Status": 0, "Answer": answer})
        if str(request.url) in dead:
            return httpx.Response(404)
        pages = sites.get(host)
        if pages is None:
            return httpx.Response(404)
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        kind = "text/plain" if request.url.path.endswith(".txt") else "text/html"
        return httpx.Response(200, text=body, headers={"content-type": kind})

    return httpx.MockTransport(handler), requested


def safe_fetcher(client):
    return SafeFetcher(client, resolve=lambda _host: ["93.184.216.34"])


def proposals(*companies):
    return "Here you go:\n" + json.dumps({"companies": list(companies)})


# The second kind of prompt a deep search sends its runner: where a new company
# it imported is based (outreach_locate.py).
LOCATE_PROMPT = "finding where each of these companies is based"


def scope_of(prompt):
    """The one scope a deep search prompt asks about."""
    return prompt.split("## What to look for\n- ", 1)[1].split(" (", 1)[0]


def only_for(reply, scope="local-accelerators", locations=None):
    """A runner that answers one scope's search with reply and finds nothing for the others."""
    return lambda prompt: (locations or proposals()) if LOCATE_PROMPT in prompt else (
        reply if scope_of(prompt) == scope else proposals()
    )


def company(name, website, **overrides):
    return {
        "company": name,
        "website": website,
        "scope": "local-accelerators",
        "summary": f"{name} builds robots",
        "fit_rationale": "Matches the student's robotics interest",
        "activity_signal": "Raised a seed round (June 2026)",
        "priority": "P1",
        "source_urls": [f"{website}/about"],
        **overrides,
    }


class FakeJev:
    """Answers every Choice with one label at one confidence, and counts calls."""

    configured = True
    model = "jev-1.13.0"

    def __init__(self, label=None, confidence=0.93, error=None, answer=None):
        self.label, self.confidence, self.error, self.answer = label, confidence, error, answer
        self.calls = []

    def evaluate(self, *, state, questions):
        self.calls.append(state)
        if self.error is not None:
            raise self.error
        (question_id, question), = questions.items()
        if self.answer is not None:
            return {"model": self.model, "answers": {question_id: self.answer}, "usage": {"input_tokens": 1, "output_tokens": 0}}
        options = list(question["criteria"])
        label = self.label or options[0]
        return {
            "model": self.model,
            "answers": {question_id: {
                "type": "choice", "choice": label, "confidence": self.confidence,
                "probabilities": {option: (self.confidence if option == label else 0.0) for option in options},
            }},
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }


PORT_FACT = {
    "section": "technology", "text": "The arm finds the charge port with a stereo camera within 90 seconds",
    "source_url": "https://news.example/chargebot-seed", "quote": "finds the charge port with a stereo camera", "person": "",
    "checked": True, "note": "",
}
STACK_FACT = {
    "section": "engineering", "text": "Motion planning in C++ on ROS 2", "source_url": "https://chargebot.example/careers",
    "quote": "motion planning code in C++", "person": "", "checked": True, "note": "",
}
BLOCKED_FACT = {
    "section": "traction", "text": "Raised a $4.5M seed round", "source_url": "https://news.example/blocked",
    "quote": "a $4.5M seed round", "person": "", "checked": False, "note": "the site turned the check away (HTTP 403)",
}
BRIEF = {"facts": [PORT_FACT, STACK_FACT, BLOCKED_FACT], "gaps": ["which motors the arm uses"], "refused": [], "proposed": 3}


def store_brief(conn, target_id, brief=BRIEF, at="2026-09-20T12:00:00+00:00", error=""):
    conn.execute(
        "UPDATE outreach_targets SET tech_brief_json=?, tech_brief_at=?, tech_brief_by='claude-code', tech_brief_error=? WHERE id=?",
        (json.dumps(brief), at, error, target_id),
    )
    conn.commit()
