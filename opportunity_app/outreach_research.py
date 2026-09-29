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
model that did not write the fact reads it beside a passage Python itself cut
from the page (the quote's paragraph, the lines around it, and the top of the
page) and says whether the passage states exactly that: the same company,
product, or person, the same numbers on the same things, nothing swapped, and
the same "not" (JUDGE_INSTRUCTIONS). Only a fact it confirms is kept as checked.
Without that second read (no model, or it failed), a fact whose words passed is
kept marked "not checked", like one from a page nobody could read, and nothing
is built on it.

A competitor fact is about another company, so its passage must name that
competitor (or the page is the competitor's own site). Whether the two compete
is the research agent's judgment unless the quote's sentence names both, and
the brief says which (the fact's note).

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
import re
import sqlite3
import unicodedata
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from .agent_providers import CliAgentProvider, _cli_available, _cli_binary, complete_text
from .operations import enqueue_job
from .outreach import LEGAL_SUFFIXES, OutreachNotFoundError, _log, company_key, get_target, website_domain
from .outreach_contacts import FetchResult, SafeFetcher, _PageParser, public_web_url_error
from .outreach_discovery import RUNNERS, UNVERIFIABLE_STATUSES
from .outreach_email_search import BLOCKED_HOSTS
from .preparation import confirmed_facts
from .schema import utc_now

Runner = Callable[[str], str]
# Sends instructions and content to a model and returns its reply.
Judge = Callable[[str, str], str]

JOB_TYPE = "outreach_company_research"
MAX_ATTEMPTS = 3
ACTIVE_JOB_STATES = {"queued", "running", "retry"}
AGENT_ENV = "PIPELINE_OUTREACH_COMPANY_RESEARCH_PROVIDER"
DISCOVERY_ENV = "PIPELINE_OUTREACH_DISCOVERY_PROVIDER"
# One company, read closely: longer than a location search, far shorter than a deep search.
RUNNER_TIMEOUT_SECONDS = 20 * 60
# Call prep researches again when the brief is older than this.
FRESH_FOR = timedelta(days=30)
# A try that failed or kept nothing waits this long before call prep tries again.
RETRY_AFTER = timedelta(days=1)

# Sections in the order they print, with the headings call prep uses.
SECTIONS = (
    ("product", "WHAT THEY BUILD"),
    ("customers", "WHO THEY SELL TO"),
    ("edge", "WHAT THEY SAY SETS THEM APART"),
    ("competitors", "COMPETITORS"),
    ("technology", "HOW IT WORKS"),
    ("engineering", "WHAT THEY BUILD IT WITH"),
    ("growth", "WHERE THEY'RE EXPANDING"),
    ("hiring", "WHERE THEY'RE HIRING"),
    ("team", "WHO BUILDS IT"),
    ("traction", "FUNDING, CUSTOMERS, AND PARTNERS"),
    ("news", "RECENT NEWS"),
)
SECTION_IDS = tuple(key for key, _ in SECTIONS)
COMPETITORS = "competitors"
MAX_FACTS_PER_SECTION = 5
MAX_FACT_CHARS = 300
MAX_QUOTE_CHARS = 600
MAX_GAPS = 8
# Bounds on one run's checking, so a long reply cannot hold the worker for hours.
MAX_PROPOSALS = 80
MAX_PAGES = 40
MAX_RENDERS = 8
# A quote shorter than this matches too much by accident to prove anything,
# and a piece of a trimmed quote shorter than PIECE_WORDS is not looked for.
MIN_QUOTE_WORDS = 5
PIECE_WORDS = 3
# The pieces of a trimmed quote must sit this close together on the page.
PIECE_GAP = 60
# Words either side of the quote where the fact's numbers, names, and other
# words may also be: a price beside a product's name, a date in the dateline.
WINDOW = 40
# Share of a fact's other words (numbers and names must all be there) that
# may be missing from the quote and the lines around it. Whether a word that
# is there means the same thing is the second read's question, not this one.
WORDS_MISSING_SHARE = 0.4
# Lines at the top of a page (its title, a dateline) that every fact on it may lean on.
TOP_LINES = 3
# The passage the second read sees: the quote's own paragraph whole (up to
# QUOTE_LINE_CHARS), then the lines around it nearest first, over the same
# stretch the word checks read and at least PASSAGE_BEFORE and PASSAGE_AFTER
# lines, until PASSAGE_CHARS.
PASSAGE_BEFORE = 2
PASSAGE_AFTER = 1
PASSAGE_CHARS = 2_500
QUOTE_LINE_CHARS = 3_000
TOP_CHARS = 300
# Facts one call of the second read checks at once.
JUDGE_BATCH = 20
# Search engines' results pages: a list of links, not a page that states anything.
SEARCH_HOSTS = {"google.com", "bing.com", "duckduckgo.com", "search.yahoo.com", "search.brave.com", "yandex.com"}

JUDGE_INSTRUCTIONS = """You check facts against the web pages they came from, for a student's call notes. Each item gives a fact, what it is about, and a passage copied from the page the fact cites: the page's own words, cut by software, not by whoever wrote the fact.

For each item, answer supported=true only when the passage itself states everything the fact says:
- the same subject: the fact is about the company, product, or person the passage says it about, not a competitor, another product, or another person on the same page;
- the same numbers with the same units, on the same thing;
- the same things: no swapped technology, material, customer, investor, or place;
- the same yes or no: a fact that drops or adds a "not", "no", or "without" is not supported.
Paraphrase, shortening, and note style are fine. Adding anything the passage does not say, even something true, is not. When unsure, answer false.

Reply with exactly one JSON object and nothing else:
{"verdicts": [{"id": "...", "supported": true, "why": "under 15 words"}]}"""

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
Start with the company's own site: product pages, specifications, customer and case study pages, documentation, blog posts, and careers pages and job posts (these name the tools and methods they use and the roles they need). Then look beyond it: patents (patents.google.com), papers and theses by the founders and staff (arXiv, Google Scholar, university pages), grant awards (sbir.gov, nsf.gov, nih.gov), GitHub, conference talks, accelerator and investor pages, funding announcements, news, and the sites of the companies that sell something similar to the same customers.

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
- person: the full name of the one person a fact is about (a founder's background, a hire), as the page writes it; empty otherwise. The quote must be the sentence or heading that names that person, not a sentence beside it. A fact from a page that is not about the company (a paper, a thesis, a lab page) must name its person, and that person must also appear in another fact from a page that does name the company.
- Up to 5 facts per section. Fewer true, specific facts are better than more vague ones. Leave a section empty rather than guess.
- gaps: up to 8 things about the company's work the student would want to know that no page you found states, as short phrases with no numbers (for example "which suppliers they use for key parts"). The student will ask about them on the call.
- Plain text, no markdown, no em dashes or en dashes.

## Output
Reply with exactly one JSON object and nothing else:
{{"facts": [{{"section": "one of: {sections}", "text": "", "source_url": "https://...", "quote": "", "person": "", "competitor": ""}}], "gaps": [""]}}"""


class ResearchUnavailable(RuntimeError):
    """No research agent is set up on this computer, or in this app."""


def research_agent() -> str:
    """The CLI that researches companies: its own setting, else the deep search's, else Claude Code."""
    for env in (AGENT_ENV, DISCOVERY_ENV):
        chosen = os.environ.get(env, "").strip()
        if chosen in RUNNERS:
            return chosen
    return "claude-code"


def available_agent(preferred: str | None = None) -> tuple[str, str]:
    """The agent to run and a note when it is not the one chosen, because that one is not installed."""
    chosen = preferred or research_agent()
    if _cli_available(_cli_binary(chosen)):
        return chosen, ""
    for other in RUNNERS:
        if other != chosen and _cli_available(_cli_binary(other)):
            return other, f"{chosen} is not installed here, so {other} did the research"
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


def _normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    text = text.replace("’", "'").replace("‘", "'")
    # "don't" is "do not", and 45% is "45 percent": the same words either way.
    return re.sub(r"n't", " not", text).replace("%", " percent ")


# A number keeps its decimals (0.1, 725.00); 1,500 and 1500 are the same number.
_TOKEN = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+")


def _tokens(text: str) -> list[str]:
    return [token.replace(",", "") if token[0].isdigit() else token for token in _TOKEN.findall(_normalized(text))]


def _numbers(tokens: list[str]) -> set[str]:
    return {token for token in tokens if token[0].isdigit()}


_NEGATION_WORDS = frozenset("not no never without none nor neither cannot nobody nothing".split())
def _negated(text: str) -> bool:
    """Whether the words say "not" anywhere (the interviewer's notes use it; facts get the second read)."""
    return any(token in _NEGATION_WORDS for token in _tokens(text))


# Words too common to say anything about whether a page backs a fact.
_STOPWORDS = frozenset(
    "about above after also among and any are around based been before being between both but can could each "
    "every for from has have into its it's more most much must only other over same such than that their them "
    "then there these they this those through under until upon very were what when where which while with "
    "within without would your uses used using make makes made built builds build company companys team "
    "offers offer provides provide includes include including called named "
    "the was but not you our who how why all may out off per via did get got let put one two new own too yet nor "
    "for any few his her him she led use tool tools listed list lists".split()
)

# Words a fact may open a sentence with that are grammar, not a name. Any other
# capitalized word must be near the quote, wherever it stands. A person's title
# (CEO, Director, Founder) is not here: CEO and CTO are different claims.
_COMMON_STARTERS = frozenset(
    "a an the its their his her this that these those it they he she we our each every both all other others some "
    "most many one two three four five six first second third as at by for from in on with after before during "
    "since until over under about per and but or also then now today recently currently previously formerly "
    "earlier later founded launched announced raised holds uses used runs sold offers ships joined appointed named "
    "hired published filed awarded won received led leads partnered deployed released introduced unveiled "
    "developed designed built based listed described degrees degree education open roles role job jobs posts "
    "patent patents paper papers grant grants customers customer pilots pilot partners products product "
    "software hardware tools stack team company headquarters employees staff engineers engineering research "
    "production manufacturing price pricing specs specifications accuracy payload speed weight size power range "
    "co inventor investors seed series funding round work area areas features feature includes including supports support "
    "says said claims claimed calls called describes described bills markets positions reports states".split()
)


def _stems(word: str) -> set[str]:
    """The word and its plain forms: writes and writing both come from write."""
    stems = {word}
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            stems |= {word[: -len(suffix)], word[: -len(suffix)] + "e"}
    return stems


def _verb_on_page(word: str, page_stems: set[str]) -> bool:
    """A sentence's opening verb ("Builds", "Studied") the page uses in another form ("building", "studies").

    Only a word in a verb's form counts, and only through another form of it:
    a name that opens the sentence ("Epson invested") must still be near the quote.
    """
    if not word.isalpha() or not word.endswith(("s", "ed", "ing")):
        return False
    return bool((_stems(word) - {word}) & page_stems)


# "$12M", "5kg", "150W", "0.1mm": a number and its unit, held to the number and unit checks.
_NUMBER_WITH_UNIT = re.compile(r"^\$?\d+(?:[.,]\d+)*[A-Za-z]{1,3}$")


def _is_name_token(token: str) -> bool:
    """Capitalized, an acronym, or a model number like 3D."""
    has_digit = any(char.isdigit() for char in token)
    has_alpha = any(char.isalpha() for char in token)
    return token[:1].isupper() or any(char.isupper() for char in token[1:]) or (has_digit and has_alpha)


def _name_runs(text: str) -> list[tuple[list[str], bool]]:
    """Runs of name-like words separated only by spaces, each with whether it opens a sentence."""
    text = str(text or "")
    runs: list[tuple[list[str], bool]] = []
    last_end = -1
    for match in re.finditer(r"[^\W_][\w.+-]*", text):
        token = match.group(0).strip(".+-")
        if not token or not _is_name_token(token) or not any(char.isalpha() for char in token) or _NUMBER_WITH_UNIT.match(token):
            last_end = -1
            continue
        gap = text[last_end:match.start()] if last_end >= 0 else None
        if gap is not None and gap.strip() == "" and runs:
            runs[-1][0].append(token)
        else:
            before = text[:match.start()].rstrip()
            runs.append(([token], not before or before[-1] in ".:;!?\"'(["))
        # A token that ends a sentence ("in Austin.") ends its run too.
        last_end = match.end() if not match.group(0).endswith(".") else -1
    return runs


def _company_names(company: str) -> list[str]:
    """The company's name, and the name without its legal suffix ("Acme Robotics, Inc." is "Acme Robotics")."""
    words = _tokens(company)
    while words and words[-1] in LEGAL_SUFFIXES:
        words = words[:-1]
    names = [" ".join(_tokens(company)), " ".join(words)]
    return [name for name in dict.fromkeys(names) if name]


# What ends a sentence, when the next one starts with a capital or a digit.
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]*[A-Z0-9])")
_ABBREVIATIONS = frozenset(
    "inc ltd co corp llc dr mr mrs ms st jr sr vs etc prof no fig approx e.g i.e u.s "
    "jan feb mar apr jun jul aug sep sept oct nov dec".split()
)
_MONTHS = frozenset(
    "jan january feb february mar march apr april may jun june jul july aug august sep sept september "
    "oct october nov november dec december".split()
)
# A number means little without its unit: 4.5M is not 4.5 billion, 5 kg is not 5 lb.
_UNITS = {
    "k": "k", "thousand": "k", "m": "m", "mn": "m", "million": "m", "b": "bn", "bn": "bn", "billion": "bn",
    "tn": "tn", "trillion": "tn", "percent": "percent", "kg": "kg", "kilogram": "kg", "g": "g", "gram": "g",
    "mg": "mg", "lb": "lb", "lbs": "lb", "pound": "lb", "oz": "oz", "ounce": "oz", "mm": "mm", "cm": "cm",
    "km": "km", "ft": "ft", "foot": "ft", "feet": "ft", "mile": "mile", "mph": "mph", "hz": "hz", "khz": "khz",
    "mhz": "mhz", "ghz": "ghz", "v": "v", "kv": "kv", "w": "w", "kw": "kw", "mw": "mw", "kwh": "kwh",
    "mwh": "mwh", "gb": "gb", "mb": "mb", "tb": "tb", "nm": "nm", "rpm": "rpm", "psi": "psi", "fps": "fps",
    "ms": "ms", "sec": "sec", "second": "sec", "min": "min", "minute": "min", "hr": "hr", "hour": "hr",
    "day": "day", "week": "week", "month": "month", "year": "year",
}
# Past tenses the page may word another way ("wrote" for "authored"), which -ed and -ing do not show.
def _sentences(line: str) -> list[str]:
    """The line cut into sentences. A period after an abbreviation or an initial does not end one."""
    parts: list[str] = []
    last = 0
    for match in _SENTENCE_END.finditer(line):
        words = line[last:match.start()].split()
        before = words[-1].rstrip(".!?\"')]").casefold() if words else ""
        if len(before) <= 1 or before in _ABBREVIATIONS:
            continue
        parts.append(line[last:match.end()])
        last = match.end()
    parts.append(line[last:])
    return parts


def _unit_pairs(tokens: list[str]) -> set[tuple[str, str]]:
    """Each number that is followed by a unit, with the unit in one spelling."""
    pairs = set()
    for number, later in zip(tokens, tokens[1:]):
        if not number[0].isdigit():
            continue
        unit = _UNITS.get(later) or _UNITS.get(later[:-1] if later.endswith("s") else later)
        if unit:
            pairs.add((number, unit))
    return pairs


def _is_unit(word: str) -> bool:
    return word in _UNITS or word.rstrip("s") in _UNITS


def _word_near(word: str, near: set[str]) -> bool:
    """The word, a plain form of it, or a word that starts the same way ("written" and "write") is in ``near``."""
    if word in near or word.rstrip("s") in near or _stems(word) & near:
        return True
    return any(
        len(other) >= 4 and len(word) >= 4 and other[:4] == word[:4]
        and len(os.path.commonprefix([word, other])) >= 0.7 * min(len(word), len(other))
        for other in near
    )


class _Page:
    """One fetched page: its raw HTML for the link to the company's site, its visible words for everything else.

    Every word also knows its line (a paragraph or heading) and its sentence,
    so a fact is held to what the quote's own sentence and paragraph say.
    """

    def __init__(self, result: FetchResult) -> None:
        self.url = result.url
        self.raw = result.text
        parser = _PageParser()
        try:
            parser.feed(result.text)
            parser.close()
        except Exception:  # noqa: BLE001 - a page too broken to parse still has its raw text
            parser.lines = [result.text]
        self.lines = [" ".join(str(line).split()) for line in [*parser.lines, *parser.json_ld]]
        self.tokens: list[str] = []
        self._sentence_spans: list[tuple[int, int]] = []
        self._sentence_of: list[int] = []
        self._line_spans: list[tuple[int, int]] = []
        self._line_of: list[int] = []
        # Structured data states facts too (a product's name, a founder's role).
        for line_number, line in enumerate([*parser.lines, *parser.json_ld]):
            start = len(self.tokens)
            sentences = _sentences(line)
            for sentence in sentences:
                words = _tokens(sentence)
                self._sentence_spans.append((len(self.tokens), len(self.tokens) + len(words)))
                self._sentence_of += [len(self._sentence_spans) - 1] * len(words)
                self._line_of += [line_number] * len(words)
                self.tokens += words
            self._line_spans.append((start, len(self.tokens)))
        self.joined = f" {' '.join(self.tokens)} "
        # Every word on the page in its plain forms, for a sentence's opening word.
        self.stems = {stem for token in set(self.tokens) for stem in _stems(token)}
        # The page's title and dateline, which every fact on it may lean on.
        self.top = set(self.tokens[: self._line_spans[min(TOP_LINES, len(self._line_spans)) - 1][1]]) if self._line_spans else set()

    def _index(self, offset: int) -> int:
        """The token at a match found at ``offset`` (the space before it) in ``joined``."""
        return self.joined.count(" ", 0, offset + 1) - 1

    def find_quote(self, quote: str) -> tuple[int, int] | None:
        """Where the quote sits on the page, as a token span, or None.

        Word for word after case and punctuation; a quote trimmed with "..." is
        matched piece by piece, in order and close together. A trimmed-away
        piece too short to look for may not carry a "not".
        """
        every = [_tokens(piece) for piece in re.split(r"\.\.\.|…", str(quote or ""))]
        pieces = [piece for piece in every if len(piece) >= PIECE_WORDS]
        if sum(len(piece) for piece in pieces) < MIN_QUOTE_WORDS:
            return None
        if any(token in _NEGATION_WORDS for piece in every if len(piece) < PIECE_WORDS for token in piece):
            return None
        start = end = None
        search_from = 0
        for piece in pieces:
            offset = self.joined.find(f" {' '.join(piece)} ", search_from)
            if offset < 0:
                return None
            first = self._index(offset)
            if end is not None and first - end > PIECE_GAP:
                return None
            start = first if start is None else start
            end = first + len(piece)
            search_from = offset + 1
        return (start, end) if start is not None else None

    def near(self, span: tuple[int, int]) -> set[str]:
        return set(self.tokens[max(0, span[0] - WINDOW): span[1] + WINDOW])

    def _sentence_tokens(self, span: tuple[int, int]) -> list[str]:
        first, last = self._sentence_of[span[0]], self._sentence_of[span[1] - 1]
        return self.tokens[self._sentence_spans[first][0]: self._sentence_spans[last][1]]

    def sentence_has(self, span: tuple[int, int], name: str) -> bool:
        """Whether the sentence the quote is in names ``name``."""
        words = _tokens(name)
        return bool(words) and f" {' '.join(words)} " in f" {' '.join(self._sentence_tokens(span))} "

    def has_phrase(self, text: str) -> bool:
        words = _tokens(text)
        return bool(words) and f" {' '.join(words)} " in self.joined

    def passage(self, span: tuple[int, int]) -> dict[str, str]:
        """What the second read sees: the quote's paragraph whole, the lines around it, and the top of the page.

        The quote's own lines always come whole, so the second read never
        judges a fact on a passage cut off before its quote. Around them, lines
        are added nearest first over the stretch the word checks read (a
        dateline, a name heading), until PASSAGE_CHARS.
        """
        if not self.lines:
            return {"top": "", "passage": ""}
        first, last = self._line_of[span[0]], self._line_of[span[1] - 1]
        low = min(self._line_of[max(0, span[0] - WINDOW)], max(0, first - PASSAGE_BEFORE))
        high = max(self._line_of[min(len(self.tokens) - 1, span[1] - 1 + WINDOW)], min(len(self.lines) - 1, last + PASSAGE_AFTER))
        core = "\n".join(self.lines[first:last + 1])[:QUOTE_LINE_CHARS]
        before, after = list(reversed(self.lines[low:first])), self.lines[last + 1:high + 1]
        room = PASSAGE_CHARS - len(core)
        above: list[str] = []
        below: list[str] = []
        for index in range(max(len(before), len(after))):
            for line, into in ((before[index] if index < len(before) else None, above), (after[index] if index < len(after) else None, below)):
                if line is not None and len(line) + 1 <= room:
                    into.append(line)
                    room -= len(line) + 1
        passage = "\n".join([*reversed(above), core, *below])
        return {"top": " | ".join(self.lines[:TOP_LINES])[:TOP_CHARS], "passage": passage}

    def missing(self, text: str, span: tuple[int, int], company: str, person: str = "") -> str:
        """What the fact says that the quote and the lines around it do not, or "".

        Numbers, units, names, and months are held to the quote and the lines
        around it, and to the page's title and dateline. That the number belongs
        to the thing the fact says (A1's payload, not B7's) is the second read's
        question. The company's name may stand anywhere: the company check
        settles it. So may ``person``'s, when the page is that one person's own
        profile ("She led...").
        """
        words = _tokens(text)
        window = self.tokens[max(0, span[0] - WINDOW): span[1] + WINDOW]
        near = set(window) | self.top
        numbers = sorted(_numbers(words) - near)
        if numbers:
            return f"the quote and the lines around it do not state {', '.join(numbers)}"
        units = sorted(f"{number} {unit}" for number, unit in _unit_pairs(words) - _unit_pairs(window))
        if units:
            return f"the quote and the lines around it do not state {', '.join(units)}"
        local_words = [*window, *self.top]
        local = set(local_words)
        sentence = self._sentence_tokens(span)
        # A month is held like a number, in the spelling the page uses ("Sept." for September).
        months = sorted(
            word for word in words
            if word in _MONTHS and word != "may" and not any(len(other) >= 3 and (other.startswith(word) or word.startswith(other)) for other in local | set(sentence))
        )
        if months:
            return f"the quote and the lines around it do not state {', '.join(months)}"
        own = set(_tokens(company))
        if len(_tokens(person)) >= 2 and self.has_phrase(person):
            own |= set(_tokens(person))
        absent: list[str] = []
        for tokens, opens_sentence in _name_runs(text):
            first = _tokens(tokens[0])
            # "Co-founder Dana Ortiz", "Writes C++": a sentence may open on a
            # plain word, which is grammar, not a name.
            grammar = opens_sentence and bool(first) and all(
                word in _COMMON_STARTERS or any(stem in local for stem in _stems(word)) or _verb_on_page(word, self.stems)
                for word in first
            )
            absent += [
                token for token in (tokens[1:] if grammar else tokens)
                if not all(word in local or word in own or word in _MONTHS for word in _tokens(token))
            ]
            # "First Round Capital" is one name: what follows its first word must stand together.
            tail = [word for token in tokens[1:] for word in _tokens(token)]
            if grammar and len(tokens) >= 3 and not set(tail) <= own and f" {' '.join(tail)} " not in f" {' '.join(local_words)} ":
                absent.append(" ".join(tokens[1:]))
        if absent:
            return f"the quote and the lines around it do not name {', '.join(dict.fromkeys(absent[:4]))}"
        # Units are held to their numbers above, and a word opening the fact ("Says", "Listed") is grammar.
        content = [
            word for index, word in enumerate(words)
            if len(word) >= 3 and not word[0].isdigit() and word not in _STOPWORDS and not _is_unit(word) and word not in _MONTHS
            and not (index == 0 and word in _COMMON_STARTERS)
        ]
        missing = [word for word in content if not (_word_near(word, near) or word in own)]
        if content and len(missing) / len(content) > WORDS_MISSING_SHARE:
            return "the quote and the lines around it do not say most of what the fact says"
        return ""

    def names_company(self, company: str, domain: str) -> bool:
        """The company's name in the visible text, or a link to its website. Never true on an empty name or domain."""
        if domain and re.search(rf"(?<![\w.-]){re.escape(domain)}(?![\w-])", self.raw, re.IGNORECASE):
            return True
        return any(self.has_phrase(name) for name in _company_names(company))


def _own_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return bool(domain) and (host == domain or host.endswith(f".{domain}"))


def _named_by_host(url: str, name: str) -> bool:
    """Whether the page's own address is the company's ("voltarm.example" for Voltarm): a page that never repeats its name."""
    key = "".join(_tokens(name))
    labels = [label.replace("-", "") for label in (urlsplit(url).hostname or "").lower().removeprefix("www.").split(".")[:-1]]
    return bool(key) and key in labels


def _refused_source(url: str) -> str:
    if public_web_url_error(url):
        return "its source is not a public web page"
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if host in SEARCH_HOSTS and (urlsplit(url).path.startswith(("/search", "/url")) or host != "google.com"):
        return "its source is a search results page"
    if any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_HOSTS):
        return f"its source ({host}) is behind a login or sells data"
    if urlsplit(url).path.lower().endswith(".pdf"):
        return "its source is a PDF, which this check cannot read; cite the page it is linked from"
    return ""


class _Reader:
    """Fetches each cited page once, within a budget, rendering it in a browser when its HTML has no text."""

    def __init__(self, fetcher: SafeFetcher, renderer: Any = None) -> None:
        self.fetcher = fetcher
        self.renderer = renderer
        self._plain: dict[str, FetchResult] = {}
        self._pages: dict[str, _Page] = {}
        self._rendered: dict[str, _Page | None] = {}

    def over_budget(self, url: str) -> bool:
        return url not in self._plain and len(self._plain) >= MAX_PAGES

    def fetch(self, url: str) -> FetchResult:
        if url not in self._plain:
            # Every redirect is checked like the cited link: a page may not send the reader to a search or data-broker site.
            self._plain[url] = self.fetcher.fetch(url, same_host_only=False, hop_check=lambda hop: _refused_source(hop) or None)
        return self._plain[url]

    def page(self, url: str) -> _Page:
        if url not in self._pages:
            self._pages[url] = _Page(self.fetch(url))
        return self._pages[url]

    def rendered(self, url: str) -> _Page | None:
        """The page after its scripts ran, for sites whose HTML is an empty shell."""
        if self.renderer is None or getattr(self.renderer, "unavailable", ""):
            return None
        if url not in self._rendered:
            # A render that failed still took its time, so every try counts.
            if len(self._rendered) >= MAX_RENDERS:
                return None
            try:
                result = self.renderer.render(url)
            except Exception:  # noqa: BLE001 - a browser that fails leaves the plain page as the answer
                result = None
            self._rendered[url] = _Page(FetchResult(result[0], 200, result[1])) if result else None
        return self._rendered[url]


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


_NO_SPAN = (0, 0)


def _check_one(
    fact: dict[str, Any], reader: _Reader, company: str, domain: str,
) -> tuple[str, str, _Page | None, bool, tuple[int, int]]:
    """The word checks: ('passed' | 'unchecked' | 'unlinked' | 'refused', reason, the page the quote was found on,
    whether it names the company, and where the quote sits on it).

    'passed' is a fact whose words are there, waiting for the second read.
    'unlinked' is one whose quote is on a page that names only a person. The
    fourth part is true for a fact from a page that names the company or is its
    own site.
    """
    url = fact["source_url"]
    refused = _refused_source(url)
    if refused:
        return "refused", refused, None, False, _NO_SPAN
    if len(_tokens(fact["quote"])) < MIN_QUOTE_WORDS:
        return "refused", "it quotes too little of its page to check", None, False, _NO_SPAN
    if reader.over_budget(url):
        return "refused", f"over the {MAX_PAGES} pages one run checks", None, False, _NO_SPAN
    result = reader.fetch(url)
    if result.error:
        hop = _refused_source(result.url)
        if hop and hop == result.error:
            return "refused", f"it sends the reader on to a page it cannot use: {hop}", None, False, _NO_SPAN
        return "refused", "its source points at a private or local address" if result.error == "private" else f"its source did not load ({result.error})", None, False, _NO_SPAN
    # Where the page ended up, not what was cited: a link on the company's site may lead anywhere.
    own_site = _own_site(url, domain) and _own_site(result.url, domain)
    if result.status in UNVERIFIABLE_STATUSES:
        # A site that turns plain requests away often shows the page to a real
        # browser; then the same checks run on what the browser read.
        rendered = reader.rendered(result.url)
        span = rendered.find_quote(fact["quote"]) if rendered is not None else None
        if span is None:
            if own_site:
                return "unchecked", f"the company's site turned the check away (HTTP {result.status})", None, False, _NO_SPAN
            return "refused", f"its site turned the check away (HTTP {result.status}) and is not the company's own", None, False, _NO_SPAN
        page = rendered
    elif result.status >= 400:
        return "refused", f"its source did not load (HTTP {result.status})", None, False, _NO_SPAN
    elif "pdf" in result.content_type.lower():
        return "refused", "its source is a PDF, which this check cannot read; cite the page it is linked from", None, False, _NO_SPAN
    else:
        page = reader.page(url)
        span = page.find_quote(fact["quote"])
        if span is None:
            rendered = reader.rendered(result.url)
            span = rendered.find_quote(fact["quote"]) if rendered is not None else None
            if span is None:
                return "refused", "the quoted words are not on its source page", None, False, _NO_SPAN
            page = rendered
    missing = page.missing(fact["text"], span, company)
    if missing:
        return "refused", missing, page, False, span
    if fact["section"] == COMPETITORS:
        # About another company: its passage names it. Whether the two compete
        # is the agent's call unless the quote's sentence names both.
        names = _company_names(fact["competitor"])
        window = f" {' '.join(page.tokens[max(0, span[0] - WINDOW): span[1] + WINDOW])} "
        if not (any(f" {' '.join(_tokens(name))} " in window for name in names) or _named_by_host(page.url, fact["competitor"])):
            return "refused", f"the quote and the lines around it do not name {fact['competitor']}", page, False, span
        together = own_site or any(page.sentence_has(span, name) for name in _company_names(company))
        return "passed", "named together on the page" if together else "picked as a competitor by the research agent", page, False, span
    if own_site or page.names_company(company, domain):
        return "passed", "", page, True, span
    return "unlinked", "its source does not name the company", page, False, span


def _second_read(items: list[dict[str, Any]], judge: Judge | None, instructions: str = JUDGE_INSTRUCTIONS) -> dict[str, tuple[bool, str]]:
    """The judge's verdict on each item it answered: (supported, why). An item it did not answer is missing.

    Call prep uses it too, with its own instructions, for the lines a model writes.
    """
    verdicts: dict[str, tuple[bool, str]] = {}
    if judge is None:
        return verdicts
    for start in range(0, len(items), JUDGE_BATCH):
        batch = items[start:start + JUDGE_BATCH]
        content = json.dumps({"items": batch}, ensure_ascii=False)
        for _attempt in range(2):
            try:
                answers = CliAgentProvider.extract_json(judge(instructions, content)).get("verdicts")
            except Exception:  # noqa: BLE001 - an unreadable answer is asked once more, then left unanswered
                continue
            if isinstance(answers, list):
                for answer in answers:
                    if isinstance(answer, dict) and str(answer.get("id")) in {item["id"] for item in batch}:
                        verdicts[str(answer["id"])] = (answer.get("supported") is True, _clean(answer.get("why"), 200))
                break
    return verdicts


def check_brief(
    raw: str, target: dict[str, Any], *, fetcher: SafeFetcher, renderer: Any = None, judge: Judge | None = None,
) -> dict[str, Any]:
    """Parse the agent's reply and keep the facts their pages back up. Returns the brief to store.

    ``judge`` is the second read (JUDGE_INSTRUCTIONS). Without it, a fact whose
    words passed is kept marked "not checked".
    """
    parsed = CliAgentProvider.extract_json(raw)
    proposals = parsed.get("facts")
    if not isinstance(proposals, list):
        raise ValueError("The research reply had no facts list")
    company = target["company"]
    domain = website_domain(target.get("website") or "")
    reader = _Reader(fetcher, renderer)
    kept: list[dict[str, Any]] = []
    # Facts whose words passed, each waiting for the second read: (fact, note, page, names the company, span, linked only by a person).
    waiting: list[tuple[dict[str, Any], str, _Page, bool, tuple[int, int], bool]] = []
    refused: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    counts = {key: 0 for key in SECTION_IDS}
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            continue
        fact = {
            "section": str(proposal.get("section") or "").strip().lower(),
            "text": _clean(proposal.get("text"), MAX_FACT_CHARS),
            "source_url": str(proposal.get("source_url") or "").strip(),
            "quote": _clean(proposal.get("quote"), MAX_QUOTE_CHARS),
            "person": _clean(proposal.get("person"), 200),
            "competitor": _clean(proposal.get("competitor"), 200),
        }
        if not fact["text"]:
            continue
        if fact["section"] != COMPETITORS:
            # Only a competitor fact is about another company; the field is no way round the name check.
            fact["competitor"] = ""

        def refuse(reason: str) -> None:
            refused.append({"section": fact["section"], "text": fact["text"], "source_url": fact["source_url"], "reason": reason})

        if index >= MAX_PROPOSALS:
            refuse(f"over the {MAX_PROPOSALS} facts one run checks")
            continue
        if fact["section"] not in SECTION_IDS:
            refuse(f"section {fact['section'] or '(none)'} is not one the research writes")
            continue
        if fact["section"] == COMPETITORS and not company_key(fact["competitor"]):
            refuse("a competitor fact must name the competitor")
            continue
        if fact["section"] == COMPETITORS and company_key(fact["competitor"]) == company_key(company):
            refuse("it names the company itself as its competitor")
            continue
        if (fact["section"], fact["text"].casefold()) in seen:
            continue
        seen.add((fact["section"], fact["text"].casefold()))
        if counts[fact["section"]] >= MAX_FACTS_PER_SECTION:
            refuse(f"over the {MAX_FACTS_PER_SECTION} facts one section holds")
            continue
        if re.search("[–—]", fact["text"]):
            fact["text"] = re.sub(r"\s*[–—]\s*", ", ", fact["text"])
        state, reason, page, names_company, span = _check_one(fact, reader, company, domain)
        if state == "refused":
            refuse(reason)
            continue
        if state == "unchecked":
            counts[fact["section"]] += 1
            kept.append({**fact, "checked": False, "note": reason})
            continue
        if state == "unlinked" and len(_tokens(fact["person"])) < 2:
            # A page that names neither the company nor a person there is about something else.
            refuse(reason)
            continue
        counts[fact["section"]] += 1
        waiting.append((fact, reason, page, names_company, span, state == "unlinked"))
    # The second read, for every fact whose words passed, in one or two calls.
    items = []
    for index, (fact, _note, page, _names, span, _unlinked) in enumerate(waiting):
        about = fact["competitor"] or fact["person"] or company
        items.append({"id": f"f{index}", "fact": fact["text"], "about": about, "page": page.url, **page.passage(span)})
    verdicts = _second_read(items, judge)
    confirmed: list[tuple[dict[str, Any], str, bool, bool]] = []
    for index, (fact, note, _page, names_company, _span, only_person) in enumerate(waiting):
        verdict = verdicts.get(f"f{index}")
        if verdict is None:
            if only_person:
                refuse_fact = {"section": fact["section"], "text": fact["text"], "source_url": fact["source_url"],
                               "reason": "its source does not name the company, and no second read could check it"}
                refused.append(refuse_fact)
                counts[fact["section"]] -= 1
                continue
            kept.append({**fact, "checked": False, "note": "its words are on the page, but no second read could check what it says"})
        elif not verdict[0]:
            refused.append({"section": fact["section"], "text": fact["text"], "source_url": fact["source_url"],
                            "reason": f"a second read of the page says it does not state this: {verdict[1] or 'no reason given'}"})
            counts[fact["section"]] -= 1
        else:
            confirmed.append((fact, note, names_company, only_person))
    # A founder's paper names the founder, not the company. It stays when a
    # confirmed fact about the company's team, from a page that does name the
    # company, names that same person. An investor or a competitor's staff
    # mentioned elsewhere do not tie anyone to the company's team.
    ties = " ".join(f"{fact['text']} {fact['quote']}" for fact, _note, names, only in confirmed if names and not only and fact["section"] == "team")
    linked_words = f" {' '.join(_tokens(ties))} "
    for fact, note, _names, only_person in confirmed:
        if not only_person:
            kept.append({**fact, "checked": True, "note": note})
            continue
        person_words = " ".join(_tokens(fact["person"]))
        if len(person_words.split()) >= 2 and f" {person_words} " in linked_words:
            kept.append({**fact, "checked": True, "note": ""})
        else:
            counts[fact["section"]] -= 1
            refused.append({
                "section": fact["section"], "text": fact["text"], "source_url": fact["source_url"],
                "reason": "its source does not name the company" if not person_words
                else f"its source does not name the company, and no confirmed fact about the team ties {fact['person']} to it",
            })
    order = {key: index for index, key in enumerate(SECTION_IDS)}
    kept.sort(key=lambda fact: order[fact["section"]])
    gaps = [_clean(gap, 200) for gap in parsed.get("gaps") or [] if isinstance(gap, str) and gap.strip()][:MAX_GAPS]
    return {"facts": kept, "gaps": gaps, "refused": refused[:60], "proposed": len(proposals)}


def brief_of(target: dict[str, Any]) -> dict[str, Any]:
    """The stored brief, or an empty one."""
    brief = target.get("tech_brief") or {}
    return brief if isinstance(brief, dict) else {}


def _when(stamp: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _checked_facts(brief: dict[str, Any]) -> list[dict[str, Any]]:
    return [fact for fact in brief.get("facts") or [] if isinstance(fact, dict) and fact.get("checked")]


def brief_is_fresh(target: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether the brief is recent and has a checked fact. One that kept none, or only facts not checked, is worth trying again."""
    written = _when(target.get("tech_brief_at")) if target.get("tech_brief_at") else None
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
    tried = _when(target.get("tech_brief_tried_at")) if target.get("tech_brief_tried_at") else None
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
        row = conn.execute("SELECT tech_brief_json FROM outreach_targets WHERE id=? AND user_id=?", (target_id, user_id)).fetchone()
        if row is None:
            raise OutreachNotFoundError(target_id)
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
            _log(conn, target_id, user_id, "tech_brief_failed", detail=f"Kept none: {summary}")
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
        _log(conn, target_id, user_id, "tech_brief_written", detail=summary)
    return get_target(conn, target_id, user_id=user_id)


def record_error(conn: sqlite3.Connection, target_id: str, *, user_id: str, error: Exception) -> None:
    """Keep why the last try failed on the company, where the Research tab and call prep show it."""
    message = " ".join(str(error).split())[:1_000] or type(error).__name__
    with conn:
        conn.execute(
            "UPDATE outreach_targets SET tech_brief_error=?, updated_at=? WHERE id=? AND user_id=?",
            (message, utc_now(), target_id, user_id),
        )
        _log(conn, target_id, user_id, "tech_brief_failed", detail=message[:500])


def text_model(provider_factory: Callable[[str, str], Any], provider: str | None, fallback: str) -> Judge:
    """The model for the second read: the call prep writer (outreach_drafting.resolve_provider), or,
    when that is the no-AI template, the research agent's own CLI, which research needs anyway."""
    from .agent_providers import provider_catalog
    from .outreach_drafting import resolve_provider

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
            _log(conn, target_id, user_id, "tech_brief_queued", detail=reason)
        else:
            conn.execute("UPDATE job_queue SET state='cancelled', updated_at=? WHERE id=?", (utc_now(), queued["id"]))
    return get_target(conn, target_id, user_id=user_id)


def due_for_research(conn: sqlite3.Connection, *, user_id: str, only_replied: bool, now: datetime | None = None) -> list[str]:
    """Targets whose brief is missing, stale, or empty: those that replied, or every one."""
    from .outreach import CALL_PREP_STATUSES

    rows = conn.execute(
        "SELECT id, status, tech_brief_at, tech_brief_json FROM outreach_targets WHERE user_id=? ORDER BY company COLLATE NOCASE",
        (user_id,),
    ).fetchall()
    return [
        row[0] for row in rows
        if (not only_replied or row[1] in CALL_PREP_STATUSES)
        and not brief_is_fresh({"tech_brief_at": row[2], "tech_brief": json.loads(row[3] or "{}")}, now)
    ]
