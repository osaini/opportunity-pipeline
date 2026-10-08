"""Text helpers of the legacy pipeline: HTML stripping, URL canonicalisation and the posting fingerprints.

The description fingerprint (a SimHash over word shingles) catches the same posting arriving from an employer
board and from an aggregator that rewrote its title.
"""

from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
from html.parser import HTMLParser

from .identity import normalized


# How the Ashby adapter opens the sentence it writes from the posting's structured pay. Scoring reads a description that
# is only this sentence as no description.
ASHBY_PAY_SENTENCE_START = "Pay listed on the Ashby posting:"

# Elements that end a line of text. Without the break, "<li>1+ years of experience</li><li>Following graduation ...</li>"
# reads as one sentence, and a later bullet becomes the tail of the one before.
_BLOCK_TAGS = frozenset({
    "address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "ol", "p", "pre", "section", "table", "tr", "ul",
})
# A character no posting text contains, standing for a block break until the whitespace is collapsed.
_BLOCK_BREAK = "\x00"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append(_BLOCK_BREAK)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append(_BLOCK_BREAK)

    def handle_data(self, data: str) -> None:
        self.parts.append(data.replace(_BLOCK_BREAK, " "))


_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>|<!--")


def strip_html(value: str | None) -> str:
    """The text of an HTML fragment: whitespace collapsed, with each block element (paragraph, list item, heading,
    line break) ending its own line. In HTML a line break in the source is only whitespace, as a browser shows it; text
    with no tags at all (Lever's ``descriptionPlain``, a pasted description) keeps its own line breaks, since there they
    are the only thing that separates one requirement from the next."""
    text = html.unescape((value or "").replace(_BLOCK_BREAK, " "))
    if _TAG_RE.search(text):
        parser = _TextExtractor()
        parser.feed(text)
        lines = re.sub(r"\s+", " ", " ".join(parser.parts)).split(_BLOCK_BREAK)
    else:
        lines = [re.sub(r"\s+", " ", line) for line in re.split(r"\r\n|\r|\n", text)]
    return "\n".join(line for line in (line.strip() for line in lines) if line)


class _ApplyControlExtractor(HTMLParser):
    """Collect the labels of clickable controls (anchors, buttons, submits).

    `classify_liveness` reads these separately from the body text: a visible
    apply control is the single strongest evidence that a posting is still
    open, and it has to be distinguishable from the same words appearing in
    prose ("we will apply your feedback").
    """

    _CONTROL_TAGS = {"a", "button"}
    _LABEL_ATTRS = ("value", "aria-label", "title")

    def __init__(self) -> None:
        super().__init__()
        self.controls: list[str] = []
        self._depth = 0
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"input", "button"}:
            values = dict(attrs)
            for key in self._LABEL_ATTRS:
                label = (values.get(key) or "").strip()
                if label:
                    self.controls.append(label)
        if tag in self._CONTROL_TAGS:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._CONTROL_TAGS and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self._flush()

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._buffer.append(data)

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        self._buffer.clear()
        if text:
            self.controls.append(text)

    def finish(self) -> list[str]:
        # A page that never closes its last anchor still has a label worth
        # reading, and unclosed tags are common enough in real ATS markup that
        # dropping the tail would lose apply controls on exactly those pages.
        self._flush()
        return self.controls


def apply_controls(markup: str | None) -> list[str]:
    parser = _ApplyControlExtractor()
    parser.feed(markup or "")
    return parser.finish()


def canonical_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    allowed = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower()
        not in {
            "gh_src",
            "lever-source",
            "source",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "trackingid",
            "refid",
            "trk",
            "trkemail",
            "midtoken",
            "midsig",
            "eid",
            "licu",
        }
        and not key.lower().startswith("utm_")
    ]
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), urllib.parse.urlencode(allowed), "")
    )


def fingerprint(company: str, title: str, location: str) -> str:
    basis = "|".join((normalized(company), normalized(title), normalized(location)))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Description fingerprinting
#
# Ported from career-ops (https://github.com/santifer/career-ops), MIT licence,
# (c) 2026 Santiago Fernandez de Valderrama -- see THIRD_PARTY_NOTICES.md. The
# upstream file is `fingerprint-core.mjs`.
#
# The same job can enter the pipeline twice under names that neither the exact
# fingerprint nor the company+title pass can reconcile: once from the employer's
# own ATS board and once from an aggregator that rewrote the title and restyled
# the company name. Aggregators rarely rewrite the requirements text, so a
# content fingerprint of the description body catches that pair.
#
# Design: 64-bit SimHash over 3-token shingles of the normalised description.
# SimHash keeps near-duplicate texts within a few bits of each other, so one
# 16-hex-character column per row is enough to compare any later pair without
# storing the body twice. No dependencies, no model calls.
# ---------------------------------------------------------------------------

# Descriptions shorter than this carry too little signal to tell a real match
# from shared boilerplate.
FINGERPRINT_MIN_TEXT = 200

# Similarity at or above this is treated as the same posting. 0.92 means at
# most 5 of 64 SimHash bits differ -- near-verbatim bodies only.
CROSSLIST_THRESHOLD = 0.92


def normalize_jd_text(text: str | None) -> str:
    """Reduce a description to a bare token stream: no tags, entities, or URLs."""
    value = str(text or "").lower()
    value = re.sub(r"<[^>]*>", " ", value)
    value = re.sub(r"&[a-z#0-9]+;", " ", value)
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[\W_]+", " ", value, flags=re.UNICODE)
    return value.strip()


def fingerprint_text(text: str | None) -> str:
    """64-bit SimHash of a description as 16 hex characters, or '' when unusable."""
    normalised = normalize_jd_text(text)
    if len(normalised) < FINGERPRINT_MIN_TEXT:
        return ""
    tokens = normalised.split(" ")
    # Length alone can pass on fewer than 3 tokens -- an unspaced CJK body
    # normalises to one giant token. No shingle would ever be hashed, leaving an
    # all-zero hash that would then score 1.0 against every other degenerate
    # body. Treat those as unfingerprintable instead.
    if len(tokens) < 3:
        return ""
    # SimHash: bit i is set when more shingles have bit i set than clear. With
    # `shingles` hashes, that is `2 * (shingles with the bit set) > shingles`; a
    # tie leaves the bit unset. Counting one column of the 64-character binary
    # strings at a time does the same tally as a per-bit Python loop per
    # shingle, at about a third of the cost. format(..., "064b") is big-endian,
    # so column `column` is bit 63 - column.
    shingles = len(tokens) - 2
    columns = zip(
        *(
            format(
                int(hashlib.sha256(" ".join(tokens[index : index + 3]).encode("utf-8")).hexdigest()[:16], 16),
                "064b",
            )
            for index in range(shingles)
        )
    )
    value = 0
    for column, bits in enumerate(columns):
        if 2 * bits.count("1") > shingles:
            value |= 1 << (63 - column)
    return f"{value:016x}"


def fingerprint_similarity(left: str, right: str) -> float:
    """Share of the 64 SimHash bits two fingerprints agree on, 0.0 when either is blank."""
    if not left or not right:
        return 0.0
    distance = bin(int(left, 16) ^ int(right, 16)).count("1")
    return (64 - distance) / 64


def stable_id(source_key: str, external_id: str) -> str:
    return hashlib.sha256(f"{source_key}|{external_id}".encode("utf-8")).hexdigest()[:16]


def classify_role(title: str, description: str) -> str:
    # Role type is a property of the posting title. Descriptions often mention
    # unrelated intern/co-op programs and otherwise create false classifications.
    text = title.lower()
    checks = (
        ("co-op", ("co-op", "coop")),
        ("externship", ("externship", "extern ")),
        ("research", ("research experience", "research assistant", "undergraduate research", "reu ")),
        ("internship", ("internship", "intern ", " intern", "summer analyst")),
        ("part_time", ("part-time", "part time", "student assistant", "student technician")),
        ("early_career", ("new grad", "early career", "entry level", "engineer i", "associate engineer")),
    )
    for role_type, terms in checks:
        if any(term in text for term in terms):
            return role_type
    return "other"
