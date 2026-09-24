"""One-word industry tags for each company, generated from its own postings.

A tag is a classification, not a fact the employer stated, so every automatic
tag carries the evidence it was inferred from and the UI marks it as inferred.
Generation is deterministic keyword matching: free, inspectable, and the same
on every run.

Tags are keyed by the company's stored fold (``company_sort_key``), so every
posting from one company shares them. Automatic tags live in ``company_tags``
and are rebuilt on every sync. A student's own edits live in
``company_tag_choices`` and outlive every rebuild: removing an automatic tag
records a 'removed' choice, so the next sync cannot bring it back.
"""

from __future__ import annotations

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

TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


@dataclass(frozen=True)
class TagRule:
    tag: str
    # Matched in the company name, titles, and descriptions.
    keywords: tuple[str, ...]
    # Matched only in the company name: words too common in postings to mean
    # anything there ("bank holiday", "health insurance").
    name_only: tuple[str, ...] = ()


# Keywords are regex fragments matched on word boundaries, case-insensitively.
# Deliberately absent: words every benefits or EEO paragraph contains
# ("insurance", "healthcare", "military", "veteran"), and generic stack words
# ("software", "data", "AWS") that would tag every company alike.
RULES: tuple[TagRule, ...] = (
    TagRule("robotics", (r"robot(?:s|ic|ics)?", r"autonomous (?:systems?|robots?|vehicles?)", r"humanoid", r"manipulators?", r"drones?", r"uavs?")),
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


def _compile(parts: Iterable[str]) -> re.Pattern[str] | None:
    parts = tuple(parts)
    if not parts:
        return None
    return re.compile(r"(?<!\w)(" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


_COMPILED = tuple(
    (rule.tag, _compile(rule.keywords), _compile((*rule.keywords, *rule.name_only)))
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


def classify_company(company: str, postings: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
    """Score every rule against one company; return the tags that clear the bar.

    ``postings`` is (title, description) pairs. The name counts once, each
    title that matches counts (capped), and descriptions count the number of
    *different* keywords they contain across all postings (capped), never how
    often one keyword repeats.
    """

    postings = list(postings)
    found: list[dict[str, Any]] = []
    for tag, keywords, name_pattern in _COMPILED:
        name_hits = {hit.lower() for hit in name_pattern.findall(company or "")} if name_pattern else set()
        title_hits: list[str] = []
        title_score = 0
        description_hits: set[str] = set()
        if keywords is not None:
            for title, description in postings:
                in_title = {hit.lower() for hit in keywords.findall(title or "")}
                if in_title:
                    title_score += TITLE_WEIGHT
                    title_hits.extend(sorted(in_title))
                description_hits.update(hit.lower() for hit in keywords.findall(description or ""))
        score = (
            (NAME_WEIGHT if name_hits else 0)
            + min(title_score, TITLE_CAP)
            + min(len(description_hits), DESCRIPTION_CAP)
        )
        if score < MIN_SCORE:
            continue
        places = [
            label
            for label, hits in (("company name", name_hits), ("job titles", title_hits), ("job descriptions", description_hits))
            if hits
        ]
        words = list(dict.fromkeys([*sorted(name_hits), *title_hits, *sorted(description_hits)]))[:3]
        where = places[0] if len(places) == 1 else ", ".join(places[:-1]) + " and " + places[-1]
        evidence = f"Inferred from the {where}: " + ", ".join(f"“{word}”" for word in words) + "."
        found.append({"tag": tag, "score": score, "evidence": evidence})
    found.sort(key=lambda item: (-item["score"], item["tag"]))
    return found[:MAX_AUTO_TAGS]


def regenerate_company_tags(conn: sqlite3.Connection, company_keys: Iterable[str] | None = None) -> int:
    """Rebuild automatic tags from the stored postings. Returns rows written.

    With ``company_keys`` only those companies are rebuilt (a capture adds one
    company); otherwise every company is. The caller owns the transaction.
    Student choices are untouched.
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
    return len(rows)


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
        for row in conn.execute(
            f"SELECT company_key, tag, evidence FROM company_tags WHERE company_key IN ({placeholders}) ORDER BY score DESC, tag",
            chunk,
        ).fetchall():
            if choices.get((row["company_key"], row["tag"])) == "removed":
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
    # The same fold schema.sort_key stores, applied to the name as displayed.
    key = str(company or "").casefold()
    if not key.strip():
        raise CompanyNotFoundError("Company not found")
    visible = capture_visible_sql("o")
    row = conn.execute(
        f"SELECT 1 FROM opportunities o WHERE o.company_sort_key = ? AND {visible} LIMIT 1",
        [key, user_id],
    ).fetchone()
    if row is None:
        raise CompanyNotFoundError("Company not found")
    return key


def _is_auto(conn: sqlite3.Connection, key: str, tag: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM company_tags WHERE company_key=? AND tag=?", (key, tag)
    ).fetchone() is not None


def set_company_tag(
    conn: sqlite3.Connection, company: str, tag: str, *, user_id: str, present: bool
) -> dict[str, Any]:
    """Add (``present=True``) or remove one tag on one company for one student."""

    tag = normalize_tag(tag)
    key = _company_key(conn, company, user_id=user_id)
    auto = _is_auto(conn, key, tag)
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
