"""What the Apply for me policy says about a question: which never take an answer from the app, and why.

Pure text rules, no database and no network. Three layers read the same question, each stricter than the last:

    context_dependent   a question that takes its meaning from the one above it, or from the employer (needs a label,
                        and is never answered by a saved answer that was written for another company)
    classify_sensitive  the precise classifier: the kind of sensitive question this is, or None
    net_topics          the broad net: what might be sensitive, kept apart from the precise classifier above

and the statement helpers the plan uses for an acknowledgment or a consent box (statement_of, classify_item).

It sits below both apply_policy (the plan, which reads it) and the store of answers the student chose to keep (which
asks it what a question is before it stores or looks up an answer), so neither has to import the other. The patterns
here mirror apps/extension/apply-engine.js; tests/fixtures/apply/context_keys.json is run by both suites.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Any, Iterable

from pipeline_core.identity import normalized_text

from ..applications.extension import SENSITIVE_FIELD

if TYPE_CHECKING:
    from .policy import SchemaField

# The categories a statement and a tick box come in. (The store of answers has its own list of what it may keep.)
STATEMENT_CATEGORIES = ("acknowledgment", "consent")
# A single box that states the answer ("I confirm that I am at least 18 years of age") is stored as ticked too.
TICKABLE = ("work_authorization", "sponsorship", "age_18")


_EEOC_NAMES = {
    "gender": "eeo_gender", "hispanic_ethnicity": "eeo_hispanic", "race": "eeo_race",
    "veteran_status": "eeo_veteran", "disability_status": "eeo_disability",
}


# Text that takes its meaning from the question above it, not from the company. These mirror the shared
# engine's rules (apps/extension/apply-engine.js: needsLabelKey, withoutEnumeration, CONTEXT_WORDING);
# tests/fixtures/apply/context_keys.json is run by both suites, so they cannot drift apart. The Python side
# must never be looser than the engine.
_CONTEXT_OPENER = re.compile(r"^(?:if yes|if so|if no|if other|please specify|please explain|please describe|other|explain)\b")
_CONTEXT_IF = re.compile(r"^if\b")
_CONTEXT_IF_ANY = re.compile(r"\bif (?:yes|so|no|not|other|applicable|any)\b")
_CONTEXT_PLEASE = re.compile(r"\bplease (?:explain|specify|describe|elaborate)\b")
_CONTEXT_DETAILS = re.compile(r"\b(?:provide|give|share|include|add|list)\s+(?:[a-z']+\s+){0,3}?(?:details?|information|info|context|explanations?)\b")
_CONTEXT_PRONOUN = re.compile(r"\b(?:list|name|give|provide|share|describe|explain|specify|identify) (?:them|it|those|these|each)\b")
_CONTEXT_VERB = re.compile(r"\b(?:explain|explanation|specify|elaborate|clarify|expand)\b")
_CONTEXT_DESCRIBE = re.compile(r"\bdescribe\b")
_CONTEXT_PHRASE = re.compile(
    r"\btell us (?:more|why)\b|\bwhy or why not\b|\bif applicable\b|\byour (?:answer|response)s? (?:above|to the previous)\b|\bprevious question\b|\bthe above\b"
)
_CONTEXT_WH = re.compile(r"^(?:which|what|when|where|who|whom|whose|how|why)\b")
# Questions whose truth depends on the employer. Found from this wording list and nothing smarter.
_CONTEXT_WORDING = re.compile(
    r"previously (?:worked|been employed|applied)|worked (?:here|for us|for this company|at)|applied (?:here|before|previously)|referr|who referred"
    r"|know (?:anyone|someone)|how did you hear|where did you (?:hear|find)|current(?:ly)? (?:an )?employee"
    r"|worked (?:for|with|at) (?:us|this|our|the company)|employed (?:by|at|with)|interviewed (?:with|at|here)|relatives?\b|family members?\b"
    r"|related to\b|spouse|immediate family|former employee|employed here\b|relations? working"
    r"|this (?:organi[sz]ation|firm|company|employer)"
    r"|\bwork (?:here|for us|with us)\b|\bour (?:company|team|organi[sz]ation|mission|products?)\b"
    r"|\bthis (?:role|position|opportunity|team)\b|\binterest(?:ed|s)? (?:you )?(?:in|about) this\b|\bjoin (?:us|our)\b"
)


def without_enumeration(key: str) -> str:
    """The key with leading tags taken off, in any order: a bullet or number ("b.", "1a)", "(ii)"), "Question 3:",
    "Question no. 3:", "Follow-up:", "Follow-on question:", "Sub-question:" and "(Optional)"."""
    text = re.sub(r"'+(?=\s|$)", "", re.sub(r"(^|\s)'+", r"\1", key)).strip()
    for _ in range(6):
        following = re.sub(r"^(?:follow ?(?:up|on)s?|sub ?questions?)(?: questions?)?\s+(?=\S)", "", text)
        following = re.sub(r"^optional\s+(?=\S)", "", following)
        following = re.sub(
            r"^(?:question|part|step|section|item|no|number)(?:\s+(?:no|number))?\s+(?:\d{1,3}[a-z]?|[a-z]|[ivx]{1,4})\s+(?=\S)", "", following,
        )
        following = re.sub(r"^(?:[a-z]|[ivx]{1,4}|\d{1,3}[a-z]?|[a-z]\d{1,3}[a-z]?)\s+(?=\S)", "", following)
        if following == text:
            break
        text = following
    return text


def needs_label_key(key: str) -> bool:
    """Whether a question key is a follow-up, an opener or too short to stand on its own words."""
    text = without_enumeration(key)
    words = len([word for word in text.split(" ") if word])
    return (
        len([word for word in key.split(" ") if word]) < 3 or words < 3
        or any(_CONTEXT_OPENER.search(item) or _CONTEXT_PLEASE.search(item) for item in (key, text))
        or bool(_CONTEXT_IF.search(text)) or bool(_CONTEXT_IF_ANY.search(text)) or bool(_CONTEXT_DETAILS.search(text))
        or bool(_CONTEXT_PRONOUN.search(text)) or bool(_CONTEXT_PHRASE.search(text))
        or (words < 8 and bool(_CONTEXT_VERB.search(text))) or (words < 6 and bool(_CONTEXT_DESCRIBE.search(text)))
        or (words < 6 and bool(_CONTEXT_WH.search(text)))
    )


# A short question that asks about the student ("When do you graduate?", "What is your GPA?") stands on its own; one that
# asks nothing about them ("When?", "Which one?", "What type?", "Where?") can only be a follow-up.
SECOND_PERSON = re.compile(r"\b(?:you|your|yours|yourself|my|we|our|us)\b")
_BARE_DETAIL = re.compile(r"^(?:the )?(?:details?|explanations?|dates?|circumstances|specifics|outcome)$")


def follow_up_wording(key: str) -> bool:
    """Whether the question's own words say it continues another question ("If yes, please explain", "When?", "Details").

    Narrower than ``needs_label_key``, which also takes any short or repeated question: "GPA", "LinkedIn Profile" and
    "When do you graduate?" stand on their own. A short question that asks about no one ("Which one?", "What type?",
    "When does it expire?"), a short "describe" and a bare "Details" continue the question above them: leaving them
    ordinary would put a felony's circumstances or a visa's type into the answer library.
    """
    text = without_enumeration(key)
    words = len([word for word in text.split(" ") if word])
    return (
        any(_CONTEXT_OPENER.search(item) or _CONTEXT_PLEASE.search(item) for item in (key, text))
        or bool(_CONTEXT_IF.search(text)) or bool(_CONTEXT_IF_ANY.search(text)) or bool(_CONTEXT_DETAILS.search(text))
        or bool(_CONTEXT_PRONOUN.search(text)) or bool(_CONTEXT_PHRASE.search(text))
        or (words < 8 and bool(_CONTEXT_VERB.search(text)))
        or bool(_BARE_DETAIL.search(text))
        or (words < 6 and bool(_CONTEXT_DESCRIBE.search(text)))
        or (words < 6 and bool(_CONTEXT_WH.search(text)) and not SECOND_PERSON.search(text))
    )


def follow_up_shaped(key: str) -> bool:
    """Whether a question could be a continuation of the one above it by its shape alone: a follow-up wording, a short question,
    one that opens with a question word, or none at all.

    Wider than ``follow_up_wording``, and the rule the extension's ``followUpShaped`` reads. It decides whether a chain of
    never-storable questions runs on through this one (see ``build_plan``): "Name of the employer" under a probation question
    is not worded as a follow-up, but a "describe" under it still belongs to the probation question.
    """
    if not key:
        return True
    return needs_label_key(key) or len(key.split()) < 6 or bool(_CONTEXT_WH.search(without_enumeration(key)))


def context_dependent(key: str) -> bool:
    """A key whose saved answer is never reused for another company, even when the row is tagged reusable."""
    return needs_label_key(key) or bool(_CONTEXT_WORDING.search(key))


# On the normalized question: lower case, every run of anything but a-z and 0-9 one space.
_OPT_NOT_MARKETING = r"\bopt\b(?! (?:in|out)\b(?! (?:the )?(?:us|u s|usa|united states|20\d\d)(?!\w)))"
_AGE_TAIL = r"(?: years?)?(?: (?:of age|old|or older|or over|and older|and over))*"
_AGE_18 = re.compile(
    rf"\b(?:(?:at least|over|above|older than) (?:the age of )?18{_AGE_TAIL}|(?:the )?age of 18{_AGE_TAIL}"
    rf"|18(?: years?)?(?: (?:of age|old|or older|or over|and older|and over))+"
    # "Are you 18+?" loses its plus sign in the normalized text, so it reads "are you 18".
    rf"|(?:are you|you are|must be) 18(?!\d){_AGE_TAIL})"
)
_PATTERNS: dict[str, re.Pattern[str]] = {
    "work_authorization": re.compile(
        r"authori[sz]ed to work|authori[sz]ation to work|work authori[sz]ation|legally (?:eligible|authori[sz]ed)|right to work|eligible to work"
        r"|legally (?:(?:able|permitted|allowed) to )?work|eligib\w* (?:for|to) (?:employment|work)|work permit"
    ),
    "sponsorship": re.compile(
        r"sponsor|immigration|petition|employment based|visa (?:sponsor|status|support|type|holder|transfer)"
        rf"|(?:require|need|hold)\w* (?:a )?visa|work visa|student visa|\b(?:f ?1|j ?1|h ?1 ?b|tn|e ?3)\b|\bstem opt\b|{_OPT_NOT_MARKETING}|\bcpt\b|practical training"
        # "What type of visa do you hold?", "Which visa are you on?", "Do you have a visa?". The company Visa is not caught.
        r"|type of visa|\b(?:hold|have|has|current\w*|which) (?:(?:a|an|your|any|the) )?(?:\w+ )?visa\b"
        # "What visa do you hold?", "Are you currently on a visa?", and a bare "Visa" heading.
        r"|\bwhat (?:(?:is|are|s) )?(?:(?:your|the|my) )?(?:\w+ )?visa\b|\bon (?:a|an) (?:\w+ )?visa\b|^visas?$"
        # Any other visa is immigration status ("What kind of visa do you have?", "Your visa", "Visa (if applicable)") unless it is
        # plainly the company: at, for or with Visa, Visa's, Visa Inc. or its card.
        r"|(?<!\bat )(?<!\bfor )(?<!\bwith )(?<!\babout )(?<!\bwhy )(?<!\bjoin )(?<!\bjoining )(?<!\blike )(?<!\bfrom )(?<!\bby )\bvisas?\b(?! s\b)(?! (?:inc|card|cards|payment|payments|network|corp|corporation|company|co|usa|international|gift)\b)"
    ),
    "age_18": _AGE_18,
    "export_control": re.compile(
        r"u s person|us person|\bitar\b|export administration regulations|export control|citizen|permanent resident|green card|clearance"
        r"|nationalit|\b(?:u s|us|united states|american) national\b|\bnational of\b"
    ),
    "eeo_gender": re.compile(r"\bgender\b|\bsex\b"),
    "eeo_hispanic": re.compile(r"hispanic|latin[oax]"),
    "eeo_race": re.compile(r"\brace\b|ethnic"),
    "eeo_veteran": re.compile(r"veteran|military|armed forces"),
    "eeo_disability": re.compile(r"disab"),
    "acknowledgment": re.compile(r"i (?:certify|attest|acknowledge|confirm|understand|agree)|accura|truthful|have read|privacy (?:notice|policy|statement)|acknowledg"),
    "consent": re.compile(r"consent|retain|retention|process(?:ing)? (?:of )?(?:my|your) (?:personal )?(?:data|information)|gdpr"),
    "salary": re.compile(r"salary|compensation|pay (?:expectation|range)|desired pay|expected pay|hourly rate|wages?\b|base pay|pay rate"),
}
_NEVER_STORABLE = re.compile(
    r"\bage\b|birth|pronoun|marital|religio|genetic|pregnan|criminal|convict|felony|misdemeanor|arrest|background check|sexual|transgender|non ?compete"
    r"|crimes?\b|offen[cs]es?\b|lgbt|queer"
)
# A question filed under a parent in one of these kinds is that kind too, whatever its own wording says (see build_plan).
INHERITING_PARENTS = frozenset({"uncategorized", "export_control", "sponsorship", "salary"})
# Most restrictive first (7.3 step 3).
RESTRICTION = (
    "uncategorized", "export_control", "salary", "sponsorship", "work_authorization", "age_18",
    "eeo_gender", "eeo_hispanic", "eeo_race", "eeo_veteran", "eeo_disability", "acknowledgment", "consent",
)
_OPTION_FLAGS = (
    ("export_control", re.compile(r"citizen|clearance|green card|permanent resident")),
    ("sponsorship", re.compile(rf"visa|h ?1 ?b|{_OPT_NOT_MARKETING}|sponsor|\b(?:f ?1|j ?1)\b|\bcpt\b")),
)
_DECLINE = re.compile(
    r"decline to (?:self identify|answer|state|identify|disclose)|do(?: not|n t) wish to (?:answer|disclose|identify|say)"
    r"|do not want to answer|prefer not to (?:say|answer|disclose|identify)"
)
CATEGORY_WORDS = {
    "work_authorization": "work authorization", "sponsorship": "visa sponsorship or immigration status", "age_18": "18 or older",
    "export_control": "export control, citizenship or security clearance", "salary": "salary",
    "eeo_gender": "voluntary self-identification", "eeo_hispanic": "voluntary self-identification",
    "eeo_race": "voluntary self-identification", "eeo_veteran": "voluntary self-identification",
    "eeo_disability": "voluntary self-identification", "acknowledgment": "a legal acknowledgment",
    "consent": "a data-processing consent", "uncategorized": "a personal question",
}


def most_restrictive(categories: Iterable[str | None]) -> str | None:
    found = {category for category in categories if category}
    return next((category for category in RESTRICTION if category in found), None)


def classify_sensitive(question: str, options: Iterable[str] = (), section: str = "", field_name: str = "") -> str | None:
    """A sensitive category, ``"uncategorized"`` (sensitive, never storable), or None (an ordinary question).

    The rules run in the order of spec 7.3. The result can only be stricter than the extension's own
    ``SENSITIVE`` rule: anything that rule flags and nothing here places is ``"uncategorized"``.
    """
    text = re.sub(r"\beighteen\b", "18", normalized_text(question))
    # 1. Never storable, for every section. An 18-or-older phrase goes first, or "years of age" would trip \bage\b.
    if _NEVER_STORABLE.search(_AGE_18.sub(" ", text)):
        return "uncategorized"
    # 2. Section rules. EEO fields are mapped by their schema field names only.
    if section in ("compliance", "demographic", "demographic_questions"):
        return _EEOC_NAMES.get(field_name, "uncategorized")
    if section == "data_compliance":
        return "consent"
    # 3. The question's own words.
    found = {category for category, pattern in _PATTERNS.items() if pattern.search(text)}
    # 4. Options fail closed: a vague question with visa, citizenship or clearance choices is sensitive too.
    choices = [normalized_text(option) for option in options]
    for category, pattern in _OPTION_FLAGS:
        if any(pattern.search(choice) for choice in choices):
            found.add(category)
    if any(_DECLINE.search(choice) for choice in choices):
        return "uncategorized"
    result = most_restrictive(found)
    # 5. Anything the extension flags and no row places can never be answered, but is never ordinary either.
    if result is None and SENSITIVE_FIELD.search(str(question or "")):
        return "uncategorized"
    return result


# --- The broad net: what might be sensitive, kept apart from the precise classifier above ----------------------
#
# ``classify_sensitive`` decides a category from listed wordings, and every review round found wordings the lists miss. So
# this is a second, deliberately wide reading of the same question: one list per topic, each item a topic word or a short
# phrase (never a sentence shape). It never marks a question sensitive by itself, and it never says which category. It only
# tightens what the app may do with a question the precise classifier called ordinary (spec 7.3, "As built"). Safety does not
# rest on it: Apply for me never carries a saved answer from one company to another, and never fills a box or an agreement from
# the answer library (spec 7.1 "As built"), so a wording the lists miss can at worst be saved by the student for that one company.
# What the net adds is a best-effort refusal:
#
# - a question that hits a NEVER-STORABLE topic (criminal, demographic, money, security), or that is filed under or follows one,
#   is left for the student: no form offers to save it, and nothing fills it from the answer library at all;
# - a checkbox, a select whose options agree to something, a Yes/No-like question that hits the agreement topic and a typed
#   signature are never filled from the answer library (only an exact stored statement may tick or choose it, D9 B).
#
# Over-blocking costs only some convenience, so a list leans wide. apps/extension/apply-engine.js repeats these lists as
# ``NET_TOPICS`` and tests/fixtures/apply/broad_net.json is run by both suites, so the two cannot drift apart. The text is
# normalized first: lower case, every run of anything but a-z and 0-9 one space ("visa's" reads "visa s", "H-1B" "h 1 b").
NET_TOPICS: dict[str, tuple[str, ...]] = {
    "immigration": (
        r"\bvisa", r"\bsponsor", r"\bimmigra", r"\bcitizen", r"\bnationalit", r"\bpassport", r"\bgreen card", r"\bpermanent resident",
        r"\bh ?1 ?b\b", r"\bopt\b", r"\bcpt\b", r"\bf ?1\b", r"\bj ?1\b", r"\btn (?:visa|status)", r"\be ?3\b", r"\bi ?9\b",
        r"\be ?verify", r"\balien",
        r"\bead\b", r"\bdaca\b", r"\btps\b", r"\basyl", r"\brefugee", r"\bforeign national", r"\blawful", r"\bh ?4\b", r"\bleave to remain", r"\bsettled status", r"\bblue card", r"\bemployment pass", r"\bworking rights", r"\blive and work", r"\bstatus in the (?:u s|us|united states)",
    ),
    "work_authorization": (
        r"\b(?:able|permitted|allowed|free|eligible|entitled|authori[sz]ed|legally|cleared) to work",
        r"\bwork (?:authori|eligib|right|permit|status|restriction|visa)", r"\bemployment (?:eligib|verification|authori|status)",
        r"\bright to work", r"\bunrestricted", r"\bwithout restrictions?\b", r"\blegally\b",
        r"\b(?:permission|right|authority|authori[sz]ation|eligibility) to (?:work|be employed)",
        r"\bwork in the (?:u s|us|usa|united states|country)",
    ),
    "criminal": (
        r"\bconvict", r"\bfelon", r"\bcriminal", r"\bcrimes?\b", r"\barrest", r"\boffen[cs]e", r"\bcourt", r"(?<!\bin )\bcharge[sd]?\b",
        r"(?<!\bone )(?<!\btwo )(?<!\bthree )(?<!\bsingle )(?<!\bfew )\bsentenc(?:e|ed|es|ing)\b", r"\bprobation", r"\bparole",
        r"\bmisdemeanou?r", r"\bbackground check", r"\bpending case", r"\bincarcerat", r"\bimprison",
        r"\bguilty", r"\bno contest", r"\bnolo\b", r"\bplea(?:d|ded)?\b", r"\bpled\b", r"\bwarrant", r"\bdui\b", r"\bdwi\b", r"\bjail", r"\bprison", r"\bpolice", r"\bindict", r"\blegal proceeding", r"\badjudicat", r"\bexpunge", r"\bsealed\b", r"\bdetained\b",
        r"\blegal matters?", r"\brestraining order", r"\blicen[sc]e\b.{0,40}\b(?:suspen|revo)", r"\blitigation", r"\blaw enforcement",
        r"\bcaution(?:ed|s)?\b", r"\boffender",
    ),
    "demographic": (
        r"\bgender", r"\bsex", r"\bfemales?\b", r"\bmales?\b", r"\bwom[ae]n\b", r"\bnon ?binary\b", r"\brace\b", r"\bracial", r"\bethnic", r"\bhispanic", r"\blatin[oax]", r"\bveteran", r"\bmilitary",
        r"\barmed forces", r"\bdisab", r"\bpronoun", r"\borientation", r"\blgbt", r"\btransgender", r"\bqueer\b", r"\breligio",
        r"\bmarital", r"\bmarried", r"\bpregnan", r"\bgenetic", r"\bage\b", r"\bbirth", r"\bdob\b", r"\byears old\b", r"\bhow old\b", r"\beeoc?\b",
        r"\bself identif",
        r"\bperson of colou?r", r"\bpeople of colou?r", r"\bbipoc", r"\bblack\b", r"\bindigenous", r"\bnative american", r"\balaska native", r"\bpacific islander", r"\bunderrepresent", r"\bminorit", r"\bover 40\b", r"\bborn\b", r"\bnational origin", r"\bmedical", r"\bhealth condition", r"\baccommodat", r"\bnational guard", r"\breserves\b", r"\bneurodiver", r"\bhe him\b", r"\bshe her\b", r"\bthey them\b", r"\braces\b",
        r"\blearning (?:difference|disabilit)", r"\badhd\b", r"\bdyslex", r"\bautis", r"\bdeaf", r"\bhard of hearing", r"\bchronic", r"\bcaregiver",
        r"\bchildren\b", r"\bcaste\b", r"\baboriginal", r"\btorres strait", r"\bfirst language", r"\bmother tongue",
    ),
    "money": (
        r"\bsalar", r"\bcompensat", r"\bpay\b", r"\bpaid\b", r"\bwages?\b", r"\bstipend", r"\bhourly\b", r"\bremunerat",
        r"\bearnings?\b", r"\bbonus", r"\b(?:pay|hourly|hour|day|week|wage|salary|desired|expected|minimum|target|base|starting|billing|annual) rate\b",
        r"\brate of pay\b", r"\b(?:expected|desired) (?:salary|compensation|pay|rate|wages?|earnings?|hourly|stipend)",
        r"\bincome", r"\bctc\b", r"\bote\b", r"\bper hour\b", r"\bhow much (?:do you |are you )?(?:currently |now )?(?:make|earn|paid)", r"\b(?:are|were|was) you (?:currently |now |still )?(?:making|earning)\b",
        r"\bcomp\b(?! (?:sci|science|eng|engineering|arch|architecture|org|bio|lit|vision|geometry|neuro|networks?|theory|systems?)\b)",
        r"\bfixed component", r"\blast drawn", r"\bvariable (?:pay|component)", r"\byour ask\b", r"\bbankrupt", r"\bcredit (?:score|check|history|report)",
    ),
    "security": (r"\bclearance", r"\bexport", r"\bitar\b", r"\bear\b", r"\bu s person", r"\bus person", r"\bsecurity", r"\bpolygraph", r"\btop secret", r"\bts sci\b", r"\bdod\b", r"\bpublic trust", r"\bbackground investigation", r"\bsanction", r"\bofac\b", r"\bsecret clearance", r"\bvetting", r"\bpoly\b", r"\baccess authori", r"\bnato\b"),
    "agreement": (
        r"\bagree", r"\backnowledg", r"\bconsent", r"\bcertif", r"\battest", r"\baffirm", r"\bdeclar", r"\bconfirm", r"\bunderstand that",
        r"\bunderstood\b", r"\baccept", r"\bterms\b", r"\bpolic(?:y|ies)\b", r"\bprivacy", r"\bnotice", r"\bdisclos", r"\bstatement",
        r"\barbitrat", r"\bhave read\b", r"\bi ve read\b", r"\breviewed\b", r"\bbound\b", r"\babide", r"\bsignature", r"\bsign here\b",
        r"\be ?sign", r"\bauthori[sz]e\b", r"\bpermission", r"\bcompl(?:y|iance|ies)\b", r"\b(?:been|was|am|are|being) informed\b", r"\binformed (?:of|that)\b", r"\bwaive", r"\bretain\b",
        r"\bretention", r"\bon file\b", r"\bhereby\b",
    ),
    "relative": (r"\brelative", r"\bfamily", r"\bspouse", r"\brelated to\b", r"\breferr", r"\bformer employee", r"\bcurrent employee", r"\bconflict of interest"),
}
# The topics no answer may be saved for or filled from the library, whichever company: the app leaves them for the student.
NEVER_STORABLE_TOPICS = ("criminal", "demographic", "money", "security")
# Topics read on a select's option labels as well as its wording. The others would over-read a plain choice list ("Security"
# as one team among several), so an option is read for the topics a person's own status is answered in.
_NET_OPTION_TOPICS = frozenset({"immigration", "work_authorization", "criminal", "demographic"})
_NET_PATTERNS = {topic: re.compile("|".join(items)) for topic, items in NET_TOPICS.items()}


# Ordinary phrases the topic lists would otherwise read as criminal or security ("take charge of a project", "in two sentences",
# "network security", "exporting data") or as pay ("hourly availability"). They are removed before the topics are read, so
# a common CS or essay prompt still gets its save form. Each is a whole phrase, never a bare topic word: "security clearance",
# "export control", "charged with", "the sentence you served" and "hourly rate" are not touched. apps/extension/apply-engine.js
# repeats this list as NET_BENIGN and tests/fixtures/apply/broad_net.json runs both.
NET_BENIGN = (
    r"\b(?:take|takes|took|taken|taking) charge\b",
    r"\b(?:in|with|within|using|about|of) (?:a|an|one|two|three|four|five|\d+(?: \d+)?|a few|a couple of|few|several) sentences?\b",
    r"\b(?:\d+(?: \d+)?|a few|few|several|a couple of|two|three|four|five) sentences\b",
    r"\b(?:network|cyber|information|application|computer|software|web|cloud|mobile|embedded|platform|infrastructure) security\b",
    r"\bsecurity (?:tools?|testing|concepts|best practices|research|vulnerabilit(?:y|ies))\b",
    r"\bexport(?:s|ed|ing)? (?:data|files?|results?|reports?|tables?|to (?:csv|excel|pdf|json|xml))\b",
    r"\bhourly (?:availability|schedule|commitment)\b",
)
_BENIGN = re.compile("|".join(NET_BENIGN))
_ADULT = "adult"   # an 18-or-older wording: possibly sensitive, but a storable kind, so never on the never-storable list
# What a question filed under a precisely sensitive one takes from it when the wording has no topic word of its own.
CATEGORY_TOPIC = {
    "work_authorization": "work_authorization", "sponsorship": "immigration", "age_18": _ADULT, "export_control": "security",
    "salary": "money", "acknowledgment": "agreement", "consent": "agreement", "uncategorized": "personal",
    "eeo_gender": "demographic", "eeo_hispanic": "demographic", "eeo_race": "demographic", "eeo_veteran": "demographic",
    "eeo_disability": "demographic",
}
# "personal" is what a question the precise classifier called uncategorized passes on: never storable, like the four above.
NEVER_TOPICS = frozenset((*NEVER_STORABLE_TOPICS, "personal"))
NET_WORDS = {
    "criminal": "criminal history", "demographic": "personal details such as age, gender or background", "money": "pay",
    "security": "security clearance or export control", "agreement": "a legal agreement", "personal": "a personal question",
}


def net_topics(text: Any) -> tuple[str, ...]:
    """The topics the broad net finds in a text, sorted. An 18-or-older wording is the topic ``adult``, not demographic."""
    words = _BENIGN.sub(" ", re.sub(r"\beighteen\b", "18", normalized_text(text)))
    plain = _AGE_18.sub(" ", words)
    found = {topic for topic, pattern in _NET_PATTERNS.items() if pattern.search(plain if topic == "demographic" else words)}
    if _AGE_18.search(words):
        found.add(_ADULT)
    return tuple(sorted(found))


def possibly_sensitive(text: Any) -> bool:
    """Whether the broad net finds anything in a text. It says "look closer", never which category."""
    return bool(net_topics(text))


def never_storable(text: Any) -> bool:
    """Whether the text hits a topic no answer may be saved for or filled from the library (criminal, demographic, money, security)."""
    return bool(set(net_topics(text)) & set(NEVER_STORABLE_TOPICS))


def plain_text(html_text: Any) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", str(html_text or ""))).split())


# Phrases read on a select's option labels for two more topics. They are narrower than the topic lists, because a plain choice list
# ("Paid", "Security" as one team among several) must not read as a question about pay or clearance.
_OPTION_EXTRA = {
    "security": re.compile(r"clearance|top secret|ts sci|\bsecret\b|public trust|polygraph"),
    "money": re.compile(r"\bsalar|\bcompensat|\bhourly\b|\bper hour\b|\bper year\b|\b\d+ ?k\b|\bhr\b|\bincome|\b\d{2,3} 000\b"),
    "demographic": re.compile(r"\basian\b|\bwhite\b|\bcaucasian|\bafrican american|\bmiddle eastern"),
}
# An option that agrees to, accepts, acknowledges, consents to, certifies or confirms something (spec 7.1, D9 B).
_AGREEMENT_OPTION = re.compile(r"\bagree|\baccept|\backnowledg|\bconsent|\bcertif|\battest|\bconfirm|\bi have read\b|\bi ve read\b|\bunderstand")
# A field that asks for a typed signature is an agreement whatever else it says.
# Typed initials are one too, and so is any "type ... to agree" instruction.
_SIGNATURE = re.compile(
    r"\bsignature\b|\be ?sign|\bsign here\b|\btype your (?:full )?(?:legal )?name\b|\binitials?\b"
    r"|\bsign(?:ed)? (?:below|off|by)\b|\bsignator|\bcountersign|\bwet ink\b"
    r"|\b(?:type|enter|print|write|input)\b.{0,60}\b(?:to|as|in) (?:agree|accept|confirm|acknowledge|consent|certify|attest)"
)
# The words of the agreement topic that, in a single-line text field's heading, make it a signature line. Narrower than the topic: "confirm",
# "permission", "notice" and "policy" are in plenty of plain questions.
_SIGNED_HEADING = re.compile(r"\b(?:agree|acknowledg|consent|certif|attest|affirm|declar|accept|waive|abide)|\bhereby\b")
_PAY_ATTENTION = re.compile(r"\bpay(?:s|ing)? (?:close |careful |special )?attention\b", re.IGNORECASE)
# A choice that is Yes or No in the student's own words: two or more of yes, no, y, n among its options.
_YES_NO_WORDS = frozenset({"yes", "no", "y", "n"})
# The mark a control that is never filled from the answer library carries in ``net_never``, whatever it says (spec 7.1, B).
TICK_MARK = "tick"


def _yes_no_like(options: Iterable[str]) -> bool:
    return len({normalized_text(option) for option in options} & _YES_NO_WORDS) >= 2


def field_net(item: SchemaField, control: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """(the topics a form field's own words hit, the marks that leave it for the student whatever its topics).

    Its label is read, and its description; a box's options too, since that is where its statement is; a select's options for the
    topics a person's own status is answered in and for a few narrow phrases (pay, clearance). The marks: a checkbox, a single one
    or a group, is never filled from the answer library (``tick``); a box, a Yes/No-like question or a select whose options agree
    to something, and a typed signature, are an ``agreement``. Only an exact stored statement may tick or choose those (D9 B).
    """
    box = control == "checkbox"
    # A select with one option cannot be a choice: it is a tick box in a select's clothes ("Hybrid, three days on site"), so it is read as one.
    single = control in ("select", "multiselect") and len(item.options) == 1
    yes_no = control == "select" and (_yes_no(item.options) or _yes_no_like(item.options))
    parts = [item.label]
    if box or single:
        parts.extend(item.options)
    own = set(net_topics(" ".join(parts)))
    description = net_topics(_PAY_ATTENTION.sub(" ", plain_text(item.description)))
    # A description is where a form sometimes puts the real question ("Please list any criminal convictions here"). Its own agreement
    # and family words are usually boilerplate, so those count only on a box or a Yes/No question, whose whole point is the statement.
    own |= set(description) if box or single or yes_no else set(description) - {"agreement", "relative"}
    options = [normalized_text(option) for option in item.options]
    if control in ("select", "multiselect") and options:
        own |= set(net_topics(" ".join(options))) & _NET_OPTION_TOPICS
        own |= {topic for topic, pattern in _OPTION_EXTRA.items() if any(pattern.search(option) for option in options)}
    marks: list[str] = []
    if control in ("checkbox", "multiselect") or single:
        marks.append(TICK_MARK)
    # A select, radio or multiselect is an agreement when an agreement word is in its options or in its heading or description:
    # "Do you certify that your answers are true?" with the options "Yes I do" / "Yes I do not" says it in the heading alone. The
    # narrow list is not the only reader: the broad net's agreement topic is read on each option and on the heading too ("I will
    # comply", "I waive my right"). A single-line text field whose heading states an agreement ("Acknowledged by (your name)") is a
    # signature line; a longer answer is not read that way, so a statement of interest is still an essay, and a heading that only
    # shares a word with the topic ("Please confirm your employment eligibility status") stays a question.
    choice_words = [normalized_text(item.label), normalized_text(plain_text(item.description))]
    if (
        ((box or single or yes_no) and "agreement" in own)
        or (
            control in ("select", "multiselect")
            and (
                any(_AGREEMENT_OPTION.search(text) for text in (*options, *choice_words))
                or any("agreement" in net_topics(text) for text in (item.label, *item.options))
            )
        )
        or (control in ("text", "textarea") and _SIGNATURE.search(" ".join(choice_words)))
        or (control == "text" and _SIGNED_HEADING.search(normalized_text(item.label)))
    ):
        marks.append("agreement")
    return frozenset(own), tuple(marks)


# A box or a Yes/No question that asks the student to agree to something is an acknowledgment, whatever its
# heading says ("Candidate Privacy Statement"): the statement is in the option's text or the description. The
# short list is read on both; the longer one only on a checkbox, whose whole job is to agree.
_AGREE_WORDS = re.compile(r"acknowledg|\bterms\b|privacy (?:statement|notice|policy)|\baccepts? (?:the|our|its|these|this|all)\b|\babide\b|\bbound by\b")
_AGREE_BOX_WORDS = re.compile(
    r"\bagree|\baccept|\bpolicy\b|\bcertif|\bread\b|\breviewed?\b|\bunderstood\b|\babide|\bbound\b|\breceiv(?:e|ed|es|ing)\b|\bi ve read\b"
    # The broad net (NET_TOPICS) is the safety floor for a box that agrees in other words; these are the common ones it caught.
    r"|\bcompl(?:y|ies)\b|\bdeclar|\bauthori[sz]e\b|\bwaive|\bbeen informed\b|\bpermission\b"
)
_TAGS = re.compile(r"<[^>]*>")
# An option this long names what it agrees to; a shorter one ("I agree", "Yes", "I accept the terms") does not.
_SPECIFIC_STATEMENT_WORDS = 6
# An option that points at text elsewhere on the form names nothing itself, however long it is.
_REFERS_ELSEWHERE = re.compile(
    r"\b(?:above|below|following|foregoing|aforementioned|herein"
    r"|(?:the|these|those|this|that) (?:terms|statement|notice|policy|policies|agreement|document|declaration"
    r"|conditions?|requirements?|provisions?|arrangements?|clauses?|obligations?|rules?|expectations?))\b"
)


def _yes_no(options: Iterable[str]) -> bool:
    words = {normalized_text(option) for option in options}
    return bool(words) and words <= {"yes", "no"}


def statement_control(control: str, options: Iterable[str]) -> bool:
    """Whether a stored, ticked statement can be put into this control: a box, or a Yes/No question with one "Yes"."""
    return control == "checkbox" or (control == "select" and _yes_no(options))


def _leaning_heading(item: SchemaField, heading: str) -> str:
    """The heading with the question above it in front, for a statement that leans on text outside its own words."""
    prefix = f"{item.parent} / "
    return heading if not item.parent or heading.startswith(prefix) else f"{prefix}{heading}"


def _statement_parts(item: SchemaField, control: str, category: str = "", answer_key: str = "") -> tuple[str, bool]:
    """(the text a stored answer to this field is matched on, whether that text leans on words outside the option).

    ``answer_key`` is the text the plan files the question under (``_answer_key``): the heading, with the question above it in
    front for a follow-up, a short heading and a heading the form repeats. The whole statement the student sees is matched, never
    the option alone: however long an option is, "I have read and agree to the following" names nothing.
    """
    heading = answer_key or item.label
    yes_no = control == "select" and category in STATEMENT_CATEGORIES and _yes_no(item.options)
    if not yes_no and (control != "checkbox" or not item.options):
        return item.label, False
    description = plain_text(item.description)
    # A box says what it agrees to in its option, a Yes/No question in its question: that is the text that has to be specific.
    own = normalized_text(item.label) if yes_no else normalized_text(item.options[0])
    short = len(own.split()) < _SPECIFIC_STATEMENT_WORDS
    refers = bool(_REFERS_ELSEWHERE.search(own))
    # A box that states a fact about the student ("Yes, this is true for me right now") is the answer to its heading, however
    # short the option is: a work-authorization or 18-or-older box carries its heading, which is that question, and is not
    # about the employer. Any other statement that is short or points elsewhere leans on the question above it as well.
    about_student = category in TICKABLE
    leans = short or refers
    if leans and not about_student:
        heading = _leaning_heading(item, heading)
    option = "" if yes_no else item.options[0]
    # What sits in front of the heading (the question above it) is kept whichever words repeat.
    lead = heading[: -len(item.label)] if item.label and heading.endswith(item.label) else ""
    label = item.label
    if option and label:
        # A heading and an option that say the same thing are one statement, not two.
        title, chosen = f" {normalized_text(label)} ", f" {normalized_text(option)} "
        if title.strip() and title in chosen:
            label = ""
        elif chosen.strip() and chosen in title:
            option = ""
    return (
        lead + " ".join(part for part in (label, option, description) if part.strip()),
        bool(description) or refers or (short and not about_student),
    )


def statement_of(item: SchemaField, control: str, category: str = "", answer_key: str = "") -> str:
    """The text a stored answer to this field is matched on: a checkbox's whole statement, else its question.

    A checkbox is its heading, its option and its description together, and a Yes/No agreement question is its
    question and its description: an option alone ("I agree", or a long generic "I have read and agree to the following")
    is never the statement, so two boxes that agree to different things never share a stored answer. A statement whose own words
    are short or point elsewhere carries the question above it too. A box that answers a question about the student (work
    authorization, sponsorship, 18 or older) carries its heading, which is that question. ``category`` says which kind of
    question it is, and ``answer_key`` is the heading as the plan files it.
    """
    return _statement_parts(item, control, category, answer_key)[0]


def statement_needs_company(item: SchemaField, control: str, category: str = "", answer_key: str = "") -> bool:
    """Whether a statement leans on a description, on text elsewhere or on its heading, so a stored answer is kept for one company."""
    return _statement_parts(item, control, category, answer_key)[1]


def classify_item(item: SchemaField, control: str, parent: str | None = None, follows: bool = False) -> str | None:
    """The category of one form field: its question, and for what has no wording of its own, what it depends on.

    A checkbox and a Yes/No question are read on their heading, their option text and their description
    together. ``follows`` says the field is filed under the question above it (a follow-up such as "If yes,
    please explain"), which passes that question's own category on: ``parent`` is that question's category, and
    its label is read as well, for a parent the listing does not carry. The most restrictive result wins.
    """
    found = [classify_sensitive(item.label, item.options, item.section, item.name)]
    if item.section == "custom":
        agreeing = control == "checkbox"
        if agreeing or (control == "select" and _yes_no(item.options)):
            statement = item.options[0] if agreeing and item.options else ""
            description = html.unescape(_TAGS.sub(" ", item.description))
            found.extend(classify_sensitive(text) for text in (statement, description) if text.strip())
            words = normalized_text(f"{item.label} {statement} {description}")
            if _AGREE_WORDS.search(words) or (agreeing and _AGREE_BOX_WORDS.search(words)):
                found.append("acknowledgment")
        if follows and item.parent:
            found.extend((classify_sensitive(item.parent), parent))
    result = most_restrictive(found)
    # A field whose own words ask for voluntary self-identification as well ("If other, please specify your gender" under a
    # work authorization question, "Do you require sponsorship? What is your race?") is never offered as a work authorization,
    # sponsorship or 18-or-older answer: no form for it may offer demographic options, and no such value is stored (D5 C (i)).
    # The broad net's demographic topic reads the whole statement a box or Yes/No question shows (its heading, its option and its
    # description), not only the heading: "I am authorized to work in the United States and I am a protected veteran" is a
    # demographic claim, not a work-authorization one.
    if result in _NOT_WITH_EEO and (eeo_words(item.label) or claims_never_storable(item, control)):
        return "uncategorized"
    return result


_NOT_WITH_EEO = frozenset({"work_authorization", "sponsorship", "age_18"})


def _claim_parts(item: SchemaField, control: str) -> list[str]:
    parts = [item.label]
    if control == "checkbox" or (control == "select" and _yes_no(item.options)):
        parts.append(plain_text(item.description))
    if control == "checkbox":
        parts.extend(item.options)
    elif control in ("select", "multiselect"):
        # A work-authorization list whose option is "Yes, and I am a protected veteran" asks for more than work authorization.
        parts.extend(item.options)
    return parts


def claims_demographic(item: SchemaField, control: str) -> bool:
    """Whether a field's heading, and for a box or a Yes/No question its option and description, hit the demographic topic.

    An 18-or-older wording is not demographic here (``net_topics`` reads it as ``adult``). A work-authorization,
    sponsorship or 18-or-older answer that also claims a demographic is never stored or filled (D5 C (i)).
    """
    return "demographic" in net_topics(" ".join(_claim_parts(item, control)))


def claims_never_storable(item: SchemaField, control: str) -> bool:
    """Whether a field's own words (heading, options and description) also hit criminal, demographic, money or security.

    A work-authorization, sponsorship or 18-or-older answer that also claims a criminal record, a demographic, pay or clearance
    ("...and I am not currently on probation") is never stored or filled: it is left for the student (D5 C (i), spec 7.3 "As built").
    """
    return never_storable(" ".join(_claim_parts(item, control)))


def eeo_words(text: Any) -> bool:
    """Whether the words themselves ask a voluntary self-identification (EEO) question, whatever else they ask."""
    words = normalized_text(text)
    return any(pattern.search(words) for category, pattern in _PATTERNS.items() if category.startswith("eeo_"))
