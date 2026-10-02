"""Does the second read catch wrong facts and keep true ones? Fake pages, a real model.

The research check (opportunity_app/outreach/quote_check.py, run by
opportunity_app/outreach/research.py) keeps a fact only
when its words are on the page and a second model, reading the page's own
passage, confirms the fact says what the page says. Word checks alone kept
failing on meaning: another person's bio pinned on a founder, a "not" dropped,
a swapped technology, another product's number, a company's own sentence
credited to a competitor. This runs those cases, and true facts written as
short notes, through the real second read and prints how each was decided.

It calls the model once (the call prep writer, or Claude Code), so it is not
part of the test suites. Run it after changing JUDGE_INSTRUCTIONS or the
passage the second read sees:

    py -3 scripts/eval_second_read.py [--provider claude-code]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_outreach_research import TARGET, fetcher_for, reply
from opportunity_app.outreach import research
from opportunity_app.outreach import quote_check
from opportunity_app.integrations.agent_providers import build_provider

TEAM = ("<html><body><h1>Chargebot team</h1><h3>Dana Ortiz</h3><p>Co-founder and CTO</p>"
        "<p>Dana previously built motion planning software at Tesla for the Model Y line.</p>"
        "<h3>Sam Lee</h3><p>Head of Perception</p><p>Sam previously led perception at Waymo for six years.</p></body></html>")
ARMS = ("<html><body><h1>Chargebot arms</h1><p>The Chargebot A1 arm has a payload of 5 kg for small parts.</p>"
        "<p>The Chargebot B7 arm lifts a payload of 20 kg for heavy parts.</p></body></html>")
ENG = "<html><body><p>At Chargebot, engineers write the motion stack in Rust; we do not use ROS in our motion stack.</p></body></html>"
PASSIVE = "<html><body><p>At Chargebot, ROS is not used anywhere in our motion stack today.</p></body></html>"
TECH = ("<html><body><h1>How Chargebot works</h1><p>Its robot arm finds the charge port with a stereo camera and plugs in "
        "within 90 seconds.</p></body></html>")
UNLIKE = ("<html><body><p>Unlike Voltarm, Chargebot finds the charge port with a stereo camera and plugs in within 90 "
          "seconds.</p><p>Voltarm sells a wall-mounted charger for fleets.</p></body></html>")
DRIVER = "<html><body><p>Chargebot is the first robot that charges an EV without a driver present, in under 90 seconds.</p></body></html>"
SITES = {"chargebot.example": {"/team": (200, TEAM), "/arms": (200, ARMS), "/eng": (200, ENG), "/passive": (200, PASSIVE),
                               "/tech": (200, TECH), "/unlike": (200, UNLIKE), "/driver": (200, DRIVER)}}


def fact(section, text, path, quote, person="", competitor=""):
    return {"section": section, "text": text, "source_url": f"https://chargebot.example{path}", "quote": quote,
            "person": person, "competitor": competitor}


# (fact, should be kept)
CASES = [
    (fact("team", "Dana Ortiz previously led perception at Waymo", "/team", "Sam previously led perception at Waymo for six years", "Dana Ortiz"), False),
    (fact("team", "Dana Ortiz previously built motion planning at Tesla for the Model Y", "/team", "Dana previously built motion planning software at Tesla", "Dana Ortiz"), True),
    (fact("product", "The Chargebot A1 arm lifts a payload of 20 kg", "/arms", "The Chargebot A1 arm has a payload of 5 kg for small parts"), False),
    (fact("product", "Chargebot A1 arm: 5 kg payload, for small parts", "/arms", "The Chargebot A1 arm has a payload of 5 kg for small parts"), True),
    (fact("engineering", "Engineers use ROS in their motion stack", "/eng", "use ROS in our motion stack"), False),
    (fact("engineering", "Engineers use ROS in the motion stack, not Rust", "/eng", "we do not use ROS in our motion stack"), False),
    (fact("engineering", "Motion stack written in Rust; no ROS", "/eng", "engineers write the motion stack in Rust; we do not use ROS"), True),
    (fact("engineering", "The motion stack uses ROS", "/passive", "ROS is not used anywhere in our motion stack today"), False),
    (fact("technology", "The arm finds the charge port with lidar and plugs in within 90 seconds", "/tech", "Its robot arm finds the charge port with a stereo camera and plugs in within 90 seconds"), False),
    (fact("technology", "Arm finds the charge port w/ stereo camera; plugs in within 90 s", "/tech", "Its robot arm finds the charge port with a stereo camera and plugs in within 90 seconds"), True),
    (fact("competitors", "Voltarm finds the charge port with a stereo camera and plugs in within 90 seconds", "/unlike", "Chargebot finds the charge port with a stereo camera and plugs in within 90 seconds", competitor="Voltarm"), False),
    (fact("competitors", "Voltarm sells a wall-mounted charger for fleets", "/unlike", "Voltarm sells a wall-mounted charger for fleets", competitor="Voltarm"), True),
    (fact("edge", "First robot to charge an EV with no driver present, under 90 seconds", "/driver", "the first robot that charges an EV without a driver present, in under 90 seconds"), True),
    (fact("edge", "The first robot that charges an EV with a driver present", "/driver", "the first robot that charges an EV without a driver present, in under 90 seconds"), False),
]

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--provider", default=None, help="Model for the second read (default: the call prep writer)")
args = parser.parse_args()
judge = research.text_model(build_provider, args.provider, "claude-code")
fetcher, _ = fetcher_for(SITES)
with fetcher:
    brief = quote_check.check_brief(reply(*[case for case, _ in CASES]), TARGET, fetcher=fetcher, judge=judge)
kept = {item["text"] for item in brief["facts"] if item["checked"]}
reasons = {item["text"]: item["reason"] for item in brief["refused"]}
right = 0
for case, should_keep in CASES:
    was_kept = case["text"] in kept
    ok = was_kept == should_keep
    right += ok
    print(f"{'OK  ' if ok else 'MISS'} {'kept   ' if was_kept else 'refused'} (want {'keep' if should_keep else 'refuse'}) | {case['text'][:70]}"
          + ("" if was_kept else f"\n       why: {reasons.get(case['text'], '(not listed)')[:140]}"))
print(f"\n{right}/{len(CASES)} decided as wanted")
sys.exit(0 if right == len(CASES) else 1)
