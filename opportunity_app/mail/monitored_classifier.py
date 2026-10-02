"""Keyword rules that read what an application email says: an offer, a rejection, an interview, an assessment.

Pure text rules, standard library only. connections.ingest_message and application_inbox use them
as the fallback beside the Jev classifier (inbox_classifiers.classify_email).
"""

from __future__ import annotations

import re


# The keyword rules for an application email, first match wins, in the order
# inbox_classifiers.EMAIL_QUESTION asks Jev to use. Assessment and scheduling
# add tasks, never a stage. application_inbox adds what the sender says (an
# assessment platform, a scheduling link) on top of these.
MONITORED_PATTERNS = (
    # "Pleased to offer" counts only for a job, never for an interview (_is_job_offer): "happy to offer
    # you an interview slot" is an invitation, "pleased to offer you the opportunity to join" an offer.
    ("offer", (
        r"\b(offer of employment|offer letter|(extend|extending) (you )?an offer"
        r"|(pleased|happy|delighted|excited|thrilled|glad) to (offer|extend) (you|the|a|an|this|our))\b"
    ), 0.95),
    # A definite statement only: "other candidates" or "not selected" said of the process in general
    # ("along with other candidates", "if you are not selected") is not a rejection (_definite).
    ("rejected", (
        r"\b(not moving forward|regret to inform|(the |this )?(position|role|opening) has (now |already )?been filled"
        r"|(mov(e|ed|ing)|proceed(ed|ing)?|go(ing)?|went|gone|continu(e|ed|ing)) ((forward|ahead|on) )?with"
        r" (an?other|other|different|a different|(some|several|the) other|more qualified|stronger|more experienced)"
        r" (candidates?|applicants?)"
        r"|(selected|chosen|chose|hired|identified|decided on|pursued?|pursuing|offered the (position|role) to)"
        r" (an?other|other|a different) (candidates?|applicants?)"
        r"|other (candidates|applicants) (whose ([\w-]+ ){1,4})?(more closely|better) (match|matches|matched|align|aligns|aligned|fit|fits|meet|meets|met)"
        r"|other (candidates|applicants) (who |that )?(were|was|are|is) (a )?(better|stronger|closer) (fit|match)"
        r"|no longer (under consideration|being considered)|not (been )?selected (for|to)"
        r"|(will not|won't|decided not to|unable to|not be able to) (be )?(mov(e|ing) (you |your application |your candidacy )?forward"
        r"|proceed(ing)? with your|continu(e|ing) with your|advanc(e|ing) your))\b"
    ), 0.92),
    ("interview", (
        r"\b(schedule|invite|invitation).{0,30}\binterview\b|\binterview availability\b"
        r"|\byour interview (is |has been )?(confirmed|scheduled)\b"
        r"|\b(like|love|want) to (invite you to|schedule|set up|arrange) (an? |some time for an? )?"
        r"(phone |video |technical |virtual |first[- ]round |final[- ]round |onsite |on-site )?(interview|phone screen|screening call)\b"
        r"|\b(offer|offering) you (an? |the )?([\w-]+ ){0,2}(interview|phone screen|screening call)\b"
    ), 0.9),
    ("assessment", (
        r"\b(online assessment|coding (challenge|assessment|test|exercise)|technical (assessment|challenge)"
        r"|take[- ]home (assignment|exercise|challenge|project)|assessment (link|invitation|invite)"
        r"|(complete|take|finish) (the |your |an |this |our )?([a-z]+ ){0,2}(assessment|coding test|challenge))\b"
    ), 0.88),
    ("scheduling", (
        r"\b((pick|choose|select|book) a (time|slot)|schedule a (time|call|chat|meeting)|share your availability"
        r"|let us know your availability|calendly\.com|goodtime\.io|modernloop\.io)\b"
    ), 0.85),
    ("application_confirmation", (
        r"\b(application (?:was |has been )?received|thank you for applying|thanks for applying|submission confirmation"
        r"|(we have |we've )?received your application)\b"
    ), 0.9),
    ("deadline", r"\b(deadline|complete by|due by)\b", 0.65),
    ("recruiter_reply", r"\b(recruiter|talent acquisition|hiring team)\b", 0.55),
)
# A rejection of one role that asks about another is not a plain rejection:
# it keeps its label, but no longer sure enough to act on alone.
_HEDGED_REJECTION = re.compile(
    r"\b(consider(ing)? you for|would you be (open|interested)|like to (move|put) you forward for"
    r"|great fit for (another|a different)|another (role|position|opening) (that|which|we))\b"
)
HEDGED_REJECTION_CONFIDENCE = 0.7

# A phrase said of what may happen is not news that it did. "If you are not
# selected", "until the position has been filled" and "we may not be able to
# move forward with all applicants" are what confirmations say; so are "if your
# background is a match, a recruiter will reach out to schedule a call" and "we
# invite the strongest candidates to interview". _definite reads the grammar
# around a match, not just nearby words, since the same words open real news:
# "we know this may be disappointing, but we will not be moving forward",
# "once again, we regret to inform you", "following your application in May,
# we would like to invite you to interview", "if you're available, we would
# like to schedule an interview" and "if you are still interested, please let
# us know your availability" are all definite.
_SENTENCE_MARKS = ".!?;\n"
# Where a clause ends inside a sentence.
_CLAUSE_BREAK = re.compile(r"[,:()]|\s[-\u2013\u2014]+\s|\bbut\b")
_DAYS_AND_MONTHS = (r"january|february|march|april|may|june|july|august|september|october|november|december"
                    r"|jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec|(mon|tues|wednes|thurs|fri|satur|sun)day")
# A word that makes what follows it conditional, up to its main clause: "if", "unless", "until the
# position has been filled", "once we have reviewed", "should you be selected", "in the event that".
# Not "once again", "at once", "you have until Friday" or "until October 5".
_CONJUNCTION = re.compile(
    r"\b(if|unless|whether|in case|in the event"
    r"|(?<!at )(?<!than )once(?! (again|more)\b)"
    r"|(?<!have )(?<!has )until(?! (then|now|today|tomorrow|tonight|midnight|noon|next|this|last|the end|end|"
    + _DAYS_AND_MONTHS + r")\b)(?! \d)"
    r"|should(?= (you|we|they|your|our|the|there|it|this|that|a|an|any|anyone)\b))\b"
)
# "May" or "might" as a modal: not the month ("in May", "mid-May", "May 5"), nor a request ("may we
# schedule"). _modal also leaves out permission to do the task ("you may now book a time").
_MODAL = re.compile(
    r"(?<!\bin )(?<!\bon )(?<!\bof )(?<!\bby )(?<!\bsince )(?<!\bearly )(?<!\bmid )(?<!\blate )(?<!\bthis )"
    r"(?<!\bnext )(?<!\blast )(?<!\bfrom )(?<!\buntil )(?<!-)"
    r"\b(might|may(?! (i|we)\b)(?!,? \d)(?! (and|or|through|to)\b))\b"
)
_PERMISSION = re.compile(
    r"may (now |also |then |still )?(schedule|book|pick|choose|select|complete|take|start|begin|access|use|reply|respond"
    r"|proceed|log in|sign in|click)\b"
)
_FUTURE = re.compile(r"\b(will|we'll|they'll|you'll|it'll|shall|going to)\b")
# Tense or mood that leaves the main clause after a condition contingent: "if selected, we will"
# (and a modal, _modal).
_CONTINGENT = re.compile(r"\b(will|we'll|they'll|you'll|it'll|shall|going to|could|would(?! (like|love)\b))\b")
# What opens a main clause that is a real request after a condition that is not about being chosen:
# "if you're available, we would like to", "if it works for you, please schedule a time". After "if
# you are selected" (_SELECTION) a request is as contingent as the selection.
_REQUEST = re.compile(
    r"\b(please|kindly|feel free"
    r"|(we|i)('d| would)( really)? (like|love)|(we|i)('d| would) be (happy|glad|delighted|pleased)"
    r"|(we|i)('re| are| am) (happy|glad|delighted|pleased|excited) to|(we|i) (invite|want) you)\b"
)
# A task a main clause asks for, by its first word: "if still interested, let us know your availability".
_IMPERATIVE = re.compile(r"(pick|choose|select|book|schedule|share|let us know|complete|take|finish|click|use)\b")
_SELECTION = re.compile(
    r"\b(selected|shortlisted|chosen|successful|qualif\w*|an? (good |strong |great |close )?(fit|match)|advance|advances"
    r"|progress|progresses|move forward|moving forward|pass|passes|meets? (our|the|all)|considered|consider you|we decide)\b"
)
# The student, addressed as the one invited: future tense then is a plan, not a maybe ("we will invite you").
_TO_YOU = re.compile(
    r"\b(invite you|invited you|you('re| are| will be| have been) invited|send you an? ([\w-]+ )?invitation"
    r"|you('ll| will) (receive|get) an? ([\w-]+ )?(invitation|invite)|your ([\w-]+ ){0,2}interview|interview with you)\b"
)
_OTHERS = re.compile(r"\b(selected|strongest|shortlisted|qualified|successful|top|chosen|a few|some) (candidates|applicants)\b")
# The labels a hedge can undo (an offer is always a proposal, so it is never hedged away).
_HEDGED = {"rejected", "interview", "assessment", "scheduling"}
_FOR_OTHERS = {"interview", "assessment", "scheduling"}


def _bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Where the sentence holding text[start:end] begins and ends."""
    left = max(text.rfind(mark, 0, start) for mark in _SENTENCE_MARKS) + 1
    rights = [index for index in (text.find(mark, end) for mark in _SENTENCE_MARKS) if index != -1]
    return left, (min(rights) if rights else len(text))


def _clause_start(text: str, low: int, high: int) -> int:
    start = low
    for found in _CLAUSE_BREAK.finditer(text, low, high):
        start = found.end()
    return start


def _clause_end(text: str, low: int, high: int) -> int:
    found = _CLAUSE_BREAK.search(text, low, high)
    return found.start() if found else high


def _modal(text: str, low: int, high: int) -> bool:
    """Whether text[low:high] has a modal "may" or "might" that is not permission given to the student."""
    return any(
        not (text[max(0, modal.start() - 4):modal.start()] == "you " and _PERMISSION.match(text, modal.start()))
        for modal in _MODAL.finditer(text, low, high)
    )


def _conditional(text: str, sentence_start: int, clause_start: int, found: re.Match[str]) -> bool:
    """Whether a condition earlier in the sentence governs the match."""
    conditions = list(_CONJUNCTION.finditer(text, sentence_start, found.start()))
    if not conditions:
        return False
    condition = conditions[-1]
    main = _clause_end(text, condition.end(), found.start())
    if _SELECTION.search(text, condition.start(), main):
        return True  # whatever follows waits on being chosen: "if selected, please book a time"
    if _REQUEST.search(text, condition.end(), found.start()):
        return False  # a request: "if you're interested, please book a time"
    if main < found.start() and not text[clause_start:found.start()].strip() and _IMPERATIVE.match(text, found.start()):
        return False  # a request by its verb: "if still interested, let us know your availability"
    if condition.start() >= clause_start:
        return True  # the match is inside the condition: "if you are not selected for this role"
    # The match is in the main clause after the condition: contingent only when that clause is.
    return bool(_CONTINGENT.search(text, main, found.end())) or _modal(text, main, found.end())


def _definite(event_type: str, text: str, found: re.Match[str]) -> bool:
    """Whether one match states what happened, rather than what might."""
    if event_type == "offer":
        return _is_job_offer(text, found)
    if event_type not in _HEDGED:
        return True
    sentence_start, sentence_end = _bounds(text, found.start(), found.end())
    clause_start = _clause_start(text, sentence_start, found.start())
    clause_end = _clause_end(text, found.end(), sentence_end)
    # A modal in the match's own clause: "we may not be able to move forward with all applicants".
    if _modal(text, clause_start, found.start()):
        return False
    if _conditional(text, sentence_start, clause_start, found):
        return False
    if event_type not in _FOR_OTHERS:
        return True
    # An invitation or a task meant for others: "we invite the strongest candidates to interview".
    if _OTHERS.search(text, clause_start, clause_end):
        return False
    # Future tense is a promise to get in touch ("we will reach out to schedule an interview"),
    # unless it invites the student outright ("we will invite you to an onsite interview next week")
    # with nothing after it making that conditional ("... if your application is selected").
    if _FUTURE.search(text, clause_start, found.start()):
        return bool(_TO_YOU.search(text, clause_start, found.end())) and not _CONJUNCTION.search(text, found.end(), clause_end)
    return True


_JOB = re.compile(r"\b(position|role|internship|intern|job|co-?op|offer|employment|join|joining|team|hire)\b")
_NOT_A_JOB = re.compile(
    r"\b(interviews?|phone|video|call|chat|screen|screening|meeting|slot|conversation|assessment|challenge|test|feedback)\b"
)


def _is_job_offer(text: str, found: re.Match[str]) -> bool:
    """An offer phrase that offers a job. "Pleased to offer you the opportunity to join Acme as an intern" and
    "pleased to offer the Software Intern position to you" do; "happy to offer you an interview slot", "pleased
    to offer you a phone screen" and "glad to offer you feedback" do not."""
    if not re.search(r"to (offer|extend) \w+$", found.group(0)):
        return True  # "offer letter", "offer of employment", "extend you an offer"
    _start, end = _bounds(text, found.start(), found.end())
    rest = text[found.end() - len(found.group(0).rsplit(" ", 1)[-1]):min(end, found.end() + 120)]
    job = _JOB.search(rest)
    other = _NOT_A_JOB.search(rest)
    return job is not None and (other is None or job.start() < other.start())


def classify_monitored_message(subject: str, body: str) -> tuple[str, float]:
    # A curly apostrophe reads as a straight one ("we'll", "won't" typed on a phone); the length stays the same.
    text = f"{subject}\n{body}".lower().replace("\u2019", "'")
    for event_type, pattern, confidence in MONITORED_PATTERNS:
        if any(_definite(event_type, text, found) for found in re.finditer(pattern, text, re.DOTALL)):
            if event_type == "rejected" and _HEDGED_REJECTION.search(text):
                return event_type, HEDGED_REJECTION_CONFIDENCE
            return event_type, confidence
    return "unknown", 0.1
