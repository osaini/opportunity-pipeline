"""Phase 2 performance work on pipeline.py must not change what the pipeline stores.

Each test here pairs a faster implementation with a verbatim copy of the one it
replaced (the `_reference_*` functions below, taken from the commit before the
change) and runs both on the same generated input, asserting identical output.
The copies are test code on purpose: they are the oracle, so they are not to be
"tidied" to match the production functions.

What changed, and what is compared:

* fingerprint_text: a column-count SimHash replaced the per-bit loop.
* deduplicate: pass 3 precomputes per-row values and tests the cheap condition
  first; the blanket `UPDATE ... duplicate_of=NULL` became a diff write.
* upsert_jobs(dedupe=False) + one deduplicate at the end of import_discovered,
  instead of one per channel. fetch_all still deduplicates inside each source's
  upsert, so a failing pass rolls that source back. `_reference_upsert_jobs` is
  the whole old upsert, so the "old" side of these comparisons shares no changed code.
* upsert_jobs reuses a stored content_fingerprint when the description is
  unchanged.
* score_all writes only rows whose result changed, with executemany.
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
import random
import sqlite3
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

import pipeline

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

FIXTURES = Path(__file__).parent / "fixtures"

WORDS = (
    "design build test engineer system software hardware robotics sensor firmware python embedded "
    "mechanical electrical thermal fluid control signal power battery manufacturing vehicle data "
    "analysis modeling simulation prototype validation requirement team intern student summer "
    "project report review safety quality supply process materials cad matlab linux"
).split()
LOCATIONS = [
    "Austin, TX",
    "Austin, Texas, United States",
    "San Francisco, CA",
    "Austin, Texas, United States; South San Francisco, California, United States",
    "Boston, MA",
    "",
    "Remote",
    "Seattle, WA",
]


# --- reference implementations: the code as it was before Phase 2 -----------------------------


@functools.lru_cache(maxsize=None)
def _reference_fingerprint_text(text):
    normalised = pipeline.normalize_jd_text(text)
    if len(normalised) < pipeline.FINGERPRINT_MIN_TEXT:
        return ""
    tokens = normalised.split(" ")
    if len(tokens) < 3:
        return ""
    weights = [0] * 64
    for index in range(len(tokens) - 2):
        shingle = " ".join(tokens[index : index + 3])
        digest = int(hashlib.sha256(shingle.encode("utf-8")).hexdigest()[:16], 16)
        for bit in range(64):
            weights[bit] += 1 if (digest >> bit) & 1 else -1
    value = 0
    for bit in range(64):
        if weights[bit] > 0:
            value |= 1 << bit
    return f"{value:016x}"


def _reference_deduplicate(conn):
    conn.execute("UPDATE jobs SET duplicate_of=NULL")
    rows = conn.execute(
        """
        SELECT id, fingerprint, content_fingerprint, status, source_key, description,
               company, title, location
        FROM jobs
        WHERE active=1
        ORDER BY fingerprint, id
        """
    ).fetchall()

    groups = {}
    for row in rows:
        groups.setdefault(row["fingerprint"], []).append(row)
    resolved = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        canonical = pipeline._canonical_of(group)["id"]
        for row in group:
            if row["id"] != canonical:
                resolved[row["id"]] = canonical

    by_role = {}
    for row in rows:
        if row["id"] in resolved:
            continue
        by_role.setdefault(
            (pipeline.normalized(row["company"]), pipeline.normalized(row["title"])), []
        ).append(row)
    for group in by_role.values():
        if len(group) < 2:
            continue
        remaining = group
        while len(remaining) > 1:
            canonical_row = pipeline._canonical_of(remaining)
            cluster = [
                row
                for row in remaining
                if row["id"] != canonical_row["id"]
                and pipeline.locations_compatible(row["location"], canonical_row["location"])
            ]
            for row in cluster:
                resolved[row["id"]] = canonical_row["id"]
            assigned = {canonical_row["id"], *(row["id"] for row in cluster)}
            remaining = [row for row in remaining if row["id"] not in assigned]

    candidates = [
        row for row in rows if row["id"] not in resolved and row["content_fingerprint"]
    ]
    clustered = set()
    for index, row in enumerate(candidates):
        if row["id"] in clustered:
            continue
        cluster = [row]
        for other in candidates[index + 1 :]:
            if other["id"] in clustered or other["source_key"] == row["source_key"]:
                continue
            if not pipeline.locations_compatible(row["location"], other["location"]):
                continue
            similarity = pipeline.fingerprint_similarity(
                row["content_fingerprint"], other["content_fingerprint"]
            )
            if similarity >= pipeline.CROSSLIST_THRESHOLD:
                cluster.append(other)
                clustered.add(other["id"])
        if len(cluster) > 1:
            clustered.add(row["id"])
            canonical = pipeline._canonical_of(cluster)["id"]
            for member in cluster:
                if member["id"] != canonical:
                    resolved[member["id"]] = canonical

    for duplicate, canonical in resolved.items():
        conn.execute("UPDATE jobs SET duplicate_of=? WHERE id=?", (canonical, duplicate))


def _reference_upsert_jobs(conn, source_key, source_name, records, seen=None):
    """upsert_jobs before Phase 2: always the per-bit SimHash, a deduplicate after every call, nothing reused.

    Calls only the _reference_* copies above for the two functions that changed, and the unchanged pipeline helpers.
    """
    seen = seen or pipeline.now_iso()
    ids = []
    for record in records:
        url = pipeline.canonical_url(record["url"])
        external_id = record["external_id"]
        job_id = pipeline.stable_id(source_key, external_id)
        ids.append(job_id)
        description = record["description"]
        location = record["location"]
        existing = conn.execute(
            "SELECT description, location FROM jobs WHERE source_key=? AND external_id=?",
            (source_key, external_id),
        ).fetchone()
        if existing:
            description = pipeline._richer_description(existing["description"], description)
            location = location or existing["location"]
        role_type = pipeline.classify_role(record["title"], description)
        fp = pipeline.fingerprint(record["company"], record["title"], location)
        content_fp = _reference_fingerprint_text(description)
        conn.execute(
            """
            INSERT INTO jobs (
                id, source_key, source_name, external_id, company, title, location,
                role_type, url, description, posted_at, first_seen_at, last_seen_at,
                active, fingerprint, content_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(source_key, external_id) DO UPDATE SET
                source_name=excluded.source_name,
                company=excluded.company,
                title=excluded.title,
                location=excluded.location,
                role_type=excluded.role_type,
                url=excluded.url,
                description=excluded.description,
                posted_at=COALESCE(excluded.posted_at, jobs.posted_at),
                last_seen_at=excluded.last_seen_at,
                active=1,
                fingerprint=excluded.fingerprint,
                content_fingerprint=excluded.content_fingerprint
            """,
            (
                job_id, source_key, source_name, external_id, record["company"], record["title"], location,
                role_type, url, description, record.get("posted_at"), seen, seen, fp, content_fp,
            ),
        )
    pipeline._retire_absent(conn, source_key, records, set(ids), seen)
    _reference_deduplicate(conn)
    return len(records)


def _reference_score_all(conn, profile):
    jobs = conn.execute("SELECT * FROM jobs").fetchall()
    reposts = pipeline.repost_flags(conn)
    for job in jobs:
        role_type = pipeline.classify_role(job["title"], job["description"])
        score_input = dict(job)
        score_input["role_type"] = role_type
        score, reasons = pipeline.score_job(score_input, profile)
        if job["id"] in reposts:
            listings, since = reposts[job["id"]]
            reasons.append(
                f"FLAG: this role has been listed under {listings} different URLs "
                f"since {since}—may be an evergreen or re-listed req"
            )
        conn.execute(
            "UPDATE jobs SET role_type=?, score=?, score_explanation=? WHERE id=?",
            (role_type, score, json.dumps(reasons), job["id"]),
        )
    conn.commit()
    return len(jobs)


# --- generated input -------------------------------------------------------------------------


def body(rng, words):
    return " ".join(rng.choice(WORDS) for _ in range(words)) + "."


def perturbed(rng, text, changes):
    tokens = text.split()
    for _ in range(changes):
        tokens[rng.randrange(len(tokens))] = "changedword"
    return " ".join(tokens)


def generate_rows(seed, count=120, sources=6):
    """Row dicts with planted duplicates of all three kinds.

    Pass 1: same company/title/location. Pass 2: same company/title, differently
    formatted location. Pass 3: a near-verbatim body under another company and
    title (0-2 words changed, so some pairs sit on each side of the threshold).
    Statuses, inactive rows, short bodies and blank locations are mixed in.
    """
    rng = random.Random(seed)
    keys = [f"greenhouse:s{i}" for i in range(sources)] + ["manual:csv", "agent:linkedin"]
    rows = []
    for index in range(count):
        description = body(rng, rng.choice([8, 70, 90, 150]))
        rows.append(
            {
                "source_key": rng.choice(keys),
                "company": f"Company {index % 40}",
                "title": f"Engineering Intern {index % 25}",
                "location": rng.choice(LOCATIONS),
                "description": description,
            }
        )
    for _ in range(count // 5):  # pass 1 and 2 copies
        base = rng.choice(rows)
        copy = dict(base, source_key=rng.choice(keys))
        if rng.random() < 0.5:
            copy["location"] = rng.choice(LOCATIONS)
        rows.append(copy)
    for _ in range(count // 4):  # pass 3 copies under another company and title
        base = rng.choice(rows)
        if len(base["description"].split()) < 60:
            continue
        rows.append(
            {
                "source_key": rng.choice(keys),
                "company": f"Restyled {rng.randrange(1000)}",
                "title": f"Role {rng.randrange(1000)}",
                "location": base["location"] if rng.random() < 0.7 else rng.choice(LOCATIONS),
                "description": perturbed(rng, base["description"], rng.choice([0, 1, 2, 6])),
            }
        )
    return rows


def populate(conn, rows, seed):
    """Insert rows with a mix of statuses, inactive rows and stale duplicate_of values."""
    rng = random.Random(seed)
    ids = []
    for index, row in enumerate(rows):
        job_id = f"job{index:04d}"
        ids.append(job_id)
        conn.execute(
            """
            INSERT INTO jobs (
                id, source_key, source_name, external_id, company, title, location, role_type, url,
                description, first_seen_at, last_seen_at, active, fingerprint, content_fingerprint, status
            ) VALUES (?, ?, 'src', ?, ?, ?, ?, 'internship', ?, ?, '2026-01-01', '2026-01-01', ?, ?, ?, ?)
            """,
            (
                job_id,
                row["source_key"],
                f"ext{index}",
                row["company"],
                row["title"],
                row["location"],
                f"https://example.com/{index}",
                row["description"],
                0 if rng.random() < 0.1 else 1,
                pipeline.fingerprint(row["company"], row["title"], row["location"]),
                _reference_fingerprint_text(row["description"]),
                rng.choice(["discovered", "discovered", "saved", "applied", "rejected"]),
            ),
        )
    # Stale links the next pass has to keep, move or clear: some on rows that
    # will resolve to a different canonical, some on rows that resolve to none,
    # some on inactive rows.
    for job_id in rng.sample(ids, len(ids) // 4):
        conn.execute("UPDATE jobs SET duplicate_of=? WHERE id=?", (rng.choice(ids), job_id))
    conn.commit()


def links(conn):
    return dict(conn.execute("SELECT id, duplicate_of FROM jobs ORDER BY id").fetchall())


class TempDbCase(unittest.TestCase):
    def open_db(self, name="pipeline.db"):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with unittest.mock.patch.object(pipeline, "DB_PATH", Path(temp.name) / name):
            conn = pipeline.connect()
        self.addCleanup(conn.close)
        return conn


class FingerprintTextParityTests(unittest.TestCase):
    def test_matches_the_per_bit_loop_on_generated_texts(self):
        rng = random.Random(11)
        checked = 0
        for _ in range(300):
            text = body(rng, rng.randint(25, 900))
            self.assertEqual(
                pipeline.fingerprint_text(text), _reference_fingerprint_text(text), text[:80]
            )
            checked += 1
        self.assertEqual(checked, 300)

    def test_matches_when_votes_tie(self):
        # Two tokens repeated give shingle counts that split evenly between bits, which the
        # `weights > 0` rule leaves unset. Sweep lengths so both parities of the shingle count occur.
        for repeats in range(40, 60):
            text = " ".join(["alpha", "beta"] * repeats)
            self.assertEqual(
                pipeline.fingerprint_text(text), _reference_fingerprint_text(text), repeats
            )

    def test_unusable_texts_are_still_blank(self):
        cases = [None, "", "too short", "x" * 400, "界" * 300, "<p>" + "a " * 10 + "</p>"]
        for text in cases:
            self.assertEqual(pipeline.fingerprint_text(text), _reference_fingerprint_text(text))
            self.assertEqual(pipeline.fingerprint_text(text), "")


class DeduplicateParityTests(TempDbCase):
    def test_pass_three_and_the_diff_write_match_the_original(self):
        planted_links = 0
        for seed in range(4):
            rows = generate_rows(seed)
            old, new = self.open_db("old.db"), self.open_db("new.db")
            populate(old, rows, seed)
            populate(new, rows, seed)
            self.assertEqual(links(old), links(new))
            _reference_deduplicate(old)
            pipeline.deduplicate(new)
            self.assertEqual(links(old), links(new), f"seed {seed}")
            planted_links += sum(1 for target in links(new).values() if target)
        self.assertGreater(planted_links, 60, "the generated rows must actually contain duplicates")

    def test_pass_three_alone_finds_the_planted_cross_listings(self):
        # Distinct company, title and fingerprint, so only the body can link them.
        old, new = self.open_db("old.db"), self.open_db("new.db")
        rng = random.Random(3)
        rows = []
        for index in range(30):
            text = body(rng, 120)
            rows.append(
                {"source_key": "greenhouse:a", "company": f"A{index}", "title": f"T{index}",
                 "location": "Austin, TX", "description": text}
            )
            rows.append(
                {"source_key": "agent:linkedin", "company": f"B{index}", "title": f"U{index}",
                 "location": "Austin, Texas, United States", "description": perturbed(rng, text, index % 3)}
            )
        populate(old, rows, 0)
        populate(new, rows, 0)
        _reference_deduplicate(old)
        pipeline.deduplicate(new)
        self.assertEqual(links(old), links(new))
        self.assertGreaterEqual(sum(1 for target in links(new).values() if target), 10)

    def test_a_second_pass_writes_nothing(self):
        conn = self.open_db()
        populate(conn, generate_rows(2), 2)
        pipeline.deduplicate(conn)
        conn.commit()
        before = conn.total_changes
        snapshot = links(conn)
        pipeline.deduplicate(conn)
        self.assertEqual(conn.total_changes, before, "an unchanged table must not be rewritten")
        self.assertEqual(links(conn), snapshot)

    def test_a_stale_link_on_an_inactive_row_is_cleared(self):
        conn = self.open_db()
        populate(conn, generate_rows(4, count=20), 4)
        conn.execute("UPDATE jobs SET active=0, duplicate_of='job0001' WHERE id='job0000'")
        pipeline.deduplicate(conn)
        self.assertIsNone(conn.execute("SELECT duplicate_of FROM jobs WHERE id='job0000'").fetchone()[0])


def posting(external_id, company, title, location, description):
    return {
        "external_id": external_id,
        "company": company,
        "title": title,
        "location": location,
        "url": f"https://example.com/{external_id}",
        "description": description,
        "posted_at": None,
    }


def per_source_postings(seed, sources=8):
    """{source_key: [posting]} with the same body listed under different sources."""
    rng = random.Random(seed)
    batches = {f"greenhouse:s{i}": [] for i in range(sources)}
    keys = list(batches)
    texts = [body(rng, 120) for _ in range(25)]
    for index in range(80):
        key = rng.choice(keys)
        text = rng.choice(texts) if rng.random() < 0.5 else body(rng, 120)
        text = perturbed(rng, text, rng.choice([0, 1, 5]))
        batches[key].append(
            posting(
                f"e{index}",
                f"Company {rng.randrange(30)}",
                f"Engineering Intern {rng.randrange(20)}",
                rng.choice(LOCATIONS),
                text,
            )
        )
    return batches


def state_digest(conn):
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT id, source_key, external_id, company, title, location, role_type, description, "
            "active, fingerprint, content_fingerprint, duplicate_of FROM jobs ORDER BY id"
        )
    ]


class DeferredDedupeTests(TempDbCase):
    def run_fetch(self, conn, batches):
        config = {
            "discovery_title_terms": ["intern"],
            "ats_sources": [
                {"kind": "greenhouse", "company": key.split(":")[1], "token": key.split(":")[1]}
                for key in batches
            ],
        }

        def fetcher(source, _terms):
            return list(batches[f"greenhouse:{source['token']}"])

        with unittest.mock.patch.dict(pipeline._SOURCE_FETCHERS, {"greenhouse": fetcher}), \
                unittest.mock.patch.object(pipeline, "_HOST_LIMITER", pipeline._HostRateLimiter(0.0)), \
                unittest.mock.patch("sys.stdout", io.StringIO()), \
                unittest.mock.patch("sys.stderr", io.StringIO()):
            return pipeline.fetch_all(conn, config, max_workers=1, max_per_host=1)

    def test_fetch_all_leaves_the_links_a_per_source_dedupe_would(self):
        total_links = 0
        for seed in range(3):
            batches = per_source_postings(seed)
            old, new = self.open_db("old.db"), self.open_db("new.db")
            # The old behaviour: upsert one source at a time, deduplicating after each.
            for key, records in batches.items():
                _reference_upsert_jobs(old, key, key, records, "2026-09-01T00:00:00+00:00")
                old.commit()
            self.run_fetch(new, batches)
            # fetch_all stamps first/last seen with the cycle clock; compare what dedupe sees.
            self.assertEqual(links(old), links(new), f"seed {seed}")
            self.assertEqual(
                [row[:-1] for row in state_digest(old)], [row[:-1] for row in state_digest(new)]
            )
            total_links += sum(1 for target in links(new).values() if target)
        self.assertGreater(total_links, 10, "the generated postings must actually contain duplicates")

    def test_a_rerun_with_changed_postings_still_matches(self):
        batches = per_source_postings(9)
        old, new = self.open_db("old.db"), self.open_db("new.db")
        for conn in (old, new):
            for key, records in batches.items():
                _reference_upsert_jobs(conn, key, key, records, "2026-09-01T00:00:00+00:00")
        rng = random.Random(5)
        for records in batches.values():  # drop some postings, edit some bodies
            del records[: len(records) // 4]
            for record in records[: len(records) // 3]:
                record["description"] = perturbed(rng, record["description"], 3)
        for key, records in batches.items():
            _reference_upsert_jobs(old, key, key, records, "2026-09-02T00:00:00+00:00")
            old.commit()
        self.run_fetch(new, batches)
        self.assertEqual(links(old), links(new))

    def test_fetch_all_deduplicates_inside_each_source(self):
        batches = per_source_postings(1)
        conn = self.open_db()
        with unittest.mock.patch.object(pipeline, "deduplicate", wraps=pipeline.deduplicate) as spy:
            self.run_fetch(conn, batches)
        # One pass per source, inside that source's upsert, so a failing pass
        # rolls that source back.
        self.assertEqual(spy.call_count, len(batches))

    def test_dedupe_false_defers_and_the_default_still_links(self):
        batches = per_source_postings(1)
        deferred, eager = self.open_db("deferred.db"), self.open_db("eager.db")
        reference = self.open_db("reference.db")
        for key, records in batches.items():
            pipeline.upsert_jobs(deferred, key, key, records, "2026-09-01T00:00:00+00:00", dedupe=False)
            pipeline.upsert_jobs(eager, key, key, records, "2026-09-01T00:00:00+00:00")
            _reference_upsert_jobs(reference, key, key, records, "2026-09-01T00:00:00+00:00")
        self.assertFalse(any(links(deferred).values()))
        self.assertTrue(any(links(eager).values()))
        pipeline.deduplicate(deferred)
        self.assertEqual(links(deferred), links(eager))
        self.assertEqual(links(reference), links(eager))
        self.assertEqual(state_digest(reference), state_digest(eager))

    def test_a_fetch_where_every_source_fails_does_not_touch_links(self):
        conn = self.open_db()
        populate(conn, generate_rows(7, count=20), 7)
        before = links(conn)

        def failing(_source, _terms):
            raise RuntimeError("HTTP Error 404")

        config = {
            "discovery_title_terms": ["intern"],
            "ats_sources": [{"kind": "greenhouse", "company": "G0", "token": "g0"}],
        }
        with unittest.mock.patch.dict(pipeline._SOURCE_FETCHERS, {"greenhouse": failing}), \
                unittest.mock.patch.object(pipeline, "_HOST_LIMITER", pipeline._HostRateLimiter(0.0)), \
                unittest.mock.patch("sys.stdout", io.StringIO()), \
                unittest.mock.patch("sys.stderr", io.StringIO()):
            pipeline.fetch_all(conn, config, max_workers=1, max_per_host=1)
        self.assertEqual(links(conn), before)

    def run_fetch_ex(self, conn, batches, *, fetch_fails=(), resume_since=None):
        """Like run_fetch, but returns (result, stderr) and can fail chosen sources' fetches."""
        config = {
            "discovery_title_terms": ["intern"],
            "ats_sources": [
                {"kind": "greenhouse", "company": key.split(":")[1], "token": key.split(":")[1]}
                for key in batches
            ],
        }

        def fetcher(source, _terms):
            key = f"greenhouse:{source['token']}"
            if key in fetch_fails:
                raise RuntimeError("HTTP Error 404")
            return list(batches[key])

        stderr = io.StringIO()
        with unittest.mock.patch.dict(pipeline._SOURCE_FETCHERS, {"greenhouse": fetcher}),                 unittest.mock.patch.object(pipeline, "_HOST_LIMITER", pipeline._HostRateLimiter(0.0)),                 unittest.mock.patch("sys.stdout", io.StringIO()),                 unittest.mock.patch("sys.stderr", stderr):
            result = pipeline.fetch_all(
                conn, config, resume_since=resume_since, max_workers=1, max_per_host=1
            )
        return result, stderr.getvalue()

    def test_failing_sources_mid_run_leave_what_a_per_source_dedupe_would(self):
        """One source's fetch fails, another's upsert fails after writing rows (rolled back)."""
        batches = per_source_postings(4)
        keys = list(batches)
        fetch_fail, upsert_fail = keys[2], keys[5]
        old, new = self.open_db("old.db"), self.open_db("new.db")
        for key, records in batches.items():  # the old behaviour: a failed source stores nothing
            if key not in (fetch_fail, upsert_fail):
                _reference_upsert_jobs(old, key, key, records, "2026-09-01T00:00:00+00:00")
                old.commit()
        real = pipeline.upsert_jobs

        def failing_upsert(conn, source_key, *args, **kwargs):
            written = real(conn, source_key, *args, **kwargs)
            if source_key == upsert_fail:
                raise RuntimeError("boom after writing")
            return written

        with unittest.mock.patch.object(pipeline, "upsert_jobs", failing_upsert):
            _result, stderr = self.run_fetch_ex(new, batches, fetch_fails=(fetch_fail,))
        self.assertIn("boom after writing", stderr)
        outcomes = dict(new.execute("SELECT source_key, outcome FROM fetch_runs").fetchall())
        self.assertEqual(outcomes[fetch_fail], "error")
        self.assertEqual(outcomes[upsert_fail], "error")
        self.assertEqual(
            [row[:-1] for row in state_digest(old)], [row[:-1] for row in state_digest(new)]
        )
        self.assertEqual(links(old), links(new))

    def test_a_failing_link_pass_rolls_the_source_back_and_records_an_error(self):
        batches = per_source_postings(2)
        keys = list(batches)
        broken = keys[3]
        old, new = self.open_db("old.db"), self.open_db("new.db")
        real_dedupe = pipeline.deduplicate

        # The reference: a source whose link pass fails stores nothing.
        for key, records in batches.items():
            if key != broken:
                _reference_upsert_jobs(old, key, key, records, "2026-09-01T00:00:00+00:00")
                old.commit()

        real_upsert = pipeline.upsert_jobs
        current = {}

        def tracking_upsert(conn, source_key, *args, **kwargs):
            current["key"] = source_key
            return real_upsert(conn, source_key, *args, **kwargs)

        def flaky_dedupe(conn):
            if current.get("key") == broken:
                raise sqlite3.OperationalError("database is locked")
            return real_dedupe(conn)

        with unittest.mock.patch.object(pipeline, "upsert_jobs", tracking_upsert), \
                unittest.mock.patch.object(pipeline, "deduplicate", flaky_dedupe):
            result, stderr = self.run_fetch_ex(new, batches)  # must not raise
        self.assertIn("database is locked", stderr)
        outcomes = dict(new.execute("SELECT source_key, outcome FROM fetch_runs").fetchall())
        self.assertEqual(outcomes[broken], "error")
        self.assertEqual({v for k, v in outcomes.items() if k != broken}, {"success"})
        self.assertEqual(new.execute("SELECT COUNT(*) FROM jobs WHERE source_key=?", (broken,)).fetchone()[0], 0)
        self.assertEqual(
            [row[:-1] for row in state_digest(old)], [row[:-1] for row in state_digest(new)]
        )
        self.assertEqual(links(old), links(new))

    def test_a_resume_refetches_the_source_whose_link_pass_failed(self):
        batches = per_source_postings(3)
        keys = list(batches)
        broken = keys[1]
        conn = self.open_db("resumed.db")
        real_upsert = pipeline.upsert_jobs
        real_dedupe = pipeline.deduplicate
        current = {}

        def tracking_upsert(c, source_key, *args, **kwargs):
            current["key"] = source_key
            return real_upsert(c, source_key, *args, **kwargs)

        def flaky_dedupe(c):
            if current.get("key") == broken:
                raise sqlite3.OperationalError("database is locked")
            return real_dedupe(c)

        with unittest.mock.patch.object(pipeline, "upsert_jobs", tracking_upsert), \
                unittest.mock.patch.object(pipeline, "deduplicate", flaky_dedupe):
            self.run_fetch_ex(conn, batches)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs WHERE source_key=?", (broken,)).fetchone()[0], 0)
        # Only the failed source is fetched again, and its rows and links land.
        self.run_fetch_ex(conn, batches, resume_since="2000-01-01T00:00:00+00:00")
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM jobs WHERE source_key=?", (broken,)).fetchone()[0], 0)
        eager = self.open_db("eager.db")
        for key, records in batches.items():
            _reference_upsert_jobs(eager, key, key, records, "2026-09-01T00:00:00+00:00")
            eager.commit()
        self.assertEqual(links(eager), links(conn))

    def test_import_discovered_matches_per_channel_dedupe_and_runs_it_once(self):
        payload = json.loads((FIXTURES / "discovered_jobs_sample.json").read_text(encoding="utf-8"))
        records = payload["postings"] if isinstance(payload, dict) and "postings" in payload else payload
        # Repeat each posting under the other channels so there is something to link.
        channels = sorted(pipeline.AGENT_CHANNELS)
        spread = []
        for index, record in enumerate(records):
            for offset, channel in enumerate(channels[:3]):
                spread.append(dict(record, channel=channel, url=f"{record['url']}?v={index}{offset}"))
        with TemporaryDirectory() as temp:
            path = Path(temp) / "discovered.json"
            path.write_text(json.dumps(spread), encoding="utf-8")
            new, old = self.open_db("new.db"), self.open_db("old.db")
            with unittest.mock.patch("sys.stdout", io.StringIO()), unittest.mock.patch("sys.stderr", io.StringIO()):
                with unittest.mock.patch.object(pipeline, "deduplicate", wraps=pipeline.deduplicate) as spy:
                    pipeline.import_discovered(new, path)
                self.assertEqual(spy.call_count, 1)

                def eager(conn, source_key, source_name, batch, seen=None, **_ignored):
                    return _reference_upsert_jobs(conn, source_key, source_name, batch, seen)

                # The old side runs the frozen upsert and the frozen link pass, so it shares no changed code with the new side.
                with unittest.mock.patch.object(pipeline, "upsert_jobs", eager),                         unittest.mock.patch.object(pipeline, "deduplicate", _reference_deduplicate):
                    pipeline.import_discovered(old, path)
        self.assertGreater(new.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(links(old), links(new))


class FingerprintReuseTests(TempDbCase):
    def record(self, description):
        return posting("e1", "Acme", "Intern", "Austin, TX", description)

    def test_unchanged_description_keeps_the_stored_fingerprint_without_hashing(self):
        conn = self.open_db()
        text = body(random.Random(1), 120)
        pipeline.upsert_jobs(conn, "greenhouse:acme", "Acme", [self.record(text)])
        stored = conn.execute("SELECT content_fingerprint FROM jobs").fetchone()[0]
        self.assertEqual(stored, _reference_fingerprint_text(text))
        with unittest.mock.patch.object(pipeline, "fingerprint_text") as spy:
            pipeline.upsert_jobs(conn, "greenhouse:acme", "Acme", [self.record(text)])
        spy.assert_not_called()
        self.assertEqual(conn.execute("SELECT content_fingerprint FROM jobs").fetchone()[0], stored)

    def test_a_changed_description_is_fingerprinted_again(self):
        conn = self.open_db()
        rng = random.Random(2)
        first, second = body(rng, 120), body(rng, 130)
        pipeline.upsert_jobs(conn, "greenhouse:acme", "Acme", [self.record(first)])
        pipeline.upsert_jobs(conn, "greenhouse:acme", "Acme", [self.record(second)])
        stored = conn.execute("SELECT description, content_fingerprint FROM jobs").fetchone()
        self.assertEqual(stored["content_fingerprint"], _reference_fingerprint_text(stored["description"]))

    def test_a_blank_stored_fingerprint_is_recomputed(self):
        conn = self.open_db()
        text = body(random.Random(3), 120)
        pipeline.upsert_jobs(conn, "greenhouse:acme", "Acme", [self.record(text)])
        conn.execute("UPDATE jobs SET content_fingerprint=''")
        pipeline.upsert_jobs(conn, "greenhouse:acme", "Acme", [self.record(text)])
        self.assertEqual(
            conn.execute("SELECT content_fingerprint FROM jobs").fetchone()[0],
            _reference_fingerprint_text(text),
        )

    def test_the_merged_description_decides_reuse_not_the_incoming_one(self):
        # A thin incoming description keeps the stored (richer) one, so the stored fingerprint stays right.
        conn = self.open_db()
        text = body(random.Random(4), 150)
        pipeline.upsert_jobs(conn, "greenhouse:acme", "Acme", [self.record(text)])
        pipeline.upsert_jobs(conn, "greenhouse:acme", "Acme", [self.record("short")])
        stored = conn.execute("SELECT description, content_fingerprint FROM jobs").fetchone()
        self.assertEqual(stored["description"], text)
        self.assertEqual(stored["content_fingerprint"], _reference_fingerprint_text(text))


class ScoreAllParityTests(TempDbCase):
    def profile(self):
        return json.loads((FIXTURES / "profile_student.json").read_text(encoding="utf-8"))

    def build(self, name):
        conn = self.open_db(name)
        rows = generate_rows(8, count=60)
        populate(conn, rows, 8)
        # A few rows already scored, so both the unchanged and changed paths run.
        conn.execute("UPDATE jobs SET score=77, score_explanation='[\"stale\"]' WHERE rowid % 3 = 0")
        conn.commit()
        return conn

    def snapshot(self, conn):
        return [
            tuple(row)
            for row in conn.execute("SELECT id, role_type, score, score_explanation FROM jobs ORDER BY id")
        ]

    def test_end_state_matches_the_original(self):
        profile = self.profile()
        old, new = self.build("old.db"), self.build("new.db")
        _reference_score_all(old, profile)
        with unittest.mock.patch("sys.stdout", io.StringIO()):
            count = pipeline.score_all(new, profile)
        self.assertEqual(count, new.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        self.assertEqual(self.snapshot(old), self.snapshot(new))

    def test_a_second_run_writes_no_rows(self):
        profile = self.profile()
        conn = self.build("new.db")
        with unittest.mock.patch("sys.stdout", io.StringIO()):
            pipeline.score_all(conn, profile)
            before = conn.total_changes
            snapshot = self.snapshot(conn)
            pipeline.score_all(conn, profile)
        self.assertEqual(conn.total_changes, before)
        self.assertEqual(self.snapshot(conn), snapshot)

    def test_a_row_whose_inputs_changed_is_rewritten(self):
        profile = self.profile()
        old, new = self.build("old.db"), self.build("new.db")
        with unittest.mock.patch("sys.stdout", io.StringIO()):
            pipeline.score_all(new, profile)
        _reference_score_all(old, profile)
        for conn in (old, new):
            conn.execute("UPDATE jobs SET title='Software Engineering Intern' WHERE id='job0003'")
            conn.commit()
        _reference_score_all(old, profile)
        with unittest.mock.patch("sys.stdout", io.StringIO()):
            pipeline.score_all(new, profile)
        self.assertEqual(self.snapshot(old), self.snapshot(new))


if __name__ == "__main__":
    unittest.main()
