"""The quote check: is a fact's quote on the page it cites, and does the page support what the fact says.

The engine behind company research (outreach_research.py), the interviewer's
profile notes (outreach_interviewer.py) and the call-prep line check
(outreach_call_prep.py). It knows nothing about jobs, agents or stored briefs.
Given a research agent's reply and the web it cites, ``check_brief`` keeps the
facts their pages back up, in two steps:

1. Word checks (``ResearchPage``, ``_check_one``): the page loads and is a page
   the check may read, the quote is on it word for word once case and
   punctuation are set aside, every number with its unit and every name in the
   fact is in the quote or the lines around it, most of its other words are
   too, and the page names the company (or a person a kept team fact ties to it).
2. A second read (``second_read``): a separate model call that sees only the
   fact and a passage Python cut from the page, and says whether the passage
   states exactly that (JUDGE_INSTRUCTIONS).

The limits (MAX_PROPOSALS, MAX_PAGES, MAX_RENDERS, FETCH_SECONDS, CHECK_SECONDS,
JUDGE_BATCH and the rest) are this module's: a test that changes one patches it
here. outreach_research.py's module docstring tells the whole story, from the
agent's prompt to the stored brief.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from typing import Any, Callable
from urllib.parse import urlsplit

from .integrations.agent_providers import CliAgentProvider
from .contact_names import website_domain
from .mail.trust import FREEMAIL, registrable_domain
from .outreach_contacts import PageParser
from .outreach_email_search import BLOCKED_HOSTS
from .outreach_identity import LEGAL_SUFFIXES, company_key, is_institution, is_platform_host, names_host
from .integrations.web_fetch import UNVERIFIABLE_STATUSES, FetchResult, SafeFetcher, public_web_url_error

# Sends instructions and content to a model and returns its reply.
Judge = Callable[[str, str], str]

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
TEAM = "team"
MAX_FACTS_PER_SECTION = 5
MAX_FACT_CHARS = 300
MAX_QUOTE_CHARS = 600
MAX_GAPS = 8
# Bounds on one run's checking, so a long reply cannot hold the worker for hours.
MAX_PROPOSALS = 80
MAX_PAGES = 40
MAX_RENDERS = 8
# Time bounds, so one server that sends a byte at a time cannot hold the worker's only thread: one
# page is read for at most FETCH_SECONDS, and once a run has spent CHECK_SECONDS on its pages
# the facts not yet read are left out.
FETCH_SECONDS = 30
CHECK_SECONDS = 15 * 60
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

An item with a "competitor" field is about that other company, and needs a second answer, "rivals": true only when the passage itself says the item's company and that competitor sell against each other: they compete for the same customers, or one is an alternative to the other. It is false when the passage shows the competitor as the company's customer, partner, supplier, investor, or acquirer, when the two are only named together, or when "competing" is about someone else (a customer choosing one of several vendors, a third company). If the passage shows the competitor is the company's customer, partner, supplier, investor, or acquirer, answer supported=false too.

Reply with exactly one JSON object and nothing else:
{"verdicts": [{"id": "...", "supported": true, "rivals": false, "why": "under 15 words"}]}
("rivals" only on an item with a "competitor" field.)"""


# Why a competitor fact is kept: its sentence says the two compete and the second read agrees, or only the agent picked it.
COMPETE_NOTE = "the quoted sentence says the two compete"
PICK_NOTE = "picked as a competitor by the research agent"
# A gap is a short phrase about what no page states. These are what a page could have talked an agent into
# copying out of this computer instead: an address, a link, a path, a key, or a long unbroken token.
_GAP_UNSAFE = re.compile(
    r"@|://|\bwww\.|[A-Za-z]:[\\/]|(?:^|\s)~?/[\w.-]+/|\\\\|"
    r"\b(?:sk|pk|ghp|gho|xox[abp]|AKIA|AIza)[-_A-Za-z0-9]{8,}",
    re.IGNORECASE,
)
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
MAX_GAP_WORDS = 14


def _leaks(text: str) -> bool:
    return bool(_GAP_UNSAFE.search(text) or _LONG_TOKEN.search(text))


def safe_gap(text: str) -> bool:
    """Whether a gap is a plain phrase: no address, link, path, key, or long token in it.

    The prompt asks for short phrases with no numbers, so a digit is out: that
    also rules out a phone number, a street address, and a token in groups
    ("3f9a1c 77be20"). A run of four same-length words ("abcd efgh ijkl mnop",
    the shape of an app password) and a long sentence are out too. This is a
    second line of defense: the research agent cannot read this computer's
    files at all (available_agent), and no filter catches every way to spell out a secret.
    """
    words = re.findall(r"[^\W_]+", text)
    if len(words) > MAX_GAP_WORDS or any(char.isdigit() for char in text):
        return False
    run = 1
    for before, word in zip(words, words[1:]):
        run = run + 1 if len(word) == len(before) and len(word) >= 4 else 1
        if run >= 4:
            return False
    return not _leaks(text)


def _normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    text = text.replace("’", "'").replace("‘", "'")
    # "don't" is "do not", and 45% is "45 percent": the same words either way.
    return re.sub(r"n't", " not", text).replace("%", " percent ")


# A number keeps its decimals (0.1, 725.00); 1,500 and 1500 are the same number.
_TOKEN = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+")


def word_tokens(text: str) -> list[str]:
    return [token.replace(",", "") if token[0].isdigit() else token for token in _TOKEN.findall(_normalized(text))]


def number_tokens(tokens: list[str]) -> set[str]:
    return {token for token in tokens if token[0].isdigit()}


# Words that say two companies compete, in the plain forms the page's tokens have.
_COMPETITION_WORDS = frozenset(
    "compete competes competing competed competitor competitors competition competitive rival rivals rivalry vs versus unlike".split()
)
_NEGATION_WORDS = frozenset("not no never without none nor neither cannot nobody nothing".split())
def says_not(text: str) -> bool:
    """Whether the words say "not" anywhere (the interviewer's notes use it; facts get the second read)."""
    return any(token in _NEGATION_WORDS for token in word_tokens(text))


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


def phrase_in(phrase: list[str], *runs: list[str]) -> bool:
    """Whether the words stand together, in order, within one of the runs (never across two of them)."""
    needle = f" {' '.join(phrase)} "
    return any(needle in f" {' '.join(run)} " for run in runs)


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


def company_names(company: str) -> list[str]:
    """The company's name, and the name without its legal suffix ("Acme Robotics, Inc." is "Acme Robotics")."""
    words = word_tokens(company)
    while words and words[-1] in LEGAL_SUFFIXES:
        words = words[:-1]
    names = [" ".join(word_tokens(company)), " ".join(words)]
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


class ResearchPage:
    """One fetched page: its raw HTML for the link to the company's site, its visible words for everything else.

    Every word also knows its line (a paragraph or heading) and its sentence,
    so a fact is held to what the quote's own sentence and paragraph say.
    """

    def __init__(self, result: FetchResult) -> None:
        self.url = result.url
        self.raw = result.text
        parser = PageParser()
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
                words = word_tokens(sentence)
                self._sentence_spans.append((len(self.tokens), len(self.tokens) + len(words)))
                self._sentence_of += [len(self._sentence_spans) - 1] * len(words)
                self._line_of += [line_number] * len(words)
                self.tokens += words
            self._line_spans.append((start, len(self.tokens)))
        self.joined = f" {' '.join(self.tokens)} "
        # Every word on the page in its plain forms, for a sentence's opening word.
        self.stems = {stem for token in set(self.tokens) for stem in _stems(token)}
        # The page's title and dateline, which every fact on it may lean on.
        # Kept in page order too: a phrase is looked for in words that stand together on the page, never across the
        # seam between the lines near a quote and the title (a set's order would put unrelated words side by side).
        self.top_words = self.tokens[: self._line_spans[min(TOP_LINES, len(self._line_spans)) - 1][1]] if self._line_spans else []
        self.top = set(self.top_words)

    def _index(self, offset: int) -> int:
        """The token at a match found at ``offset`` (the space before it) in ``joined``."""
        return self.joined.count(" ", 0, offset + 1) - 1

    def find_quote(self, quote: str) -> tuple[int, int] | None:
        """Where the quote sits on the page, as a token span, or None.

        Word for word after case and punctuation; a quote trimmed with "..." is
        matched piece by piece, in order and close together. The pieces of
        PIECE_WORDS words or more are found first. A shorter piece ("$60M")
        cannot be found by itself, so it must sit between the pieces around it,
        and every piece of the quote is on the page or the quote is refused.
        A short piece may not carry a "not".
        """
        every = [word_tokens(piece) for piece in re.split(r"\.\.\.|…", str(quote or ""))]
        every = [piece for piece in every if piece]
        pieces = [piece for piece in every if len(piece) >= PIECE_WORDS]
        if sum(len(piece) for piece in pieces) < MIN_QUOTE_WORDS:
            return None
        if any(token in _NEGATION_WORDS for piece in every if len(piece) < PIECE_WORDS for token in piece):
            return None
        found: dict[int, tuple[int, int]] = {}
        end = None
        search_from = 0
        for number, piece in enumerate(every):
            if len(piece) < PIECE_WORDS:
                continue
            offset = self.joined.find(f" {' '.join(piece)} ", search_from)
            if offset < 0:
                return None
            first = self._index(offset)
            if end is not None and first - end > PIECE_GAP:
                return None
            end = first + len(piece)
            found[number] = (first, end)
            search_from = offset + 1
        start = min(first for first, _ in found.values())
        stop = max(last for _, last in found.values())
        # A short piece sits after the piece before it and before the piece after it, or, at either
        # end of the quote, within PIECE_GAP of the nearest piece.
        cursor = start
        for number, piece in enumerate(every):
            if number in found:
                cursor = found[number][1]
                continue
            later = [found[other][0] for other in range(number + 1, len(every)) if other in found]
            limit = later[0] if later else stop + PIECE_GAP
            low = cursor if any(other in found for other in range(number)) else max(0, start - PIECE_GAP)
            for at in range(low, min(limit, len(self.tokens)) - len(piece) + 1):
                if self.tokens[at: at + len(piece)] == piece:
                    start, stop, cursor = min(start, at), max(stop, at + len(piece)), at + len(piece)
                    break
            else:
                return None
        return (start, stop)

    def near(self, span: tuple[int, int]) -> set[str]:
        return set(self.tokens[max(0, span[0] - WINDOW): span[1] + WINDOW])

    def _sentence_tokens(self, span: tuple[int, int]) -> list[str]:
        first, last = self._sentence_of[span[0]], self._sentence_of[span[1] - 1]
        return self.tokens[self._sentence_spans[first][0]: self._sentence_spans[last][1]]

    def sentence_has(self, span: tuple[int, int], name: str) -> bool:
        """Whether the sentence the quote is in names ``name``."""
        words = word_tokens(name)
        return bool(words) and f" {' '.join(words)} " in f" {' '.join(self._sentence_tokens(span))} "

    def sentence_says_compete(self, span: tuple[int, int], companies: list[str], competitors: list[str]) -> bool:
        """Whether the quote's own sentence names both companies and uses a word of competition.

        "Chargebot partners with Voltarm" and a customer list name two companies
        and say nothing about competing.
        """
        joined = f" {' '.join(self._sentence_tokens(span))} "
        words = set(joined.split())
        named = all(
            any(f" {' '.join(word_tokens(name))} " in joined for name in names if word_tokens(name)) for names in (companies, competitors)
        )
        return named and (bool(words & _COMPETITION_WORDS) or " alternative to " in joined or " instead of " in joined)

    def has_phrase(self, text: str) -> bool:
        words = word_tokens(text)
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
        words = word_tokens(text)
        window = self.tokens[max(0, span[0] - WINDOW): span[1] + WINDOW]
        near = set(window) | self.top
        numbers = sorted(number_tokens(words) - near)
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
        own = set(word_tokens(company))
        if len(word_tokens(person)) >= 2 and self.has_phrase(person):
            own |= set(word_tokens(person))
        absent: list[str] = []
        for tokens, opens_sentence in _name_runs(text):
            first = word_tokens(tokens[0])
            # "Co-founder Dana Ortiz", "Writes C++": a sentence may open on a
            # plain word, which is grammar, not a name.
            grammar = opens_sentence and bool(first) and all(
                word in _COMMON_STARTERS or any(stem in local for stem in _stems(word)) or _verb_on_page(word, self.stems)
                for word in first
            )
            absent += [
                token for token in (tokens[1:] if grammar else tokens)
                if not all(word in local or word in own or word in _MONTHS for word in word_tokens(token))
            ]
            # "First Round Capital" is one name: what follows its first word must stand together.
            tail = [word for token in tokens[1:] for word in word_tokens(token)]
            if grammar and len(tokens) >= 3 and not set(tail) <= own and not phrase_in(tail, window, self.top_words):
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

    def names_company(self, company: str, domain: str, path: str = "") -> bool:
        """The company's name in the visible text, or a link to its website. Never true on an empty name or domain.

        On a shared host the website is one path on it (``path``), so only a link to that path counts.
        """
        if domain and re.search(rf"(?<![\w.-]){re.escape(domain + path)}(?![\w-])", self.raw, re.IGNORECASE):
            return True
        return any(self.has_phrase(name) for name in company_names(company))


def site_scope(website: str, company: str) -> tuple[str, str]:
    """The host a company's website stands for, and the path on it when the host is shared: ("", "") for none.

    A university lab's page (uni.edu/bovi-lab), a page on a platform
    (sites.google.com/view/acme, github.com/acme), or any path on a host the
    company's name is not in stands for that path only, never every page on the
    host. The rules are the inbox's (outreach_identity.site_domain).
    """
    host = website_domain(website)
    if not host or "." not in host or host in FREEMAIL:
        return "", ""
    text = str(website or "").strip()
    try:
        path = urlsplit(text if "//" in text else f"https://{text}").path.rstrip("/").lower()
    except ValueError:
        path = ""
    if names_host(company, host):
        return host, ""
    if path:
        return host, path
    if (is_platform_host(host) or is_institution(host)) and host == (registrable_domain(host) or host):
        return "", ""
    return host, ""


def is_own_site(url: str, scope: tuple[str, str]) -> bool:
    domain, path = scope
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not domain or not (host == domain or host.endswith(f".{domain}")):
        return False
    here = (parts.path or "/").lower()
    return not path or (host == domain and (here == path or here.startswith(f"{path}/")))


def _named_by_host(url: str, name: str) -> bool:
    """Whether the page's own address is the company's ("voltarm.example" for Voltarm): a page that never repeats its name."""
    key = "".join(word_tokens(name))
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

    def __init__(self, fetcher: SafeFetcher, renderer: Any = None, *, budget_seconds: float = CHECK_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.fetcher = fetcher
        self.renderer = renderer
        self.clock = clock
        self._stop_at = clock() + budget_seconds
        self._plain: dict[str, FetchResult] = {}
        self._pages: dict[str, ResearchPage] = {}
        self._rendered: dict[str, ResearchPage | None] = {}
        # Why a page the browser ended on may not be used, by the page that was asked for.
        self.render_refused: dict[str, str] = {}

    def out_of_time(self, url: str) -> bool:
        """Whether the run's time for reading pages is spent and this page is not one already read."""
        return url not in self._plain and self.clock() >= self._stop_at

    def over_budget(self, url: str) -> bool:
        return url not in self._plain and len(self._plain) >= MAX_PAGES

    def fetch(self, url: str) -> FetchResult:
        if url not in self._plain:
            # Every redirect is checked like the cited link: a page may not send the reader to a search or data-broker site.
            # Never longer than what is left of the run's time, so CHECK_SECONDS holds to within a second.
            left = max(1.0, self._stop_at - self.clock())
            self._plain[url] = self.fetcher.fetch(
                url, same_host_only=False, hop_check=lambda hop: _refused_source(hop) or None,
                deadline_seconds=min(FETCH_SECONDS, left),
            )
        return self._plain[url]

    def page(self, url: str) -> ResearchPage:
        if url not in self._pages:
            self._pages[url] = ResearchPage(self.fetch(url))
        return self._pages[url]

    def rendered(self, url: str) -> ResearchPage | None:
        """The page after its scripts ran, for sites whose HTML is an empty shell.

        A browser follows redirects on its own, so the page it ended on is held to the
        same source rules as a link (no LinkedIn, data broker, search results, or PDF).
        """
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
            refused = _refused_source(result[0]) if result else ""
            if refused:
                self.render_refused[url] = refused
                result = None
            self._rendered[url] = ResearchPage(FetchResult(result[0], 200, result[1])) if result else None
        return self._rendered[url]


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


_NO_SPAN = (0, 0)


def _check_one(
    fact: dict[str, Any], reader: _Reader, company: str, scope: tuple[str, str],
) -> tuple[str, str, ResearchPage | None, bool, tuple[int, int]]:
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
    if len(word_tokens(fact["quote"])) < MIN_QUOTE_WORDS:
        return "refused", "it quotes too little of its page to check", None, False, _NO_SPAN
    if reader.over_budget(url):
        return "refused", f"over the {MAX_PAGES} pages one run checks", None, False, _NO_SPAN
    if reader.out_of_time(url):
        return "refused", "over the time one run spends reading pages", None, False, _NO_SPAN
    result = reader.fetch(url)
    if result.error:
        hop = _refused_source(result.url)
        if hop and hop == result.error:
            return "refused", f"it sends the reader on to a page it cannot use: {hop}", None, False, _NO_SPAN
        return "refused", "its source points at a private or local address" if result.error == "private" else f"its source did not load ({result.error})", None, False, _NO_SPAN
    # Where the page ended up, not what was cited: a link on the company's site may lead anywhere.
    own_site = is_own_site(url, scope) and is_own_site(result.url, scope)
    if result.status in UNVERIFIABLE_STATUSES:
        # A site that turns plain requests away often shows the page to a real
        # browser; then the same checks run on what the browser read.
        rendered = reader.rendered(result.url)
        span = rendered.find_quote(fact["quote"]) if rendered is not None else None
        if span is None:
            if result.url in reader.render_refused:
                return "refused", f"it sends the reader on to a page it cannot use: {reader.render_refused[result.url]}", None, False, _NO_SPAN
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
    # A browser may have ended on another host than the plain fetch did: own site is where the page read stands.
    own_site = own_site and is_own_site(page.url, scope)
    missing = page.missing(fact["text"], span, company)
    if missing:
        return "refused", missing, page, False, span
    if fact["section"] == COMPETITORS:
        # About another company: its passage names it. Whether the two compete
        # is the agent's call unless the quote's own sentence says so.
        names = company_names(fact["competitor"])
        window = f" {' '.join(page.tokens[max(0, span[0] - WINDOW): span[1] + WINDOW])} "
        if not (any(f" {' '.join(word_tokens(name))} " in window for name in names) or _named_by_host(page.url, fact["competitor"])):
            return "refused", f"the quote and the lines around it do not name {fact['competitor']}", page, False, span
        # Being named beside each other says nothing about competing (a partner, a customer, an investor),
        # and neither does the company's own site listing them. The quote's sentence must say it.
        says = page.sentence_says_compete(span, company_names(company), names)
        return "passed", COMPETE_NOTE if says else PICK_NOTE, page, False, span
    if own_site or page.names_company(company, *scope):
        return "passed", "", page, True, span
    return "unlinked", "its source does not name the company", page, False, span


def second_read(
    items: list[dict[str, Any]], judge: Judge | None, instructions: str = JUDGE_INSTRUCTIONS, rivals: dict[str, bool] | None = None,
) -> dict[str, tuple[bool, str]]:
    """The judge's verdict on each item it answered: (supported, why). An item it did not answer is missing.

    Call prep uses it too, with its own instructions, for the lines a model writes.
    ``rivals``, when given, gets each answered item's "rivals" answer (only ever true when it said true).
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
                        # What the judge says is kept on the student's screen, so it may not carry an address, link, path, or key either.
                        why = _clean(answer.get("why"), 200)
                        verdicts[str(answer["id"])] = (answer.get("supported") is True, why if not _leaks(why) else "")
                        if rivals is not None:
                            rivals[str(answer["id"])] = answer.get("rivals") is True
                break
    return verdicts


def check_brief(
    raw: str, target: dict[str, Any], *, fetcher: SafeFetcher, renderer: Any = None, judge: Judge | None = None,
    budget_seconds: float = CHECK_SECONDS, clock: Callable[[], float] = time.monotonic,
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
    scope = site_scope(target.get("website") or "", company)
    reader = _Reader(fetcher, renderer, budget_seconds=budget_seconds, clock=clock)
    kept: list[dict[str, Any]] = []
    # Facts whose words passed, each waiting for the second read: (fact, note, page, names the company, span, linked only by a person).
    waiting: list[tuple[dict[str, Any], str, ResearchPage, bool, tuple[int, int], bool]] = []
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
        state, reason, page, names_company, span = _check_one(fact, reader, company, scope)
        if state == "refused":
            refuse(reason)
            continue
        if state == "unchecked":
            counts[fact["section"]] += 1
            kept.append({**fact, "checked": False, "note": reason})
            continue
        if state == "unlinked" and (fact["section"] != TEAM or len(word_tokens(fact["person"])) < 2):
            # A page that names neither the company nor a person there is about something else. And a person's
            # own page (a paper, a thesis, an earlier company's page) speaks for the person, never for the company's
            # technology, customers, or funding: only the team section may rest on it.
            refuse(f"{reason}; a fact from a person's own page belongs in {TEAM}, not {fact['section']}"
                   if fact["section"] != TEAM and len(word_tokens(fact["person"])) >= 2 else reason)
            continue
        if state == "unlinked" and f" {' '.join(word_tokens(fact['person']))} " not in f" {' '.join(word_tokens(fact['text']))} ":
            # The note says whose page it is from, so the student never reads it as the company's own.
            refuse(f"its source is {fact['person']}'s own page, so the fact must name {fact['person']}")
            continue
        counts[fact["section"]] += 1
        waiting.append((fact, reason, page, names_company, span, state == "unlinked"))
    # The second read, for every fact whose words passed, in one or two calls.
    items = []
    for index, (fact, _note, page, _names, span, _unlinked) in enumerate(waiting):
        # Only a team fact is about a person; any other fact is asked of the company, whoever it names.
        about = fact["competitor"] or (fact["person"] if fact["section"] == TEAM else "") or company
        items.append({
            "id": f"f{index}", "fact": fact["text"], "about": about, "page": page.url,
            **({"competitor": fact["competitor"]} if fact["section"] == COMPETITORS else {}), **page.passage(span),
        })
    rivals: dict[str, bool] = {}
    verdicts = second_read(items, judge, rivals=rivals)
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
            if note == COMPETE_NOTE and not rivals.get(f"f{index}"):
                # The sentence has both names and a word of competition; the second read did not find the two selling against each other.
                note = PICK_NOTE
            confirmed.append((fact, note, names_company, only_person))
    # A founder's paper names the founder, not the company. It stays when a
    # confirmed fact about the company's team, from a page that does name the
    # company, names that same person. An investor or a competitor's staff
    # mentioned elsewhere do not tie anyone to the company's team.
    ties = " ".join(f"{fact['text']} {fact['quote']}" for fact, _note, names, only in confirmed if names and not only and fact["section"] == "team")
    linked_words = f" {' '.join(word_tokens(ties))} "
    for fact, note, _names, only_person in confirmed:
        if not only_person:
            kept.append({**fact, "checked": True, "note": note})
            continue
        person_words = " ".join(word_tokens(fact["person"]))
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
    gaps = [
        _clean(gap, 200) for gap in parsed.get("gaps") or []
        if isinstance(gap, str) and gap.strip() and safe_gap(_clean(gap, 200))
    ][:MAX_GAPS]
    return {"facts": kept, "gaps": gaps, "refused": refused[:60], "proposed": len(proposals)}
