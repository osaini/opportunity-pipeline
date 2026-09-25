"""One-word industry tags for each company, generated from its own postings.

A tag is a classification, not a fact the employer stated, so every automatic
tag carries the evidence it was inferred from and the UI marks it as inferred.
Generation is deterministic keyword matching: free, inspectable, and the same
on every run.

Tags are keyed by the company's stored fold (``company_sort_key``), so every
posting from one company shares them. Automatic tags live in ``company_tags``
and are rebuilt on every sync, and on startup whenever the rules below have
changed since the last rebuild. A student's own edits live in
``company_tag_choices`` and outlive every rebuild: removing an automatic tag
records a 'removed' choice, so the next sync cannot bring it back.

Outreach companies usually have no posting, so their tags come from the
student's own research summary instead, stored per student in
``outreach_company_tags`` (one student's private research never tags a
company for another). Both kinds share the company key and the choices, so
removing a tag removes it on Discover and on Outreach alike.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from pipeline_core.visibility import capture_visible_sql

MAX_AUTO_TAGS = 3
# A name match, a title match, or two different keywords across the
# company's descriptions. One keyword repeated in every description is not
# enough: a company's boilerplate is copied into all of its postings, so
# counting repeats would let a benefits paragraph outvote the actual work.
MIN_SCORE = 2
NAME_WEIGHT = 3
TITLE_WEIGHT = 2
TITLE_CAP = 4
DESCRIPTION_CAP = 2
# A niche tag also counts when its words recur across the company's postings:
# in at least this many, and at least this share of them. That recurring text
# is usually the company describing itself ("the largest drone delivery
# service"), which is exactly the signal a narrow tag needs.
NICHE_MIN_POSTINGS = 2
NICHE_MIN_SHARE = 0.2

TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


@dataclass(frozen=True)
class TagRule:
    tag: str
    # Matched in the company name, titles, and descriptions.
    keywords: tuple[str, ...]
    # Matched only in the company name: words too common in postings to mean
    # anything there ("bank holiday", "health insurance").
    name_only: tuple[str, ...] = ()
    # Narrow enough that repetition is evidence rather than boilerplate, and
    # never crowded out by the MAX_AUTO_TAGS broad ones.
    niche: bool = False


# Keywords are regex fragments matched on word boundaries, case-insensitively.
# Deliberately absent: words every benefits or EEO paragraph contains
# ("insurance", "healthcare", "military", "veteran"), and generic stack words
# ("software", "data", "AWS") that would tag every company alike.
RULES: tuple[TagRule, ...] = (
    TagRule("robotics", (r"robot(?:s|ic|ics)?", r"autonomous (?:systems?|robots?|vehicles?)", r"humanoid", r"manipulators?")),
    # Its own tag, not part of robotics: a drone company may be either, both,
    # or aerospace, and the student sorts by it on its own.
    TagRule("drone", (r"drones?", r"uavs?", r"s?uas", r"unmanned aerial (?:vehicles?|systems?)", r"unmanned aircraft(?: systems?)?", r"quadcopters?", r"multirotors?"), (r"drones?",), niche=True),
    TagRule("ai", (r"artificial intelligence", r"machine learning", r"deep learning", r"large language models?", r"llms?", r"generative ai", r"computer vision", r"neural networks?", r"reinforcement learning"), (r"ai",)),
    TagRule("semiconductors", (r"semiconductors?", r"asics?", r"fpgas?", r"vlsi", r"wafers?", r"lithography", r"chip design", r"silicon validation", r"rtl design")),
    TagRule("hardware", (r"pcb(?:a|s)?", r"embedded systems?", r"firmware", r"consumer electronics", r"power electronics", r"electronics design", r"circuit boards?", r"hardware engineering")),
    TagRule("aerospace", (r"aerospace", r"aircraft", r"avionics", r"satellites?", r"spacecraft", r"rockets?", r"launch vehicles?", r"propulsion", r"aviation"), (r"space", r"spacex")),
    TagRule("defense", (r"defense contractor", r"national security", r"department of defense", r"dod", r"security clearance", r"missiles?", r"warfighters?"), (r"defen[cs]e",)),
    TagRule("automotive", (r"automotive", r"electric vehicles?", r"powertrains?", r"adas", r"self-driving", r"chassis"), (r"motors?", r"automotive")),
    TagRule("energy", (r"renewables?", r"renewable energy", r"clean energy", r"solar", r"wind (?:energy|power|turbines?|farms?)", r"batter(?:y|ies)", r"power grid", r"electric grid", r"grid-scale", r"oil and gas", r"nuclear", r"energy storage", r"utilities"), (r"energy", r"power", r"solar", r"nuclear")),
    TagRule("climate", (r"climate tech", r"climate change", r"climate solutions", r"decarboni[sz]ation", r"carbon capture", r"carbon removal", r"cleantech", r"net[- ]zero")),
    TagRule("biotech", (r"biotech(?:nology)?", r"pharmaceuticals?", r"pharma", r"drug discovery", r"genomics", r"therapeutics", r"biologics", r"life sciences", r"crispr", r"assays?"), (r"bio\w*", r"therapeutics", r"pharma(?:ceuticals?)?")),
    TagRule("healthcare", (r"patients", r"patient care", r"clinical", r"clinicians?", r"hospitals?", r"medical devices?", r"medtech", r"telehealth", r"surgical", r"ehr"), (r"health", r"medical", r"hospital", r"clinic")),
    TagRule("fintech", (r"fintech", r"payments", r"payment processing", r"neobank", r"digital banking", r"lending platform", r"financial technology")),
    TagRule("finance", (r"investment banking", r"asset management", r"hedge funds?", r"private equity", r"capital markets", r"quantitative trading", r"market making", r"trading", r"securities", r"wealth management"), (r"bank", r"capital", r"financial", r"securities", r"investments?")),
    TagRule("insurance", (r"underwriting", r"actuarial", r"reinsurance", r"insurers?", r"policyholders?", r"claims adjust\w*"), (r"insurance", r"assurance", r"mutual")),
    TagRule("crypto", (r"crypto(?:currency|currencies)?", r"blockchain", r"web3", r"digital assets", r"defi", r"bitcoin", r"ethereum")),
    TagRule("security", (r"cybersecurity", r"cyber security", r"information security", r"infosec", r"threat detection", r"malware", r"penetration testing", r"zero trust", r"security operations"), (r"security", r"cyber")),
    TagRule("cloud", (r"saas", r"cloud computing", r"cloud infrastructure", r"cloud platform", r"cloud-native", r"iaas", r"paas")),
    TagRule("gaming", (r"video games?", r"gaming", r"game studio", r"game engine", r"esports", r"unreal engine"), (r"games?", r"gaming")),
    TagRule("media", (r"entertainment", r"media company", r"broadcast(?:ing)?", r"journalism", r"newsroom", r"publishing", r"television", r"streaming service", r"film studio"), (r"media", r"entertainment", r"studios?", r"pictures", r"news")),
    TagRule("ecommerce", (r"e-?commerce", r"online marketplace", r"online retail", r"marketplace platform")),
    TagRule("retail", (r"retail", r"merchandising", r"consumer goods", r"cpg", r"consumer packaged goods"), (r"stores?", r"retail", r"brands")),
    TagRule("telecom", (r"telecom(?:munications)?", r"wireless", r"5g", r"lte", r"network operator"), (r"telecom", r"wireless", r"communications")),
    TagRule("logistics", (r"logistics", r"supply chain", r"freight", r"shipping", r"warehous(?:e|es|ing)", r"fulfillment"), (r"logistics", r"freight", r"shipping")),
    TagRule("manufacturing", (r"manufacturing", r"factory", r"production line", r"machining", r"cnc", r"industrial automation", r"assembly line"), (r"manufacturing", r"industries", r"industrial")),
    TagRule("construction", (r"construction", r"hvac", r"building design", r"general contractor"), (r"construction", r"builders?")),
    TagRule("chemicals", (r"chemicals", r"specialty chemicals", r"chemical (?:company|plant|manufacturing)", r"polymers?", r"petrochemicals?"), (r"chemicals?", r"materials")),
    TagRule("consulting", (r"consulting", r"consultancy", r"advisory services", r"professional services"), (r"consulting", r"consultants?", r"partners", r"advisors?")),
    TagRule("government", (r"federal agency", r"government agency", r"public sector", r"municipal", r"state agency", r"city of", r"county of"), (r"department", r"agency", r"city of", r"county", r"state of")),
    TagRule("research", (r"national laborator(?:y|ies)", r"national lab", r"research institute", r"research lab(?:oratory)?", r"research center", r"principal investigator"), (r"university", r"institute", r"laborator(?:y|ies)", r"college")),
    TagRule("education", (r"edtech", r"k-12", r"curriculum", r"tutoring", r"learning platform", r"online learning"), (r"education", r"academy", r"school")),
    TagRule("nonprofit", (r"nonprofit", r"non-profit", r"501\(c\)", r"charitable"), (r"foundation", r"nonprofit")),
    TagRule("agriculture", (r"agriculture", r"agricultural", r"agtech", r"farming", r"crops?", r"agronomy"), (r"farms?", r"agri\w*")),
    TagRule("food", (r"food and beverage", r"food safety", r"restaurants?", r"beverages?", r"food production"), (r"foods?", r"beverages?", r"kitchen")),
    TagRule("travel", (r"airlines?", r"hospitality", r"hotels?", r"travel industry", r"cruise"), (r"airlines?", r"airways", r"hotels?", r"resorts?", r"travel")),
)

KNOWN_TAGS = frozenset(rule.tag for rule in RULES)

# Bump when classify_company's logic changes; RULES and the weights are
# fingerprinted directly. A changed fingerprint rebuilds tags on startup, so a
# rule edit shows up without waiting for the next refresh.
CLASSIFIER_VERSION = 3
RULES_FINGERPRINT = hashlib.sha256(
    repr((
        CLASSIFIER_VERSION, RULES, MAX_AUTO_TAGS, MIN_SCORE, NAME_WEIGHT, TITLE_WEIGHT,
        TITLE_CAP, DESCRIPTION_CAP, NICHE_MIN_POSTINGS, NICHE_MIN_SHARE,
    )).encode()
).hexdigest()[:16]


def _compile(parts: Iterable[str]) -> re.Pattern[str] | None:
    parts = tuple(parts)
    if not parts:
        return None
    # One capture group per keyword, so every match reports which keyword it
    # was: "fpga" and "fpgas" are one keyword, not two different ones.
    return re.compile(r"(?<!\w)(?:" + "|".join(f"({part})" for part in parts) + r")(?!\w)", re.IGNORECASE)


def _hits(pattern: re.Pattern[str] | None, text: str) -> dict[int, str]:
    """Keyword index -> the first text that keyword matched."""

    found: dict[int, str] = {}
    if pattern is None or not text:
        return found
    for match in pattern.finditer(text):
        found.setdefault(match.lastindex or 0, match.group(match.lastindex or 0).lower())
    return found


_COMPILED = tuple(
    (rule, _compile(rule.keywords), _compile((*rule.keywords, *rule.name_only)))
    for rule in RULES
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_tag(value: str) -> str:
    """One lowercase word; hyphens allowed inside. Raises ValueError otherwise."""

    tag = str(value or "").strip().lstrip("#").lower()
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError("A tag is one word: letters, numbers, or hyphens, up to 24 characters.")
    return tag


def classify_company(
    company: str,
    postings: Iterable[tuple[str, str]],
    *,
    labels: tuple[str, str] = ("job titles", "job descriptions"),
) -> list[dict[str, Any]]:
    """Score every rule against one company; return the tags that clear the bar.

    ``postings`` is (title, description) pairs. The name counts once, each
    title that matches counts (capped), and descriptions count the number of
    *different* keywords they contain across all postings (capped), never how
    often one keyword repeats. Spellings of one keyword ("fpga", "FPGAs")
    are the same keyword. A niche tag may instead qualify by recurring across
    postings (NICHE_MIN_POSTINGS, NICHE_MIN_SHARE); broad tags are capped at
    MAX_AUTO_TAGS, niche ones are kept alongside them.
    """

    postings = list(postings)
    found: list[dict[str, Any]] = []
    for rule, keywords, name_pattern in _COMPILED:
        name_hits = _hits(name_pattern, company or "")
        title_hits: dict[int, str] = {}
        title_score = 0
        description_hits: dict[int, str] = {}
        postings_with_hits = 0
        if keywords is not None:
            for title, description in postings:
                in_title = _hits(keywords, title or "")
                if in_title:
                    title_score += TITLE_WEIGHT
                    for index, word in in_title.items():
                        title_hits.setdefault(index, word)
                in_description = _hits(keywords, description or "")
                for index, word in in_description.items():
                    description_hits.setdefault(index, word)
                if in_title or in_description:
                    postings_with_hits += 1
        score = (
            (NAME_WEIGHT if name_hits else 0)
            + min(title_score, TITLE_CAP)
            + min(len(description_hits), DESCRIPTION_CAP)
        )
        recurring = (
            rule.niche
            and postings_with_hits >= NICHE_MIN_POSTINGS
            and postings_with_hits >= NICHE_MIN_SHARE * len(postings)
        )
        if recurring:
            score = max(score, MIN_SCORE)
        if score < MIN_SCORE:
            continue
        places = [
            label
            for label, hits in (("company name", name_hits), (labels[0], title_hits), (labels[1], description_hits))
            if hits
        ]
        words = list(dict.fromkeys([*name_hits.values(), *title_hits.values(), *description_hits.values()]))[:3]
        where = places[0] if len(places) == 1 else ", ".join(places[:-1]) + " and " + places[-1]
        evidence = f"Inferred from the {where}: " + ", ".join(f"“{word}”" for word in words)
        if recurring:
            evidence += f" (in {postings_with_hits} of {len(postings)} postings)"
        found.append({"tag": rule.tag, "score": score, "evidence": evidence + ".", "niche": rule.niche})
    found.sort(key=lambda item: (-item["score"], item["tag"]))
    broad = [item for item in found if not item["niche"]][:MAX_AUTO_TAGS]
    kept = {id(item) for item in broad}
    return [
        {key: value for key, value in item.items() if key != "niche"}
        for item in found
        if id(item) in kept or item["niche"]
    ]


def regenerate_company_tags(conn: sqlite3.Connection, company_keys: Iterable[str] | None = None) -> int:
    """Rebuild automatic tags from the stored postings. Returns rows written.

    With ``company_keys`` only those companies are rebuilt (a capture adds one
    company); otherwise every company is, and the rules fingerprint is
    recorded. The caller owns the transaction. Student choices are untouched.
    """

    keys = None if company_keys is None else sorted({str(key) for key in company_keys if key})
    if keys is not None and not keys:
        return 0
    sql = "SELECT company, company_sort_key, title, description FROM opportunities"
    params: list[Any] = []
    if keys is not None:
        sql += f" WHERE company_sort_key IN ({','.join('?' for _ in keys)})"
        params.extend(keys)
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"company": "", "postings": []})
    for row in conn.execute(sql, params).fetchall():
        key = str(row["company_sort_key"] or "")
        if not key:
            continue
        grouped[key]["company"] = grouped[key]["company"] or str(row["company"] or "")
        grouped[key]["postings"].append((str(row["title"] or ""), str(row["description"] or "")))

    timestamp = utc_now()
    rows = [
        (key, found["tag"], found["score"], found["evidence"], timestamp)
        for key, entry in grouped.items()
        for found in classify_company(entry["company"], entry["postings"])
    ]
    if keys is None:
        conn.execute("DELETE FROM company_tags")
    else:
        conn.execute(f"DELETE FROM company_tags WHERE company_key IN ({','.join('?' for _ in keys)})", keys)
    if rows:
        conn.executemany(
            "INSERT INTO company_tags(company_key, tag, score, evidence, generated_at) VALUES(?, ?, ?, ?, ?)",
            rows,
        )
    if keys is None:
        _regenerate_all_outreach_tags(conn)
        conn.execute(
            """
            INSERT INTO company_tag_rules(id, fingerprint, generated_at) VALUES(1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                fingerprint = excluded.fingerprint,
                generated_at = excluded.generated_at
            """,
            (RULES_FINGERPRINT, timestamp),
        )
    return len(rows)


def ensure_company_tags_current(conn: sqlite3.Connection) -> bool:
    """Rebuild every tag if the rules changed since the last full rebuild."""

    # A database migrated only part of the way (as some tests stage it) has
    # no tag tables yet; it is brought current once 0028 is applied.
    applied = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = '0028_outreach_company_tags.sql'"
    ).fetchone()
    if applied is None:
        return False
    row = conn.execute("SELECT fingerprint FROM company_tag_rules WHERE id = 1").fetchone()
    if row is not None and row["fingerprint"] == RULES_FINGERPRINT:
        return False
    with conn:
        regenerate_company_tags(conn)
    return True


def tag_key(company: str) -> str:
    """The key tags are stored under: the fold schema.sort_key stores."""

    return str(company or "").casefold()


def classify_outreach(company: str, summary: str, notes: str, *, unverified: bool) -> list[dict[str, Any]]:
    """Tags for an outreach company, from the student's research on it.

    The summary is a sentence or two about what the company does, with no
    boilerplate, so it counts like a job title: one keyword is enough. The
    evidence says when that research is unconfirmed deep-search output.
    """

    found = classify_company(company, [(summary, notes)], labels=("research summary", "research notes"))
    if unverified:
        for item in found:
            item["evidence"] += " The research is unverified deep-search output."
    return found


def _outreach_rows(conn: sqlite3.Connection, user_id: str) -> list[tuple[str, str, str, str]]:
    return [
        (str(row["company"] or ""), str(row["summary"] or ""), str(row["activity_signal"] or ""), str(row["research_confidence"] or ""))
        for row in conn.execute(
            "SELECT company, summary, activity_signal, research_confidence FROM outreach_targets WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()
    ]


def _outreach_signature(rows: list[tuple[str, str, str, str]]) -> str:
    return hashlib.sha256(repr((RULES_FINGERPRINT, rows)).encode()).hexdigest()[:24]


def _write_outreach_tags(conn: sqlite3.Connection, user_id: str, rows: list[tuple[str, str, str, str]]) -> None:
    """Replace one student's outreach tags. The caller owns the transaction."""

    timestamp = utc_now()
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for company, summary, notes, confidence in rows:
        key = tag_key(company)
        if not key:
            continue
        for item in classify_outreach(company, summary, notes, unverified=confidence == "unverified"):
            current = best.get((key, item["tag"]))
            if current is None or item["score"] > current["score"]:
                best[(key, item["tag"])] = item
    conn.execute("DELETE FROM outreach_company_tags WHERE user_id=?", (user_id,))
    if best:
        conn.executemany(
            """
            INSERT INTO outreach_company_tags(user_id, company_key, tag, score, evidence, generated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            [(user_id, key, tag, item["score"], item["evidence"], timestamp) for (key, tag), item in best.items()],
        )
    conn.execute(
        """
        INSERT INTO outreach_tag_state(user_id, signature, generated_at) VALUES(?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            signature = excluded.signature,
            generated_at = excluded.generated_at
        """,
        (user_id, _outreach_signature(rows), timestamp),
    )


def _regenerate_all_outreach_tags(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM outreach_company_tags")
    conn.execute("DELETE FROM outreach_tag_state")
    for row in conn.execute("SELECT DISTINCT user_id FROM outreach_targets").fetchall():
        user_id = str(row["user_id"])
        _write_outreach_tags(conn, user_id, _outreach_rows(conn, user_id))


def sync_outreach_tags(conn: sqlite3.Connection, user_id: str) -> bool:
    """Bring one student's outreach tags up to date with their outreach list.

    Companies are added, imported, found by deep search, renamed and edited
    through many paths; rather than hook each, the tags are rebuilt whenever
    the tag-relevant columns of the list differ from the last rebuild. The
    comparison reads a few short columns, so an unchanged list costs little.
    """

    rows = _outreach_rows(conn, user_id)
    state = conn.execute("SELECT signature FROM outreach_tag_state WHERE user_id=?", (user_id,)).fetchone()
    if state is not None and state["signature"] == _outreach_signature(rows):
        return False
    with conn:
        _write_outreach_tags(conn, user_id, rows)
    return True


def decorate_outreach_with_tags(
    conn: sqlite3.Connection, items: list[dict[str, Any]], *, user_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Outreach items with their tags, and each tag's company count among them."""

    keyed = [{**item, "company_sort_key": tag_key(item.get("company") or "")} for item in items]
    tags = tags_for_companies(conn, (item["company_sort_key"] for item in keyed), user_id=user_id)
    decorated = [{**item, "tags": tags.get(item["company_sort_key"], [])} for item in keyed]
    counts: dict[str, int] = defaultdict(int)
    for item in decorated:
        for tag in item["tags"]:
            counts[tag["tag"]] += 1
    return decorated, [{"tag": tag, "companies": count} for tag, count in sorted(counts.items())]


def _chunks(values: list[str], size: int = 500) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def tags_for_companies(
    conn: sqlite3.Connection, company_keys: Iterable[str], *, user_id: str
) -> dict[str, list[dict[str, Any]]]:
    """The tags one student sees on each company: automatic minus removed, plus added."""

    keys = sorted({str(key) for key in company_keys if key})
    result: dict[str, list[dict[str, Any]]] = {key: [] for key in keys}
    for chunk in _chunks(keys):
        placeholders = ",".join("?" for _ in chunk)
        choices = {
            (row["company_key"], row["tag"]): row["choice"]
            for row in conn.execute(
                f"SELECT company_key, tag, choice FROM company_tag_choices WHERE user_id=? AND company_key IN ({placeholders})",
                [user_id, *chunk],
            ).fetchall()
        }
        # Posting-derived tags first, then ones from this student's own
        # outreach research; a tag both produce shows once, with the posting's
        # evidence.
        generated = conn.execute(
            f"""
            SELECT company_key, tag, evidence, score, 0 AS private FROM company_tags
            WHERE company_key IN ({placeholders})
            UNION ALL
            SELECT company_key, tag, evidence, score, 1 AS private FROM outreach_company_tags
            WHERE user_id = ? AND company_key IN ({placeholders})
            """,
            [*chunk, user_id, *chunk],
        ).fetchall()
        for row in sorted(generated, key=lambda row: (row["private"], -int(row["score"]), row["tag"])):
            if choices.get((row["company_key"], row["tag"])) == "removed":
                continue
            if any(item["tag"] == row["tag"] for item in result[row["company_key"]]):
                continue
            result[row["company_key"]].append({"tag": row["tag"], "origin": "auto", "evidence": row["evidence"]})
        for (key, tag), choice in sorted(choices.items()):
            if choice == "added" and not any(item["tag"] == tag for item in result[key]):
                result[key].append({"tag": tag, "origin": "manual", "evidence": "Added by you."})
    return result


def decorate_with_tags(
    conn: sqlite3.Connection, items: list[dict[str, Any]], *, user_id: str
) -> list[dict[str, Any]]:
    tags = tags_for_companies(conn, (item.get("company_sort_key") or "" for item in items), user_id=user_id)
    return [{**item, "tags": tags.get(item.get("company_sort_key") or "", [])} for item in items]


def tag_facets(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    """Every tag on a company with an active, visible posting, with company counts."""

    visible = capture_visible_sql("o")
    rows = conn.execute(
        f"""
        SELECT DISTINCT o.company_sort_key AS company_key
        FROM opportunity_read_model o
        WHERE o.active = 1 AND o.duplicate_of IS NULL AND {visible}
        """,
        [user_id],
    ).fetchall()
    counts: dict[str, int] = defaultdict(int)
    for tags in tags_for_companies(conn, (row["company_key"] for row in rows), user_id=user_id).values():
        for item in tags:
            counts[item["tag"]] += 1
    return [{"tag": tag, "companies": count} for tag, count in sorted(counts.items())]


class CompanyNotFoundError(LookupError):
    pass


def _company_key(conn: sqlite3.Connection, company: str, *, user_id: str) -> str:
    """The key of a company this student can see: a posting or their own outreach."""

    key = tag_key(company)
    if not key.strip():
        raise CompanyNotFoundError("Company not found")
    visible = capture_visible_sql("o")
    row = conn.execute(
        f"SELECT 1 FROM opportunities o WHERE o.company_sort_key = ? AND {visible} LIMIT 1",
        [key, user_id],
    ).fetchone()
    if row is None and not any(tag_key(name) == key for name, *_ in _outreach_rows(conn, user_id)):
        raise CompanyNotFoundError("Company not found")
    return key


def _is_auto(conn: sqlite3.Connection, key: str, tag: str, *, user_id: str) -> bool:
    return conn.execute(
        """
        SELECT 1 FROM company_tags WHERE company_key=? AND tag=?
        UNION ALL
        SELECT 1 FROM outreach_company_tags WHERE user_id=? AND company_key=? AND tag=?
        """,
        (key, tag, user_id, key, tag),
    ).fetchone() is not None


def set_company_tag(
    conn: sqlite3.Connection, company: str, tag: str, *, user_id: str, present: bool
) -> dict[str, Any]:
    """Add (``present=True``) or remove one tag on one company for one student."""

    tag = normalize_tag(tag)
    key = _company_key(conn, company, user_id=user_id)
    sync_outreach_tags(conn, user_id)
    auto = _is_auto(conn, key, tag, user_id=user_id)
    with conn:
        if present == auto:
            # Adding an automatic tag back, or removing a tag that exists
            # only as the student's own addition: no choice needs recording.
            conn.execute(
                "DELETE FROM company_tag_choices WHERE user_id=? AND company_key=? AND tag=?",
                (user_id, key, tag),
            )
        else:
            conn.execute(
                """
                INSERT INTO company_tag_choices(user_id, company_key, tag, choice, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(user_id, company_key, tag) DO UPDATE SET
                    choice = excluded.choice,
                    updated_at = excluded.updated_at
                """,
                (user_id, key, tag, "added" if present else "removed", utc_now()),
            )
    return {"company_key": key, "tags": tags_for_companies(conn, [key], user_id=user_id)[key]}
