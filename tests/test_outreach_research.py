"""Technical research on a company, and the pages that have to back up every fact."""

import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from opportunity_app import outreach_research as research
from opportunity_app.outreach import create_target, get_target
from opportunity_app.web_fetch import SafeFetcher
from opportunity_app.outreach_config import COMPANY_RESEARCH_ENV, RESEARCH_ENV
from opportunity_app.outreach_settings import OutreachSettings
from opportunity_app.schema import ensure_product_schema
from opportunity_app.database import connect_product

from helpers_platform import build_and_migrate
from helpers_outreach import confirm_facts

USER = "local-user"

PRESS = (
    "<html><body><h1>Chargebot raises seed round</h1>"
    "<p>Chargebot, the Austin robotics company, today announced a $4.5M seed round led by Northgate Ventures.</p>"
    "<p>Its robot arm finds the charge port with a stereo camera and plugs in within 90 seconds.</p>"
    "<p>CTO Dana Ortiz leads the controls team.</p></body></html>"
)
CAREERS = (
    "<html><body><h2>Robotics Software Engineer</h2>"
    "<p>You will write motion planning code in C++ and Python on ROS 2, and test it on our fleet.</p>"
    "<footer>chargebot.example</footer></body></html>"
)
THESIS = (
    "<html><body><h1>Dana Ortiz: Visual servoing for connector insertion</h1>"
    "<p>This thesis shows that a stereo camera can guide a plug into a port with sub-millimeter error.</p></body></html>"
)
OTHER_COMPANY = "<html><body><p>Voltarm builds charging robots with lidar and ships them to fleets.</p></body></html>"
# The two specs sit side by side, so only the quote's own sentence keeps them apart.
ARMS = (
    "<html><body><h1>Chargebot arms</h1><p>The Chargebot A1 arm has a payload of 5 kg for small parts.</p>"
    "<p>The Chargebot B7 arm lifts a payload of 20 kg for heavy parts.</p></body></html>"
)
TEAM = (
    "<html><body><h1>Chargebot team</h1><p>Dana Ortiz, CTO. Dana leads the controls team.</p>"
    "<p>Sam Lee, Head of Perception. Sam previously led perception at Waymo for six years.</p></body></html>"
)
COMPARE = (
    "<html><body><p>Chargebot finds the charge port with a stereo camera and plugs in within 90 seconds.</p>"
    "<p>Voltarm, a rival, sells a wall-mounted charger.</p></body></html>"
)
# Both companies in one paragraph, in different sentences.
COMPARE_ONE_PARAGRAPH = (
    "<html><body><p>Chargebot finds the charge port with a stereo camera and plugs in within 90 seconds. "
    "Voltarm, a rival, sells a wall-mounted charger.</p></body></html>"
)
VOLTARM_APART = (
    "<html><body><p>Chargebot ships arms to fleets.</p>"
    "<p>Voltarm builds charging robots with lidar and ships them to fleets.</p></body></html>"
)
NORTHGATE_CAPITAL = "<html><body><p>Chargebot today announced a $4.5M seed round led by Northgate Capital.</p></body></html>"
VOLTARM_CEO = (
    "<html><body><p>Voltarm is led by CEO Sam Lee, who founded it in 2019 to build charging robots.</p></body></html>"
)
LEE_THESIS = (
    "<html><body><h1>Sam Lee: Force control for plug insertion</h1>"
    "<p>This thesis shows force control can insert a plug with sub-millimeter error in tests.</p></body></html>"
)
DATELINE = (
    "<html><body><p>AUSTIN, Texas, Sept. 3, 2026 - Chargebot today announced a $4.5M seed round led by Northgate Ventures "
    "to expand production of its charging arm.</p></body></html>"
)
EDGE = (
    "<html><body><p>Chargebot is the first robot that charges an EV without a driver present, in under 90 seconds.</p></body></html>"
)
NO_ROS = "<html><body><p>At Chargebot, engineers write the motion stack in Rust; we do not use ROS in our motion stack.</p></body></html>"
# A script and a stylesheet name the company; the visible text does not.
SCRIPT_ONLY = (
    "<html><head><script>var partner = 'Chargebot';</script><style>.chargebot{color:red}</style></head>"
    "<body><p>Voltarm builds charging robots with lidar and ships them to fleets.</p></body></html>"
)
SITES = {
    "chargebot.example": {
        "/careers": (200, CAREERS),
        "/": (200, "<html><body><h1>Chargebot</h1></body></html>"),
        "/specs": (403, "Forbidden"),
        "/arms": (200, ARMS),
        "/engineering": (200, NO_ROS),
        "/team": (200, TEAM),
        "/edge": (200, EDGE),
        "/dateline": (200, DATELINE),
        # A link on the company's site that leads somewhere else.
        "/go": (302, "https://news.example/voltarm"),
        "/li": (302, "https://www.crunchbase.com/organization/voltarm"),
        "/gone": (302, "https://news.example/blocked"),
    },
    "news.example": {
        "/chargebot-seed": (200, PRESS),
        "/voltarm": (200, OTHER_COMPANY),
        "/blocked": (403, "Forbidden"),
        "/report.pdf": (200, "%PDF-1.7 binary"),
        "/scripts": (200, SCRIPT_ONLY),
        "/compare": (200, COMPARE),
        "/compare-one": (200, COMPARE_ONE_PARAGRAPH),
        "/voltarm-apart": (200, VOLTARM_APART),
        "/northgate": (200, NORTHGATE_CAPITAL),
        "/voltarm-ceo": (200, VOLTARM_CEO),
        **{f"/walled{number}": (403, "Forbidden") for number in range(40)},
    },
    "university.example": {"/theses/ortiz": (200, THESIS), "/theses/lee": (200, LEE_THESIS)},
    "www.crunchbase.com": {"/organization/voltarm": (403, "Forbidden")},
}


def site_transport(sites):
    """Pages by host and path; a value is (status, body), and .pdf paths are served as PDFs. A 302's body is where it leads."""
    requested = []

    def handler(request):
        requested.append(str(request.url))
        page = sites.get(request.url.host.removeprefix("www."), {}).get(request.url.path)
        if page is None:
            return httpx.Response(404)
        status, body = page
        if status in {301, 302}:
            return httpx.Response(status, headers={"location": body})
        kind = "application/pdf" if request.url.path.endswith(".pdf") else "text/html"
        return httpx.Response(status, text=body, headers={"content-type": kind})

    return httpx.MockTransport(handler), requested


def fetcher_for(sites=SITES):
    transport, requested = site_transport(sites)
    client = httpx.Client(transport=transport)
    return SafeFetcher(client, resolve=lambda _host: ["93.184.216.34"]), requested


def fact(section, text, source_url, quote, person="", competitor=""):
    return {"section": section, "text": text, "source_url": source_url, "quote": quote, "person": person, "competitor": competitor}


SEED = fact(
    "traction", "Raised a $4.5M seed round led by Northgate Ventures",
    "https://news.example/chargebot-seed", "today announced a $4.5M seed round led by Northgate Ventures",
)
CAMERA = fact(
    "technology", "The arm finds the charge port with a stereo camera and plugs in within 90 seconds",
    "https://news.example/chargebot-seed", "Its robot arm finds the charge port with a stereo camera and plugs in within 90 seconds",
)
STACK = fact(
    "engineering", "Motion planning is written in C++ and Python on ROS 2",
    "https://chargebot.example/careers", "write motion planning code in C++ and Python on ROS 2",
)
CTO = fact("team", "CTO Dana Ortiz leads the controls team", "https://news.example/chargebot-seed", "CTO Dana Ortiz leads the controls team")
THESIS_FACT = fact(
    "team", "Dana Ortiz wrote a thesis on stereo camera guidance for plugging a connector into a port",
    "https://university.example/theses/ortiz", "a stereo camera can guide a plug into a port with sub-millimeter error",
    person="Dana Ortiz",
)
TARGET = {"company": "Chargebot, Inc.", "website": "https://chargebot.example"}


def reply(*facts, gaps=()):
    return "Here is the research:\n" + json.dumps({"facts": list(facts), "gaps": list(gaps)})


# Why a fact the second read said no to is left out.
SECOND = "a second read of the page says it does not state this: the passage says otherwise"


class SecondRead:
    """Stands in for the model that rereads each fact beside its page's passage.

    It says yes to every fact except those in ``refuse``, and keeps what it was
    shown, so a test can see that the words that decide a fact reached it.
    """

    def __init__(self, refuse=(), fail=False, not_rivals=()):
        self.refuse, self.fail, self.not_rivals = set(refuse), fail, set(not_rivals)
        self.items, self.calls = [], 0

    def __call__(self, instructions, content):
        self.calls += 1
        if self.fail:
            raise RuntimeError("the model is not reachable")
        items = json.loads(content)["items"]
        self.items += items
        return json.dumps({"verdicts": [
            {"id": item["id"], "supported": item["fact"] not in self.refuse,
             "why": "the passage says otherwise" if item["fact"] in self.refuse else "stated",
             # Asked only about a competitor fact, and by default the passage does say the two sell against each other.
             **({"rivals": item["fact"] not in self.not_rivals} if "competitor" in item else {})}
            for item in items
        ]})

    def shown(self, text):
        return next(item for item in self.items if item["fact"] == text)


class CheckBriefTests(unittest.TestCase):
    def check(self, *facts, gaps=(), target=TARGET, sites=SITES, second=None):
        self.second = second if second is not None else SecondRead()
        fetcher, self.requested = fetcher_for(sites)
        with fetcher:
            return research.check_brief(reply(*facts, gaps=gaps), target, fetcher=fetcher, judge=self.second)

    def refused(self, brief):
        return {item["text"]: item["reason"] for item in brief["refused"]}

    def test_a_fact_its_page_states_in_the_quoted_words_is_kept(self):
        brief = self.check(SEED, STACK, gaps=["which motors the arm uses"])
        self.assertEqual([item["text"] for item in brief["facts"]], [STACK["text"], SEED["text"]], "kept in section order")
        self.assertTrue(all(item["checked"] for item in brief["facts"]))
        self.assertEqual(brief["facts"][1]["quote"], SEED["quote"], "the quote is kept to show the student")
        self.assertEqual(brief["gaps"], ["which motors the arm uses"])
        self.assertEqual(brief["proposed"], 2)

    def test_without_a_second_read_a_fact_is_kept_not_checked(self):
        fetcher, _ = fetcher_for()
        with fetcher:
            brief = research.check_brief(reply(SEED), TARGET, fetcher=fetcher)
        self.assertEqual([(item["checked"], item["note"]) for item in brief["facts"]],
                         [(False, "its words are on the page, but no second read could check what it says")])

    def test_a_second_read_that_fails_leaves_facts_not_checked_after_one_more_try(self):
        brief = self.check(SEED, STACK, second=SecondRead(fail=True))
        self.assertEqual([item["checked"] for item in brief["facts"]], [False, False])
        self.assertEqual(self.second.calls, 2, "asked once more, then left")

    def test_the_second_read_sees_the_pages_own_passage_and_what_the_fact_is_about(self):
        self.check(SEED)
        shown = self.second.shown(SEED["text"])
        self.assertEqual(shown["about"], "Chargebot, Inc.")
        self.assertEqual(shown["page"], SEED["source_url"])
        self.assertIn("today announced a $4.5M seed round led by Northgate Ventures", shown["passage"])
        self.assertIn("Chargebot raises seed round", shown["top"], "the page's title comes too")

    def test_the_passage_always_holds_the_quotes_own_paragraph_and_a_nearby_dateline(self):
        from opportunity_app.web_fetch import FetchResult

        history = "<p>" + "Background paragraph about the company history. " * 60 + "</p>"
        page = research.ResearchPage(FetchResult("https://news.example/release", 200,
                                          "<p>AUSTIN, Texas, May 12, 2026 /PRNewswire/</p>" + history
                                          + "<p>Chargebot says its hardware can ship directly and is already deployed in the field.</p>"))
        shown = page.passage(page.find_quote("its hardware can ship directly and is already deployed"))["passage"]
        self.assertIn("already deployed in the field", shown, "a long paragraph before the quote never pushes it out")
        self.assertLessEqual(len(shown), research.PASSAGE_CHARS + research.QUOTE_LINE_CHARS)
        short = research.ResearchPage(FetchResult("https://news.example/short", 200,
                                           "<p>AUSTIN, Texas, May 12, 2026 /PRNewswire/</p><p>Chargebot today named Dana Ortiz CEO.</p>"))
        self.assertIn("May 12, 2026", short.passage(short.find_quote("Chargebot today named Dana Ortiz CEO"))["passage"])

    def test_second_reads_go_in_batches(self):
        texts = (
            "Chargebot announced a $4.5M seed round led by Northgate Ventures", "Chargebot today announced a $4.5M seed round",
            "A $4.5M seed round led by Northgate Ventures", "Northgate Ventures led a $4.5M seed round",
            "The Austin robotics company announced a $4.5M seed round",
        )
        many = [{**SEED, "section": section, "text": text} for section in ("traction", "news", "growth", "edge", "customers") for text in texts]
        brief = self.check(*many)
        self.assertEqual(len(brief["facts"]), 25)
        self.assertEqual(self.second.calls, 2, f"{research.JUDGE_BATCH} facts a call")

    def test_a_lightly_trimmed_quote_still_matches(self):
        trimmed = {**CAMERA, "quote": "Its robot arm finds the charge port with a stereo camera and ... plugs in within 90 seconds"}
        self.assertEqual(len(self.check(trimmed)["facts"]), 1)

    def test_quoted_words_that_are_not_on_the_page_are_refused(self):
        made_up = {**SEED, "quote": "the round will fund a new factory in Round Rock and double the team"}
        self.assertEqual(self.refused(self.check(made_up)), {SEED["text"]: "the quoted words are not on its source page"})

    def test_a_number_or_a_name_the_page_does_not_state_is_refused(self):
        wrong_amount = {**SEED, "text": "Raised a $6M seed round led by Northgate Ventures"}
        wrong_investor = {**SEED, "text": "Sequoia led the $4.5M seed round"}
        brief = self.check(wrong_amount, wrong_investor)
        self.assertEqual(brief["facts"], [])
        reasons = self.refused(brief)
        self.assertEqual(reasons[wrong_amount["text"]], "the quote and the lines around it do not state 6")
        self.assertEqual(self.second.items, [], "the second read only sees facts whose words passed")
        self.assertEqual(reasons[wrong_investor["text"]], "the quote and the lines around it do not name Sequoia",
                         "a name that opens the sentence still has to be there")

    def test_a_number_that_belongs_to_another_thing_is_left_to_the_second_read(self):
        wrong_arm = fact(
            "product", "The Chargebot A1 arm lifts a payload of 20 kg", "https://chargebot.example/arms",
            "The Chargebot A1 arm has a payload of 5 kg for small parts",
        )
        right_arm = {**wrong_arm, "text": "The Chargebot A1 arm has a payload of 5 kg"}
        brief = self.check(wrong_arm, right_arm, second=SecondRead(refuse=[wrong_arm["text"]]))
        self.assertEqual([item["text"] for item in brief["facts"]], [right_arm["text"]])
        self.assertEqual(self.refused(brief), {wrong_arm["text"]: SECOND})
        passage = self.second.shown(wrong_arm["text"])["passage"]
        self.assertIn("A1 arm has a payload of 5 kg", passage)
        self.assertIn("B7 arm lifts a payload of 20 kg", passage, "20 kg is on the page right below; the second read sees whose it is")

    def test_a_fact_that_drops_its_quotes_not_is_left_out_by_the_second_read(self):
        flipped = fact("engineering", "Engineers use ROS in their motion stack", "https://chargebot.example/engineering",
                       "we do not use ROS in our motion stack")
        honest = {**flipped, "text": "Engineers do not use ROS in their motion stack"}
        brief = self.check(flipped, honest, second=SecondRead(refuse=[flipped["text"]]))
        self.assertEqual([item["text"] for item in brief["facts"]], [honest["text"]])
        self.assertEqual(self.refused(brief), {flipped["text"]: SECOND})
        self.assertIn("we do not use ROS in our motion stack", self.second.shown(flipped["text"])["passage"])

    def test_a_quote_must_be_on_the_page_word_for_word(self):
        # Every three-word run of it is on the page, but not in this order.
        shuffled = {**CAMERA, "quote": "plugs in within 90 seconds. Its robot arm finds the charge port with a stereo camera and"}
        self.assertEqual(self.refused(self.check(shuffled)), {CAMERA["text"]: "the quoted words are not on its source page"})

    def test_a_plain_word_opening_a_sentence_is_grammar_not_a_name(self):
        listed = {**STACK, "text": "Listed tools: C++ and Python on ROS 2 for motion planning"}
        self.assertEqual(len(self.check(listed)["facts"]), 1, self.refused(self.check(listed)))

    def test_a_sentence_may_open_on_a_verb_but_not_on_a_name_the_page_does_not_have(self):
        verb = {**STACK, "text": "Writes motion planning code in C++ and Python on ROS 2"}
        self.assertEqual(len(self.check(verb)["facts"]), 1, "writes is write, which the page uses")
        elsewhere = fact("product", "Voltarm makes the A1 arm with a payload of 5 kg", "https://chargebot.example/arms",
                         "The Chargebot A1 arm has a payload of 5 kg for small parts")
        self.assertEqual(self.refused(self.check(elsewhere)), {elsewhere["text"]: "the quote and the lines around it do not name Voltarm"})
        self.assertEqual(self.second.items, [])

    def test_a_blocked_page_is_read_in_a_browser_and_checked_the_same_way(self):
        class Browser:
            unavailable = ""

            def render(self, url):
                return url, PRESS

        blocked = {**SEED, "source_url": "https://news.example/blocked"}
        fetcher, _ = fetcher_for()
        with fetcher:
            brief = research.check_brief(reply(blocked, {**blocked, "text": "Raised a $9M seed round led by Northgate Ventures"}),
                                         TARGET, fetcher=fetcher, renderer=Browser(), judge=SecondRead())
        self.assertEqual([(item["text"], item["checked"]) for item in brief["facts"]], [(SEED["text"], True)])
        self.assertEqual(brief["refused"][0]["reason"], "the quote and the lines around it do not state 9")

    def test_a_real_quote_cannot_carry_an_unrelated_claim(self):
        unrelated = {**CAMERA, "text": "Every joint uses harmonic drives with zero backlash"}
        self.assertEqual(self.refused(self.check(unrelated)), {unrelated["text"]: "the quote and the lines around it do not say most of what the fact says"})

    def test_a_fact_that_quotes_too_little_is_refused_unread(self):
        brief = self.check({**SEED, "quote": "seed round"})
        self.assertEqual(self.refused(brief), {SEED["text"]: "it quotes too little of its page to check"})
        self.assertEqual(self.requested, [])

    def test_a_page_that_does_not_load_or_is_not_a_page_is_refused(self):
        cases = {
            "https://news.example/missing": "its source did not load (HTTP 404)",
            "https://www.google.com/search?q=chargebot": "its source is a search results page",
            "https://www.google.com/url?q=https://news.example/chargebot-seed": "its source is a search results page",
            "https://www.linkedin.com/company/chargebot": "its source (linkedin.com) is behind a login or sells data",
            "https://news.example/report.pdf": "its source is a PDF, which this check cannot read; cite the page it is linked from",
            "http://127.0.0.1/admin": "its source is not a public web page",
        }
        facts = [{**SEED, "text": f"Raised a $4.5M seed round, per source {index}", "source_url": url} for index, url in enumerate(cases)]
        brief = self.check(*facts)
        self.assertEqual(brief["facts"], [])
        self.assertEqual({item["source_url"]: item["reason"] for item in brief["refused"]}, cases)
        self.assertFalse(any("google.com" in url or "linkedin.com" in url for url in self.requested), "never fetched")

    def test_the_companys_own_site_turning_readers_away_is_kept_as_not_checked(self):
        own = {**SEED, "source_url": "https://chargebot.example/specs"}
        brief = self.check(own)
        self.assertEqual(len(brief["facts"]), 1)
        self.assertFalse(brief["facts"][0]["checked"])
        self.assertIn("HTTP 403", brief["facts"][0]["note"])

    def test_another_site_turning_readers_away_is_refused(self):
        elsewhere = {**SEED, "source_url": "https://news.example/blocked"}
        self.assertEqual(self.refused(self.check(elsewhere)),
                         {SEED["text"]: "its site turned the check away (HTTP 403) and is not the company's own"})

    def test_a_page_about_another_company_is_refused(self):
        elsewhere = fact("technology", "Builds charging robots with lidar", "https://news.example/voltarm", "builds charging robots with lidar and ships them to fleets")
        self.assertEqual(self.refused(self.check(elsewhere)), {elsewhere["text"]: "its source does not name the company"})

    def test_with_no_website_on_file_a_page_must_still_name_the_company(self):
        elsewhere = fact("technology", "Builds charging robots with lidar", "https://news.example/voltarm", "builds charging robots with lidar and ships them to fleets")
        no_site = {"company": "Chargebot", "website": ""}
        self.assertEqual(self.refused(self.check(elsewhere, target=no_site)), {elsewhere["text"]: "its source does not name the company"})
        self.assertEqual(len(self.check(SEED, target=no_site)["facts"]), 1, "a page that does name it still counts")

    def test_a_name_only_in_scripts_or_styles_does_not_count(self):
        hidden = fact("technology", "Builds charging robots with lidar", "https://news.example/scripts", "builds charging robots with lidar and ships them to fleets")
        self.assertEqual(self.refused(self.check(hidden)), {hidden["text"]: "its source does not name the company"})

    def test_the_shared_company_check_never_matches_an_empty_domain(self):
        from opportunity_app.outreach_discovery import mentions_company

        self.assertFalse(mentions_company(OTHER_COMPANY, "Chargebot", ""))
        self.assertTrue(mentions_company(PRESS, "Chargebot, Inc.", ""))

    def test_a_competitor_fact_is_about_the_competitor_and_says_who_linked_them(self):
        rival = {**fact("competitors", "Voltarm builds charging robots with lidar", "https://news.example/voltarm",
                        "Voltarm builds charging robots with lidar and ships them to fleets"), "competitor": "Voltarm"}
        kept = self.check(rival)["facts"]
        self.assertEqual([(item["text"], item["note"]) for item in kept],
                         [(rival["text"], "picked as a competitor by the research agent")],
                         "the page names Voltarm, not Chargebot, so the pairing is the agent's")
        wrong_name = {**rival, "competitor": "Ampbot"}
        self.assertEqual(self.refused(self.check(wrong_name)), {rival["text"]: "the quote and the lines around it do not name Ampbot"})
        unnamed = {**rival, "competitor": ""}
        self.assertEqual(self.refused(self.check(unnamed)), {rival["text"]: "a competitor fact must name the competitor"})
        itself = {**rival, "competitor": "Chargebot"}
        self.assertEqual(self.refused(self.check(itself)), {rival["text"]: "it names the company itself as its competitor"})

    def test_another_persons_sentence_pinned_on_the_named_person_is_left_out_by_the_second_read(self):
        pinned = fact(
            "team", "Dana Ortiz previously led perception at Waymo", "https://chargebot.example/team",
            "Sam previously led perception at Waymo for six years", person="Dana Ortiz",
        )
        own = {**pinned, "text": "Sam Lee previously led perception at Waymo", "person": "Sam Lee"}
        brief = self.check(pinned, own, second=SecondRead(refuse=[pinned["text"]]))
        self.assertEqual([item["text"] for item in brief["facts"]], [own["text"]])
        self.assertEqual(self.refused(brief), {pinned["text"]: SECOND})
        shown = self.second.shown(pinned["text"])
        self.assertEqual(shown["about"], "Dana Ortiz")
        self.assertIn("Sam previously led perception at Waymo", shown["passage"])

    def test_the_competitor_field_is_no_way_round_the_name_check_on_other_sections(self):
        sneaky = {**SEED, "text": "Raised a $4.5M seed round led by Sequoia Capital", "competitor": "Sequoia Capital"}
        brief = self.check(sneaky)
        self.assertEqual(brief["facts"], [])
        self.assertIn("do not name Sequoia", self.refused(brief)[sneaky["text"]])

    def test_a_competitor_fact_on_the_companys_own_sentence_is_left_out_by_the_second_read(self):
        text = "Voltarm finds the charge port with a stereo camera and plugs in within 90 seconds"
        quote = "Chargebot finds the charge port with a stereo camera and plugs in within 90 seconds"
        taken = fact("competitors", text, "https://news.example/compare-one", quote, competitor="Voltarm")
        brief = self.check(taken, second=SecondRead(refuse=[text]))
        self.assertEqual(self.refused(brief), {text: SECOND})
        shown = self.second.shown(text)
        self.assertEqual(shown["about"], "Voltarm", "the second read is told whose fact it is")
        self.assertIn(quote, shown["passage"])

    def test_named_together_only_when_the_quoted_sentence_names_both(self):
        rival = fact("competitors", "Voltarm builds charging robots with lidar and ships them to fleets", "https://news.example/voltarm-apart",
                     "Voltarm builds charging robots with lidar and ships them to fleets", competitor="Voltarm")
        self.assertEqual([item["note"] for item in self.check(rival)["facts"]], ["picked as a competitor by the research agent"],
                         "Chargebot is only in another sentence of the page")

    def test_a_quote_that_starts_after_the_not_is_read_with_its_not(self):
        engineers = "Engineers use ROS in their motion stack"
        after_not = fact("engineering", engineers, "https://chargebot.example/engineering", "use ROS in our motion stack")
        brief = self.check(after_not, second=SecondRead(refuse=[engineers]))
        self.assertEqual(self.refused(brief), {engineers: SECOND})
        self.assertIn("we do not use ROS", self.second.shown(engineers)["passage"], "the passage is the page's, not the quote")
        # The words dropped from a trimmed quote may not hide a not.
        hidden = fact("engineering", engineers, "https://chargebot.example/engineering", "we ... not ... use ROS in our motion stack")
        self.assertEqual(self.refused(self.check(hidden)), {engineers: "the quoted words are not on its source page"})

    def test_a_not_about_something_else_is_left_out_by_the_second_read(self):
        swapped = fact("engineering", "Engineers use ROS in the motion stack, not Rust", "https://chargebot.example/engineering",
                       "we do not use ROS in our motion stack")
        rust = {**swapped, "text": "Engineers write the motion stack in Rust and do not use ROS"}
        brief = self.check(swapped, rust, second=SecondRead(refuse=[swapped["text"]]))
        self.assertEqual([item["text"] for item in brief["facts"]], [rust["text"]])
        self.assertEqual(self.refused(brief), {swapped["text"]: SECOND})

    def test_a_positive_paraphrase_of_without_is_not_refused(self):
        page = "https://chargebot.example/edge"
        quote = "the first robot that charges an EV without a driver present, in under 90 seconds"
        kept = [
            fact("edge", "Says it is the first robot to charge an EV with no driver present", page, quote),
            fact("edge", "Claims to be the first robot that charges an EV without a driver", page, quote),
            fact("edge", "The first robot to charge an EV autonomously, in under 90 seconds", page, quote),
        ]
        brief = self.check(*kept)
        self.assertEqual(len(brief["facts"]), 3, self.refused(brief))
        flipped = fact("edge", "The first robot that charges an EV with a driver present", page, quote)
        self.assertEqual(self.refused(self.check(flipped, second=SecondRead(refuse=[flipped["text"]]))), {flipped["text"]: SECOND})

    def test_a_swapped_technology_or_magnitude_is_refused(self):
        lidar = {**CAMERA, "text": "The arm finds the charge port with lidar and plugs in within 90 seconds"}
        billion = {**SEED, "text": "Raised a $4.5 billion seed round led by Northgate Ventures"}
        pounds = fact("product", "The Chargebot A1 arm has a payload of 5 lb", "https://chargebot.example/arms",
                      "The Chargebot A1 arm has a payload of 5 kg for small parts")
        reasons = self.refused(self.check(lidar, billion, pounds, second=SecondRead(refuse=[lidar["text"]])))
        self.assertEqual(reasons[lidar["text"]], SECOND, "a swapped word is a question of meaning")
        self.assertEqual([item["fact"] for item in self.second.items], [lidar["text"]], "a swapped unit never gets that far")
        self.assertIn("do not state 4.5 bn", reasons[billion["text"]])
        self.assertIn("do not state 5 lb", reasons[pounds["text"]])

    def test_the_same_number_in_another_spelling_of_its_unit_is_kept(self):
        million = {**SEED, "text": "Raised a $4.5 million seed round led by Northgate Ventures"}
        kilos = fact("product", "The Chargebot A1 arm has a payload of 5 kilograms", "https://chargebot.example/arms",
                     "The Chargebot A1 arm has a payload of 5 kg for small parts")
        brief = self.check(million, kilos)
        self.assertEqual(len(brief["facts"]), 2, self.refused(brief))

    def test_a_date_in_the_dateline_backs_the_month_and_year_but_not_another(self):
        page = "https://chargebot.example/dateline"
        quote = "today announced a $4.5M seed round led by Northgate Ventures to expand production of its charging arm"
        right = fact("news", "In September 2026, Chargebot raised a $4.5M seed round led by Northgate Ventures", page, quote)
        wrong_month = {**right, "text": "In March 2026, Chargebot raised a $4.5M seed round led by Northgate Ventures"}
        wrong_year = {**right, "text": "In September 2025, Chargebot raised a $4.5M seed round led by Northgate Ventures"}
        brief = self.check(right, wrong_month, wrong_year)
        self.assertEqual([item["text"] for item in brief["facts"]], [right["text"]])
        reasons = self.refused(brief)
        self.assertIn("do not state march", reasons[wrong_month["text"]])
        self.assertIn("do not state 2025", reasons[wrong_year["text"]])

    def test_a_title_opening_the_sentence_must_be_on_the_page(self):
        ceo = {**CTO, "text": "CEO Dana Ortiz leads the controls team"}
        first_round = fact("traction", "First Round Capital led the $4.5M seed round", "https://news.example/northgate",
                           "today announced a $4.5M seed round led by Northgate Capital")
        brief = self.check(ceo, first_round)
        self.assertEqual(brief["facts"], [])
        reasons = self.refused(brief)
        self.assertIn("do not name CEO", reasons[ceo["text"]])
        self.assertIn("do not name Round Capital", reasons[first_round["text"]], "First Round Capital is one name")
        northgate = {**first_round, "text": "Northgate Capital led the $4.5M seed round"}
        self.assertEqual(len(self.check(northgate)["facts"]), 1)

    def test_a_name_is_never_pieced_together_across_the_title_and_the_lines_near_the_quote(self):
        # The title ends "... Capital" words and the body says "seed round": read as one run, "round capital"
        # would appear to stand on the page. It must be looked for within each run in page order, whatever
        # the hash seed (this failed about one run in fifteen when the title words were a set).
        self.assertFalse(research._phrase_in(["round", "capital"], ["seed", "round"], ["capital", "weekly"]))
        self.assertTrue(research._phrase_in(["round", "capital"], ["first", "round", "capital", "led"], []))
        page = ("<html><body><h1>Capital Weekly</h1>"
                "<p>Chargebot today announced a $4.5M seed round led by Northgate Ventures.</p></body></html>")
        sites = {**SITES, "news.example": {**SITES["news.example"], "/capital-weekly": (200, page)}}
        first_round = fact("traction", "First Round Capital led the $4.5M seed round", "https://news.example/capital-weekly",
                           "today announced a $4.5M seed round led by Northgate Ventures")
        brief = self.check(first_round, sites=sites)
        self.assertEqual(brief["facts"], [])
        self.assertIn("do not name Round Capital", self.refused(brief)[first_round["text"]])

    def test_a_competitors_staff_do_not_tie_a_paper_to_the_company(self):
        rival = fact("competitors", "Voltarm is led by CEO Sam Lee, who founded it in 2019", "https://news.example/voltarm-ceo",
                     "Voltarm is led by CEO Sam Lee, who founded it in 2019", competitor="Voltarm")
        thesis = fact("team", "Sam Lee wrote a thesis showing force control can insert a plug with sub-millimeter error",
                      "https://university.example/theses/lee", "This thesis shows force control can insert a plug with sub-millimeter error",
                      person="Sam Lee")
        brief = self.check(rival, thesis)
        self.assertEqual([item["section"] for item in brief["facts"]], ["competitors"])
        self.assertIn("no confirmed fact about the team ties Sam Lee to it", self.refused(brief)[thesis["text"]])

    def test_a_redirect_is_checked_like_the_link_that_was_cited(self):
        elsewhere = fact("technology", "Builds charging robots with lidar", "https://chargebot.example/go",
                         "builds charging robots with lidar and ships them to fleets")
        broker = fact("technology", "Builds charging robots with lidar for fleets", "https://chargebot.example/li",
                      "builds charging robots with lidar and ships them to fleets")
        turned_away = {**SEED, "source_url": "https://chargebot.example/gone"}
        brief = self.check(elsewhere, broker, turned_away)
        self.assertEqual(brief["facts"], [], "the company's link is not the company's page once it leads elsewhere")
        by_url = {item["source_url"]: item["reason"] for item in brief["refused"]}
        self.assertEqual(by_url["https://chargebot.example/go"], "its source does not name the company")
        self.assertIn("behind a login or sells data", by_url["https://chargebot.example/li"])
        self.assertEqual(by_url["https://chargebot.example/gone"], "its site turned the check away (HTTP 403) and is not the company's own")
        self.assertFalse(any("crunchbase" in url for url in self.requested), "the data broker is never requested")

    def test_failed_renders_count_against_the_render_budget(self):
        class Browser:
            unavailable = ""
            calls = 0

            def render(self, url):
                self.calls += 1

        browser = Browser()
        facts = [{**SEED, "source_url": f"https://news.example/walled{number}", "text": f"Raised a $4.5M seed round, source {number}"}
                 for number in range(40)]
        fetcher, _ = fetcher_for()
        with fetcher:
            brief = research.check_brief(reply(*facts), TARGET, fetcher=fetcher, renderer=browser, judge=SecondRead())
        self.assertEqual(brief["facts"], [])
        self.assertEqual(browser.calls, research.MAX_RENDERS)

    def test_a_founders_paper_stays_when_another_fact_ties_them_to_the_company(self):
        brief = self.check(THESIS_FACT, CTO)
        self.assertEqual({item["text"] for item in brief["facts"]}, {THESIS_FACT["text"], CTO["text"]})
        alone = self.check(THESIS_FACT)
        self.assertEqual(alone["facts"], [])
        self.assertIn("no confirmed fact about the team ties Dana Ortiz to it", alone["refused"][0]["reason"])
        stranger = {**THESIS_FACT, "person": "Sam Lee"}
        self.assertEqual(self.check(stranger, CTO)["facts"], [self.check(CTO)["facts"][0]], "the person must be the one on the page")

    def test_sections_are_capped_and_unknown_ones_refused(self):
        many = [{**STACK, "text": STACK["text"]}] + [
            {**STACK, "text": text} for text in (
                "Writes motion planning code on ROS 2", "Uses C++ for motion planning", "Uses Python for motion planning",
                "Tests motion planning code on its fleet", "Motion planning code runs on ROS 2", "Python and C++ on ROS 2",
            )
        ]
        brief = self.check(*many, {**SEED, "section": "gossip"}, STACK)
        self.assertEqual(len(brief["facts"]), research.MAX_FACTS_PER_SECTION)
        reasons = [item["reason"] for item in brief["refused"]]
        self.assertIn(f"over the {research.MAX_FACTS_PER_SECTION} facts one section holds", reasons)
        self.assertIn("section gossip is not one the research writes", reasons)
        self.assertEqual(brief["proposed"], len(many) + 2, "the repeated fact is dropped, not counted twice")

    def test_one_run_checks_a_bounded_number_of_facts(self):
        many = [{**SEED, "section": "news", "text": f"Raised a $4.5M seed round led by Northgate Ventures ({index})"} for index in range(5)]
        with mock.patch.object(research, "MAX_PROPOSALS", 2), mock.patch.object(research, "MAX_PAGES", 1):
            brief = self.check(*many, {**SEED, "source_url": "https://chargebot.example/careers"})
        reasons = [item["reason"] for item in brief["refused"]]
        self.assertEqual(reasons.count("over the 2 facts one run checks"), 4)
        self.assertEqual(len(self.requested), 1, "one page fetched, however long the reply")

    def test_a_reply_without_a_facts_list_is_an_error(self):
        fetcher, _ = fetcher_for()
        with fetcher, self.assertRaises(ValueError):
            research.check_brief('{"companies": []}', TARGET, fetcher=fetcher)

    def test_a_page_built_by_scripts_is_read_in_a_browser(self):
        shell = {"chargebot.example": {"/careers": (200, "<html><body><div id='app'></div><footer>chargebot.example</footer></body></html>")}}

        class Renderer:
            unavailable = ""

            def render(self, url):
                return url, CAREERS

        fetcher, _ = fetcher_for(shell)
        with fetcher:
            brief = research.check_brief(reply(STACK), TARGET, fetcher=fetcher, renderer=Renderer(), judge=SecondRead())
        self.assertEqual(len(brief["facts"]), 1)

    def test_a_persons_own_page_backs_only_a_team_fact(self):
        """A thesis names its author, not the company: it may not carry a claim about the company's technology."""
        about_company = fact(
            "technology", "Dana Ortiz showed a stereo camera can guide a plug into a port", THESIS_FACT["source_url"],
            THESIS_FACT["quote"], person="Dana Ortiz",
        )
        brief = self.check(about_company, THESIS_FACT, CTO)
        self.assertEqual({item["section"] for item in brief["facts"]}, {"team"}, "the thesis stays a team fact")
        self.assertIn("belongs in team, not technology", self.refused(brief)[about_company["text"]])
        # A team fact from a person's page says whose page it is.
        anonymous = {**THESIS_FACT, "text": "A stereo camera can guide a plug into a port with sub-millimeter error"}
        brief = self.check(anonymous, CTO)
        self.assertIn("must name Dana Ortiz", self.refused(brief)[anonymous["text"]])

    def test_a_fact_outside_the_team_section_is_asked_about_the_company_whoever_it_names(self):
        named = fact("technology", CAMERA["text"], CAMERA["source_url"], CAMERA["quote"], person="Dana Ortiz")
        self.check(named)
        self.assertEqual(self.second.shown(CAMERA["text"])["about"], "Chargebot, Inc.")
        team = {**CTO, "person": "Dana Ortiz"}
        self.check(team)
        self.assertEqual(self.second.shown(team["text"])["about"], "Dana Ortiz")

    def test_naming_two_companies_is_not_proof_they_compete(self):
        partner = "<html><body><p>Chargebot partners with Voltarm to deploy chargers at fleets.</p></body></html>"
        rivals = "<html><body><p>Chargebot competes with Voltarm in fleet charging.</p></body></html>"
        customers = "<html><body><h1>Partners</h1><p>Chargebot works with Voltarm on charging.</p></body></html>"
        sites = {
            **SITES,
            "news.example": {**SITES["news.example"], "/partner": (200, partner), "/rivals": (200, rivals)},
            "chargebot.example": {**SITES["chargebot.example"], "/partners": (200, customers)},
        }

        def pair(url, quote):
            return fact("competitors", quote, url, quote, competitor="Voltarm")

        together = pair("https://news.example/partner", "Chargebot partners with Voltarm to deploy chargers at fleets")
        own = pair("https://chargebot.example/partners", "Chargebot works with Voltarm on charging")
        says = pair("https://news.example/rivals", "Chargebot competes with Voltarm in fleet charging")
        notes = {item["source_url"]: item["note"] for item in self.check(together, own, says, sites=sites)["facts"]}
        self.assertEqual(notes["https://news.example/partner"], "picked as a competitor by the research agent")
        self.assertEqual(notes["https://chargebot.example/partners"], "picked as a competitor by the research agent",
                         "the company's own site listing a name is not the company calling it a competitor")
        self.assertEqual(notes["https://news.example/rivals"], "the quoted sentence says the two compete")

    def test_a_sentence_with_both_names_and_a_word_of_competition_still_needs_the_second_read_to_call_them_rivals(self):
        customer = "<html><body><h1>Case study</h1><p>Voltarm, a fleet operator, chose Chargebot over competing chargers.</p></body></html>"
        partner = "<html><body><p>Chargebot partners with Voltarm to compete with Tesla in fleet charging.</p></body></html>"
        rivals = "<html><body><p>Chargebot competes with Voltarm in fleet charging.</p></body></html>"
        sites = {
            **SITES,
            "chargebot.example": {**SITES["chargebot.example"], "/customers": (200, customer)},
            "news.example": {**SITES["news.example"], "/partner2": (200, partner), "/rivals": (200, rivals)},
        }
        cust = fact("competitors", "Voltarm, a fleet operator, chose Chargebot over competing chargers",
                    "https://chargebot.example/customers", "Voltarm, a fleet operator, chose Chargebot over competing chargers", competitor="Voltarm")
        part = fact("competitors", "Chargebot partners with Voltarm to compete with Tesla in fleet charging",
                    "https://news.example/partner2", "Chargebot partners with Voltarm to compete with Tesla in fleet charging", competitor="Voltarm")
        real = fact("competitors", "Chargebot competes with Voltarm in fleet charging",
                    "https://news.example/rivals", "Chargebot competes with Voltarm in fleet charging", competitor="Voltarm")
        # Python finds a word of competition in all three sentences; only the second read can say who sells against whom.
        second = SecondRead(not_rivals=[cust["text"], part["text"]])
        brief = self.check(cust, part, real, sites=sites, second=second)
        notes = {item["source_url"]: item["note"] for item in brief["facts"]}
        self.assertEqual(notes["https://chargebot.example/customers"], research.PICK_NOTE)
        self.assertEqual(notes["https://news.example/partner2"], research.PICK_NOTE)
        self.assertEqual(notes["https://news.example/rivals"], research.COMPETE_NOTE)
        self.assertEqual(second.shown(real["text"])["competitor"], "Voltarm", "the judge is told which company the fact is about")
        # No second read: the fact is kept "not checked", and never as a rival the page says.
        brief = self.check(cust, real, sites=sites, second=SecondRead(fail=True))
        self.assertTrue(all(not item["checked"] and item["note"] != research.COMPETE_NOTE for item in brief["facts"]))
        # A judge that says the passage shows a customer or partner refuses the fact outright.
        brief = self.check(cust, sites=sites, second=SecondRead(refuse=[cust["text"]]))
        self.assertEqual(brief["facts"], [])
        self.assertIn("does not state this", self.refused(brief)[cust["text"]])

    def test_the_second_read_is_asked_whether_the_two_compete_and_only_of_a_competitor_fact(self):
        self.assertIn('"rivals"', research.JUDGE_INSTRUCTIONS)
        for relation in ("customer", "partner", "supplier", "investor", "acquirer"):
            self.assertIn(relation, research.JUDGE_INSTRUCTIONS)
        second = SecondRead()
        self.check(SEED, second=second)
        self.assertNotIn("competitor", second.shown(SEED["text"]), "only a competitor fact carries the question")

    def test_what_the_second_read_says_may_not_carry_an_address_or_link_onto_the_screen(self):
        def judge(instructions, content):
            items = json.loads(content)["items"]
            return json.dumps({"verdicts": [
                {"id": item["id"], "supported": False, "why": "see https://evil.example/x or mail dana@evil.example"} for item in items
            ]})

        brief = self.check(SEED, second=judge)
        self.assertEqual(self.refused(brief)[SEED["text"]], "a second read of the page says it does not state this: no reason given")
        digits = {}
        self.assertEqual(
            research.second_read([{"id": "f0"}], lambda i, c: json.dumps({"verdicts": [{"id": "f0", "supported": False, "why": "quote says 4.5M, fact says 45M"}]}), rivals=digits),
            {"f0": (False, "quote says 4.5M, fact says 45M")}, "a reason may carry numbers: it is not a gap",
        )

    def test_a_short_piece_of_a_trimmed_quote_must_be_on_the_page_too(self):
        page = "https://news.example/chargebot-seed"
        text = "Raised a seed round led by Northgate Ventures"
        real = fact("traction", text, page, "the Austin robotics company, today announced a $4.5M ... seed round led by Northgate Ventures")
        swapped = {**real, "quote": "the Austin robotics company, today announced a $60M ... seed round led by Northgate Ventures"}
        far = {**real, "quote": "the Austin robotics company, today announced a $4.5M ... seed round led by Northgate Ventures ... and $60M"}
        self.assertEqual(len(self.check(real)["facts"]), 1, "a short piece that is on the page is fine")
        for quote in (swapped["quote"], far["quote"]):
            with self.subTest(quote):
                brief = self.check({**real, "quote": quote})
                self.assertEqual(brief["facts"], [])
                self.assertEqual(self.refused(brief), {text: "the quoted words are not on its source page"})

    def test_a_shared_host_website_stands_for_its_own_path_only(self):
        for website, company, expected in (
            ("https://chargebot.example", "Chargebot", ("chargebot.example", "")),
            ("https://www.uni.example/bovi-lab/", "Bovi Lab", ("uni.example", "/bovi-lab")),
            ("https://sites.google.com/view/acme", "Acme", ("sites.google.com", "/view/acme")),
            ("https://github.com", "Acme", ("", "")),
            ("", "Acme", ("", "")),
        ):
            with self.subTest(website):
                self.assertEqual(research._site_scope(website, company), expected)
        scope = ("uni.example", "/bovi-lab")
        self.assertTrue(research._own_site("https://uni.example/bovi-lab/people", scope))
        self.assertTrue(research._own_site("https://uni.example/bovi-lab", scope))
        self.assertFalse(research._own_site("https://uni.example/other-lab", scope), "another page on the host is not the lab's")
        self.assertFalse(research._own_site("https://uni.example/bovi-lab-two", scope))
        self.assertTrue(research._own_site("https://news.chargebot.example/x", ("chargebot.example", "")))

    def test_another_page_on_a_shared_host_is_not_the_companys_own_site(self):
        sites = {"uni.example": {"/bovi-lab/pubs": (403, "Forbidden"), "/other-lab/pubs": (403, "Forbidden")}}
        target = {"company": "Bovi Lab", "website": "https://uni.example/bovi-lab"}
        mine = fact("technology", "Builds soft robot grippers from silicone", "https://uni.example/bovi-lab/pubs", "builds soft robot grippers from silicone")
        other = {**mine, "text": "Builds soft robot grippers from silicone rubber", "source_url": "https://uni.example/other-lab/pubs"}
        brief = self.check(mine, other, target=target, sites=sites)
        self.assertEqual([item["source_url"] for item in brief["facts"]], [mine["source_url"]])
        self.assertFalse(brief["facts"][0]["checked"])
        self.assertIn("is not the company's own", self.refused(brief)[other["text"]])

    def test_a_page_the_browser_ends_on_is_held_to_the_same_source_rules_as_a_link(self):
        class Renderer:
            unavailable = ""

            def __init__(self, ends_on):
                self.ends_on = ends_on

            def render(self, url):
                return self.ends_on, CAREERS

        walled = {"chargebot.example": {"/careers": (403, "Forbidden")}}

        def brief(ends_on):
            fetcher, _ = fetcher_for(walled)
            with fetcher:
                return research.check_brief(reply(STACK), TARGET, fetcher=fetcher, renderer=Renderer(ends_on), judge=SecondRead())

        self.assertEqual(len(brief("https://chargebot.example/careers")["facts"]), 1)
        refused = brief("https://www.linkedin.com/company/chargebot/jobs")
        self.assertEqual(refused["facts"], [])
        self.assertIn("sends the reader on to a page it cannot use", refused["refused"][0]["reason"])

    def test_own_site_is_where_the_browser_ended_not_where_the_plain_fetch_went(self):
        nameless = "<html><body><p>You will write motion planning code in C++ and Python on ROS 2, and test it on our fleet.</p></body></html>"

        class Renderer:
            unavailable = ""

            def render(self, url):
                return "https://jobs.example/opening", nameless

        walled = {"chargebot.example": {"/careers": (403, "Forbidden")}}
        fetcher, _ = fetcher_for(walled)
        with fetcher:
            brief = research.check_brief(reply(STACK), TARGET, fetcher=fetcher, renderer=Renderer(), judge=SecondRead())
        self.assertEqual(brief["facts"], [], "a nameless page on another host is not the company's just because the link was")
        self.assertEqual(brief["refused"][0]["reason"], "its source does not name the company")

    def test_a_slow_server_costs_the_run_its_time_budget_not_the_worker_thread(self):
        now = [0.0]

        def clock():
            return now[0]

        # Every page read "takes" ten seconds; a run gets thirty.
        transport, _ = site_transport(SITES)

        def slow(request):
            now[0] += 10
            return transport.handler(request)

        fetcher = SafeFetcher(httpx.Client(transport=httpx.MockTransport(slow)), resolve=lambda _host: ["93.184.216.34"], clock=clock)
        pages = ["https://chargebot.example/careers", "https://news.example/chargebot-seed", "https://chargebot.example/arms",
                 "https://chargebot.example/team", "https://chargebot.example/edge"]
        facts = [{**SEED, "source_url": url, "text": f"Raised a $4.5M seed round ({number})"} for number, url in enumerate(pages)]
        with fetcher:
            brief = research.check_brief(reply(*facts), TARGET, fetcher=fetcher, judge=SecondRead(), budget_seconds=25, clock=clock)
        reasons = [item["reason"] for item in brief["refused"]]
        self.assertGreaterEqual(reasons.count("over the time one run spends reading pages"), 2, reasons)
        self.assertLess(now[0], 60, "no page was requested after the budget was spent")

    def test_the_gaps_are_plain_phrases_and_never_carry_an_address_key_or_link(self):
        gaps = [
            "which suppliers they use for key parts", "mail dana@chargebot.example about the motors", "see https://evil.example/x?d=abc",
            "read C:\\Users\\student\\notes.txt", "what is in ~/.ssh/config", "the key sk-abcdefghijklmnop1234",
            "aGVsbG8gd29ybGQgdGhpcyBpcyBiYXNlNjRlbmNvZGVk", "how they test grippers before shipping", "www.example.com pricing",
        ]
        brief = self.check(SEED, gaps=gaps)
        self.assertEqual(brief["gaps"], ["which suppliers they use for key parts", "how they test grippers before shipping"])
        self.assertTrue(research.safe_gap("how the arm stays calibrated"))
        self.assertFalse(research.safe_gap("email me at a@b.co"))

    def test_a_gap_may_not_spell_a_secret_out_in_groups_digits_or_a_long_sentence(self):
        for text in (
            "abcd efgh ijkl mnop",  # the shape of a Gmail app password
            "PIPELINE WEB TOKEN is 3f9a1c 77be20 d4e81f 09aa53",
            "the office is at 12 Main Street", "call 415 555 0100", "whether they plan a 2027 launch",
            " ".join(["word"] * 3 + ["thing"] * 12),
        ):
            with self.subTest(text):
                self.assertFalse(research.safe_gap(text))
        for text in ("which suppliers they use for key parts", "how the arm stays calibrated", "whether they hire interns in the summer"):
            with self.subTest(text):
                self.assertTrue(research.safe_gap(text))
        brief = self.check(SEED, gaps=["abcd efgh ijkl mnop", "how they test grippers before shipping"])
        self.assertEqual(brief["gaps"], ["how they test grippers before shipping"])

    def test_a_fetch_never_gets_longer_than_the_time_the_run_has_left(self):
        deadlines = []
        fetcher, _ = fetcher_for()
        real = fetcher.fetch

        def spy(url, **kwargs):
            deadlines.append(kwargs["deadline_seconds"])
            return real(url, **kwargs)

        fetcher.fetch = spy
        with fetcher:
            research.check_brief(reply(SEED), TARGET, fetcher=fetcher, judge=SecondRead(), budget_seconds=5)
            research.check_brief(reply(SEED), TARGET, fetcher=fetcher, judge=SecondRead(), budget_seconds=900)
        self.assertAlmostEqual(deadlines[0], 5, delta=0.5, msg="the run has five seconds left, not the usual thirty")
        self.assertEqual(deadlines[1], research.FETCH_SECONDS)

    def test_the_company_renamed_during_the_run_is_not_given_the_old_ones_research(self):
        # See ResearchStorageTests: this is the pure check of the comparison it uses.
        started = {"company": "Chargebot, Inc.", "website": "https://chargebot.example"}
        self.assertFalse(research.company_changed(started, "CHARGEBOT", "https://www.chargebot.example/about"))
        self.assertFalse(research.company_changed({**started, "website": ""}, "Chargebot", "https://chargebot.example"))
        self.assertTrue(research.company_changed(started, "Voltarm", "https://chargebot.example"))
        self.assertTrue(research.company_changed(started, "Chargebot", "https://voltarm.example"))


class ResearchStorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        ensure_product_schema(self.conn)
        confirm_facts(self.conn, name="Test Student", degree="Mechanical Engineering", interest_keywords=["robotics", "controls"])
        self.target = create_target(self.conn, {
            "company": "Chargebot, Inc.", "website": "https://chargebot.example", "summary": "Robots that charge parked EVs",
            "contact_name": "Dana Ortiz", "contact_role": "CTO", "source_urls": ["https://news.example/chargebot-seed"],
        }, user_id=USER)
        self.prompts = []

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def research(self, raw):
        def runner(prompt):
            self.prompts.append(prompt)
            if isinstance(raw, Exception):
                raise raw
            return raw

        fetcher, _ = fetcher_for()
        with fetcher:
            return research.research_company(
                self.conn, self.target["id"], user_id=USER, runner=runner, fetcher=fetcher, agent="claude-code", judge=SecondRead(),
            )

    def test_the_brief_is_stored_with_when_and_who(self):
        target = self.research(reply(SEED, STACK, {**SEED, "text": "Raised $9M"}, gaps=["which motors they use"]))
        brief = target["tech_brief"]
        self.assertEqual(len(brief["facts"]), 2)
        self.assertEqual(len(brief["refused"]), 1)
        self.assertEqual(target["tech_brief_by"], "claude-code")
        self.assertTrue(target["tech_brief_at"])
        self.assertEqual(target["tech_brief_error"], "")
        self.assertTrue(research.brief_is_fresh(target))
        events = get_target(self.conn, self.target["id"], user_id=USER, include_events=True)["events"]
        self.assertIn(("tech_brief_written", "2 facts kept, 1 left out"), [(event["event_type"], event["detail"]) for event in events])

    def test_the_prompt_names_the_company_and_the_students_field_and_nothing_private(self):
        self.research(reply(SEED))
        prompt = self.prompts[0]
        for expected in ("Chargebot, Inc.", "https://chargebot.example", "Robots that charge parked EVs",
                         "https://news.example/chargebot-seed", "Dana Ortiz, CTO", "Mechanical Engineering", "robotics"):
            self.assertIn(expected, prompt)
        self.assertNotIn("Test Student", prompt, "the student's name does not go to the search")

    def test_a_failed_run_is_recorded_and_raised(self):
        with self.assertRaisesRegex(RuntimeError, "not signed in"):
            self.research(RuntimeError("Claude Code exited 1: not signed in"))
        target = get_target(self.conn, self.target["id"], user_id=USER)
        self.assertIn("not signed in", target["tech_brief_error"])
        self.assertEqual(target["tech_brief"], {})

    def test_a_run_that_keeps_nothing_never_replaces_a_brief_that_has_facts(self):
        self.research(reply(SEED))
        target = self.research(reply({**SEED, "text": "Raised $9M"}))
        self.assertEqual([item["text"] for item in target["tech_brief"]["facts"]], [SEED["text"]])
        self.assertIn("fewer than the 1 in the brief on file", target["tech_brief_error"])

    def test_a_run_that_keeps_only_unchecked_facts_never_replaces_a_brief_of_checked_ones(self):
        self.research(reply(SEED))
        walled = {**SEED, "source_url": "https://chargebot.example/specs"}
        target = self.research(reply(walled))
        self.assertEqual([(item["text"], item["checked"]) for item in target["tech_brief"]["facts"]], [(SEED["text"], True)])
        self.assertIn("checked 0 facts", target["tech_brief_error"])
        self.assertTrue(research.brief_is_fresh(target))

    def test_a_brief_of_unchecked_facts_alone_is_tried_again(self):
        target = self.research(reply({**SEED, "source_url": "https://chargebot.example/specs"}))
        self.assertEqual([item["checked"] for item in target["tech_brief"]["facts"]], [False])
        self.assertEqual(target["tech_brief_error"], "")
        self.assertFalse(research.brief_is_fresh(target))
        self.assertEqual(research.due_for_research(self.conn, user_id=USER, only_replied=False), [self.target["id"]])

    def test_stale_and_missing_briefs_are_due(self):
        self.assertEqual(research.due_for_research(self.conn, user_id=USER, only_replied=False), [self.target["id"]])
        self.assertEqual(research.due_for_research(self.conn, user_id=USER, only_replied=True), [], "not replied")
        self.research(reply(SEED))
        self.assertEqual(research.due_for_research(self.conn, user_id=USER, only_replied=False), [])
        later = datetime.now(timezone.utc) + research.FRESH_FOR + timedelta(days=1)
        self.assertEqual(research.due_for_research(self.conn, user_id=USER, only_replied=False, now=later), [self.target["id"]])

    def test_research_that_kept_nothing_is_tried_again(self):
        self.research(reply({**SEED, "text": "Raised $9M"}))
        target = get_target(self.conn, self.target["id"], user_id=USER)
        self.assertEqual(len(target["tech_brief"]["refused"]), 1, "what was left out is still shown")
        self.assertFalse(research.brief_is_fresh(target))
        self.assertEqual(research.due_for_research(self.conn, user_id=USER, only_replied=False), [self.target["id"]])

    def test_call_prep_researches_at_most_once_a_day(self):
        self.assertTrue(research.research_due(get_target(self.conn, self.target["id"], user_id=USER)))
        with self.assertRaises(RuntimeError):
            self.research(RuntimeError("connection reset"))
        target = get_target(self.conn, self.target["id"], user_id=USER)
        self.assertTrue(target["tech_brief_tried_at"])
        self.assertFalse(research.research_due(target), "a retry of the same job does not search again")
        tomorrow = datetime.now(timezone.utc) + research.RETRY_AFTER + timedelta(minutes=1)
        self.assertTrue(research.research_due(target, tomorrow))
        queued = research.queue_research(self.conn, self.target["id"], user_id=USER, reason="t")
        self.assertFalse(research.research_due(queued, tomorrow), "not while a research job is on its way")

    def test_a_job_is_queued_once(self):
        first = research.queue_research(self.conn, self.target["id"], user_id=USER, reason="t")
        again = research.queue_research(self.conn, self.target["id"], user_id=USER, reason="t")
        self.assertEqual(first["tech_brief_job"]["state"], "queued")
        self.assertEqual(first["tech_brief_job_id"], again["tech_brief_job_id"])

    def test_a_job_that_loses_the_race_is_cancelled_unrun(self):
        first = research.queue_research(self.conn, self.target["id"], user_id=USER, reason="t")
        # Another tab read the company before the first job was recorded.
        with mock.patch.object(research, "get_target", return_value={**first, "tech_brief_job": None}):
            research.queue_research(self.conn, self.target["id"], user_id=USER, reason="t")
        states = [row[0] for row in self.conn.execute("SELECT state FROM job_queue WHERE job_type=? ORDER BY created_at", (research.JOB_TYPE,))]
        self.assertEqual(sorted(states), ["cancelled", "queued"])
        self.assertEqual(get_target(self.conn, self.target["id"], user_id=USER)["tech_brief_job_id"], first["tech_brief_job_id"])

    def test_research_still_running_when_the_company_is_renamed_writes_nothing_onto_it(self):
        from opportunity_app.outreach import update_target

        def runner(prompt):
            update_target(self.conn, self.target["id"], {"company": "Different Motors", "website": "https://different.example"}, user_id=USER)
            return reply(SEED, STACK)

        fetcher, _ = fetcher_for()
        with fetcher:
            target = research.research_company(
                self.conn, self.target["id"], user_id=USER, runner=runner, fetcher=fetcher, agent="claude-code", judge=SecondRead(),
            )
        self.assertEqual(target["company"], "Different Motors")
        self.assertEqual(target["tech_brief"], {}, "Chargebot's checked facts are not the new company's")
        self.assertIn("changed while it was being researched", target["tech_brief_error"])
        events = get_target(self.conn, self.target["id"], user_id=USER, include_events=True)["events"]
        self.assertNotIn("tech_brief_written", [event["event_type"] for event in events])

    def test_research_is_not_queued_while_call_prep_is_researching_the_same_company(self):
        self.conn.execute("UPDATE outreach_targets SET tech_brief_tried_at=? WHERE id=?",
                          (datetime.now(timezone.utc).isoformat(), self.target["id"]))
        self.conn.commit()
        running = {**get_target(self.conn, self.target["id"], user_id=USER), "call_prep_job": {"state": "running"}}
        with mock.patch.object(research, "get_target", return_value=running):
            queued = research.queue_research(self.conn, self.target["id"], user_id=USER, reason="button")
        self.assertIsNone(queued.get("tech_brief_job"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM job_queue WHERE job_type=?", (research.JOB_TYPE,)).fetchone()[0], 0)
        # A day later, or with call prep not running, a click queues as before.
        idle = {**running, "call_prep_job": {"state": "succeeded"}}
        with mock.patch.object(research, "get_target", return_value=idle):
            research.queue_research(self.conn, self.target["id"], user_id=USER, reason="button")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM job_queue WHERE job_type=?", (research.JOB_TYPE,)).fetchone()[0], 1)

    def test_the_prompt_follows_the_students_field_with_no_field_assumed(self):
        confirm_facts(self.conn, degree="History", interest_keywords=["archives"])
        self.research(reply(SEED))
        self.assertIn("Studies History.", self.prompts[-1])
        self.assertNotIn("engineer", self.prompts[-1].split("## The student")[1].split("## Where to look")[0].casefold())


class AgentChoiceTests(unittest.TestCase):
    def test_its_own_setting_then_the_deep_searchs_then_claude_code(self):
        with mock.patch.dict("os.environ", {COMPANY_RESEARCH_ENV: "codex-cli", RESEARCH_ENV: "claude-code"}):
            self.assertEqual(research.research_agent(), "codex-cli")
        with mock.patch.dict("os.environ", {COMPANY_RESEARCH_ENV: "", RESEARCH_ENV: "codex-cli"}):
            self.assertEqual(research.research_agent(), "codex-cli")
        with mock.patch.dict("os.environ", {COMPANY_RESEARCH_ENV: "", RESEARCH_ENV: ""}):
            self.assertEqual(research.research_agent(), "claude-code")

    def test_a_missing_cli_falls_back_to_the_other_and_says_so(self):
        with mock.patch.dict("os.environ", {research.ALLOW_CODEX_ENV: "1"}), \
                mock.patch.object(research, "cli_available", side_effect=lambda binary: "codex" in binary):
            self.assertEqual(research.available_agent("claude-code"), ("codex-cli", "claude-code is not installed here, so codex-cli did the research"))
        with mock.patch.object(research, "cli_available", return_value=False):
            with self.assertRaises(research.ResearchUnavailable):
                research.available_agent("claude-code")

    def test_codex_is_swapped_for_claude_code_when_both_are_installed(self):
        """Codex's read-only sandbox can still read local files, and research reads untrusted pages."""
        with mock.patch.object(research, "cli_available", return_value=True):
            agent, note = research.available_agent("codex-cli")
            self.assertEqual(agent, "claude-code")
            self.assertIn("can read files on this computer", note)
            self.assertEqual(research.available_agent("claude-code"), ("claude-code", ""))
        with mock.patch.dict("os.environ", {research.ALLOW_CODEX_ENV: "1"}), \
                mock.patch.object(research, "cli_available", side_effect=lambda binary: "codex" in binary):
            self.assertEqual(research.available_agent("codex-cli"), ("codex-cli", ""), "with nothing else installed it is used only when the student allowed it")

    def test_codex_does_not_research_untrusted_pages_unless_the_student_allowed_it(self):
        """Codex's read-only sandbox can read local files; the pages it reads are not trusted."""
        only_codex = mock.patch.object(research, "cli_available", side_effect=lambda binary: "codex" in binary)
        for value in (None, "", "0", "no"):
            for preferred in ("codex-cli", "claude-code"):
                env = {} if value is None else {research.ALLOW_CODEX_ENV: value}
                with self.subTest(value=value, preferred=preferred), mock.patch.dict("os.environ", env), only_codex:
                    os.environ.pop(research.ALLOW_CODEX_ENV, None) if value is None else None
                    with self.assertRaises(research.ResearchUnavailable) as raised:
                        research.available_agent(preferred)
                    self.assertIn(research.ALLOW_CODEX_ENV, str(raised.exception))
                    self.assertIn("Install Claude Code", str(raised.exception))
        # The Research button is not offered either, and no job is queued to fail.
        with mock.patch.dict("os.environ", {research.ALLOW_CODEX_ENV: ""}), only_codex:
            researcher = research.web_researcher(lambda: None)
            self.assertIn("can read files", researcher.problem())
        with mock.patch.dict("os.environ", {research.ALLOW_CODEX_ENV: "yes"}), only_codex:
            self.assertEqual(research.web_researcher(lambda: None).problem(), "")
        # With Claude Code installed nothing changes, allowed or not.
        with mock.patch.dict("os.environ", {research.ALLOW_CODEX_ENV: ""}), mock.patch.object(research, "cli_available", side_effect=lambda binary: "claude" in binary):
            self.assertEqual(research.available_agent("codex-cli")[0], "claude-code")
            self.assertEqual(research.available_agent("claude-code"), ("claude-code", ""))

    def test_the_setting_is_offered_and_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text("", encoding="utf-8")
            _, platform_path = build_and_migrate(Path(folder))
            settings = OutreachSettings(env_path=env, attachment_dir=Path(folder) / "attach", resume_storage=Path(folder) / "resumes")
            with closing(connect_product(platform_path)) as conn, mock.patch.dict("os.environ", {COMPANY_RESEARCH_ENV: ""}):
                ensure_product_schema(conn)
                view = settings.view(conn, user_id=USER)
                self.assertEqual(view["company_research_agent"]["value"], "")
                self.assertEqual({option["id"] for option in view["company_research_agent"]["options"]}, {"claude-code", "codex-cli"})
                with self.assertRaises(ValueError):
                    settings.update(conn, {"company_research_agent": "legacy"}, user_id=USER)
                settings.update(conn, {"company_research_agent": "codex-cli"}, user_id=USER)
                self.assertIn(f"{COMPANY_RESEARCH_ENV}=codex-cli", env.read_text(encoding="utf-8"))
                with mock.patch.dict("os.environ", {"PIPELINE_LINKEDIN_ACCOUNT": ""}):
                    settings.update(conn, {"linkedin_account": "https://www.linkedin.com/in/Test-Student-1/"}, user_id=USER)
                    self.assertIn("PIPELINE_LINKEDIN_ACCOUNT=test-student-1", env.read_text(encoding="utf-8"), "only the username is kept")
                    self.assertEqual(settings.view(conn, user_id=USER)["linkedin_account"]["value"], "test-student-1")
                    with self.assertRaises(ValueError):
                        settings.update(conn, {"linkedin_account": "https://example.com/about"}, user_id=USER)


if __name__ == "__main__":
    unittest.main()
