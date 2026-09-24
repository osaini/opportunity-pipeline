"""The fetch runs sources concurrently without hammering any one host.

34 of the enabled sources share Greenhouse's API host and 21 share Ashby's, so
"go faster" and "stay polite" pull against each other. These tests hold fetches
open on barriers to observe what the scheduler actually does rather than
inferring it from wall-clock time, which would be flaky.

They also pin the parts that must survive the change: one `fetch_runs` row per
source with a visible `running` state, per-source isolation for database
failures as well as network ones, a single observation timestamp for the whole
cycle, and the exit-75 transient count the scheduled wrapper depends on.
"""

from __future__ import annotations

import email.utils
import io
import sqlite3
import threading
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pipeline


def sources(*specs):
    return {
        "discovery_title_terms": ["intern"],
        "ats_sources": [
            dict(kind=kind, company=company, token=company.lower(), **extra)
            for kind, company, extra in specs
        ],
    }


GREENHOUSE_8 = sources(*[("greenhouse", f"G{i}", {}) for i in range(8)])


class InFlight:
    """A fetcher that records the most fetches ever in flight at once.

    Each fetch holds until the scheduler next stops submitting and waits for
    one to finish. By then it has submitted everything its ceilings admit in
    that round, and anything past them, so every round's peak is exact. A
    fixed sleep is not: the scheduler commits a ``fetch_runs`` row before every
    submit, and on a slow disk one commit can outlast the sleep, so each fetch
    finished before the next began. That made the peak 1 on a correct
    scheduler and hid one that overran its ceiling.
    """

    def __init__(self):
        self.live = 0
        self.peak = 0
        self._waits = 0
        self._changed = threading.Condition()

    def __call__(self, source, _terms):
        with self._changed:
            self.live += 1
            self.peak = max(self.peak, self.live)
            started_during = self._waits
            # The timeout only bounds a hung run; it is never reached when
            # the scheduler works.
            self._changed.wait_for(lambda: self._waits > started_during, timeout=5)
            self.live -= 1
        return []

    def observing(self):
        real_wait = pipeline.futures_wait

        def futures_wait(*args, **kwargs):
            with self._changed:
                self._waits += 1
                self._changed.notify_all()
            return real_wait(*args, **kwargs)

        return unittest.mock.patch.object(pipeline, "futures_wait", futures_wait)


class FetchConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = unittest.mock.patch.object(pipeline, "DB_PATH", Path(temp.name) / "pipeline.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = pipeline.connect()
        self.addCleanup(self.conn.close)
        # Real sleeps would make these tests slow and flaky; the limiter's
        # behaviour is covered separately.
        limiter = unittest.mock.patch.object(pipeline, "_HOST_LIMITER", pipeline._HostRateLimiter(0.0))
        limiter.start()
        self.addCleanup(limiter.stop)

    def run_fetch(self, config, fetcher, **kwargs):
        with unittest.mock.patch.dict(
            pipeline._SOURCE_FETCHERS, {"greenhouse": fetcher, "lever": fetcher, "ashby": fetcher}
        ), unittest.mock.patch("sys.stdout", io.StringIO()) as out, \
                unittest.mock.patch("sys.stderr", io.StringIO()) as err:
            failures = pipeline.fetch_all(self.conn, config, **kwargs)
        return failures, out.getvalue(), err.getvalue()

    # --- ceilings -----------------------------------------------------------

    def test_never_exceeds_the_per_host_ceiling(self):
        fetcher = InFlight()
        with fetcher.observing():
            self.run_fetch(GREENHOUSE_8, fetcher, max_workers=12, max_per_host=3)
        self.assertLessEqual(fetcher.peak, 3, "more than max_per_host requests were in flight on one host")
        self.assertEqual(fetcher.peak, 3, "the scheduler never used the host's full allowance")

    def test_never_exceeds_the_global_ceiling(self):
        fetcher = InFlight()
        # Eight distinct hosts, so only the global limit can bind.
        config = sources(*[
            ("workday", f"W{i}", {"tenant": f"t{i}", "datacenter": "wd1", "site": "s"})
            for i in range(8)
        ])

        with fetcher.observing(), \
                unittest.mock.patch.dict(pipeline._SOURCE_FETCHERS, {"workday": fetcher}), \
                unittest.mock.patch("sys.stdout", io.StringIO()), \
                unittest.mock.patch("sys.stderr", io.StringIO()):
            pipeline.fetch_all(self.conn, config, max_workers=3, max_per_host=4)
        self.assertLessEqual(fetcher.peak, 3, "more than max_workers requests were in flight")
        self.assertEqual(fetcher.peak, 3, "the scheduler never used its full worker allowance")

    def test_a_saturated_host_does_not_starve_another_host(self):
        """The scheduler must not park queued work in the worker pool.

        Four Greenhouse sources are held open, filling that host's allowance.
        A lever source queued behind them has its own host and must still run,
        which it cannot do if blocked Greenhouse tasks occupy every worker.
        """

        lever_started = threading.Event()
        # Whether lever got to run *while* greenhouse was still holding its
        # host allowance. Checking only after fetch_all returns proves nothing:
        # by then a head-of-line-blocked lever has run too, just far too late.
        concurrent = []

        def fetcher(source, _terms):
            if source["kind"] == "lever":
                lever_started.set()
                return []
            concurrent.append(lever_started.wait(timeout=2))
            return []

        config = sources(
            *[("greenhouse", f"G{i}", {}) for i in range(6)],
            ("lever", "Lev", {}),
        )
        self.run_fetch(config, fetcher, max_workers=6, max_per_host=4)
        self.assertTrue(
            concurrent and all(concurrent),
            "lever did not run while greenhouse held its host allowance: "
            "queued work is occupying the worker pool",
        )

    # --- fetch_runs lifecycle ----------------------------------------------

    def test_a_running_row_is_visible_while_a_source_is_in_flight(self):
        """An interrupted run must leave evidence of what was in flight.

        The probe happens inside the fetcher, on the worker thread, with its
        own connection: `fetch_all` owns the test's connection and sqlite
        objects cannot cross threads.
        """

        seen: list[dict[str, str]] = []

        def fetcher(source, _terms):
            probe = sqlite3.connect(pipeline.DB_PATH)
            probe.row_factory = sqlite3.Row
            try:
                seen.extend(
                    dict(row)
                    for row in probe.execute("SELECT source_key, outcome FROM fetch_runs")
                )
            finally:
                probe.close()
            return []

        config = sources(("greenhouse", "Solo", {}))
        self.run_fetch(config, fetcher, max_workers=1, max_per_host=1)
        self.assertEqual(seen, [{"source_key": "greenhouse:solo", "outcome": "running"}])
        # ...and it reaches a terminal state once the fetch returns.
        self.assertEqual(
            self.conn.execute("SELECT outcome FROM fetch_runs").fetchone()[0], "success"
        )

    def test_every_source_gets_exactly_one_terminal_fetch_run_row(self):
        def fetcher(source, _terms):
            if source["company"] == "G2":
                raise pipeline.TransientFetchError("offline")
            if source["company"] == "G3":
                raise RuntimeError("HTTP Error 404")
            return []

        failures, _, _ = self.run_fetch(GREENHOUSE_8, fetcher, max_workers=4, max_per_host=4)
        rows = self.conn.execute("SELECT source_key, outcome FROM fetch_runs").fetchall()
        self.assertEqual(len(rows), 8)
        self.assertEqual({r["outcome"] for r in rows}, {"success", "error"})
        self.assertEqual(sum(r["outcome"] == "error" for r in rows), 2)
        self.assertNotIn("running", {r["outcome"] for r in rows})
        self.assertEqual(failures, 1, "only the transient failure counts toward exit 75")

    # --- isolation ----------------------------------------------------------

    def test_a_database_failure_in_one_source_does_not_abandon_the_others(self):
        real_upsert = pipeline.upsert_jobs

        def flaky_upsert(conn, source_key, source_name, records, seen=None):
            if source_key == "greenhouse:g3":
                raise sqlite3.OperationalError("simulated write failure")
            return real_upsert(conn, source_key, source_name, records, seen)

        with unittest.mock.patch.object(pipeline, "upsert_jobs", flaky_upsert):
            self.run_fetch(GREENHOUSE_8, lambda s, t: [], max_workers=4, max_per_host=4)
        rows = {r["source_key"]: r["outcome"] for r in
                self.conn.execute("SELECT source_key, outcome FROM fetch_runs")}
        self.assertEqual(len(rows), 8, "every source still reached a terminal state")
        self.assertEqual(rows["greenhouse:g3"], "error")
        self.assertEqual(sum(v == "success" for v in rows.values()), 7)

    def test_a_malformed_record_fails_only_its_own_source(self):
        """Persistence failures are not all sqlite3.Error.

        A record missing a field raises KeyError out of upsert_jobs, and a bad
        URL raises ValueError. Catching only sqlite3.Error let those escape
        fetch_all entirely: the source kept a `running` row forever, every other
        completed future's result was discarded, and queued sources never ran.
        """

        def fetcher(source, _terms):
            if source["company"] == "G3":
                return [{"external_id": "x-1", "company": "G3"}]  # missing url/title/...
            return []

        failures, _, _ = self.run_fetch(GREENHOUSE_8, fetcher, max_workers=4, max_per_host=4)
        rows = {r["source_key"]: r["outcome"] for r in
                self.conn.execute("SELECT source_key, outcome FROM fetch_runs")}
        self.assertEqual(len(rows), 8, "the malformed source aborted the whole fetch")
        self.assertNotIn("running", set(rows.values()), "a source was left mid-flight")
        self.assertEqual(rows["greenhouse:g3"], "error")
        self.assertEqual(sum(v == "success" for v in rows.values()), 7)
        # A malformed payload is the employer's problem, not the network's, so
        # it must not ask the scheduled wrapper for a retry.
        self.assertEqual(failures, 0)

    def test_a_partial_batch_is_rolled_back_rather_than_committed(self):
        """A source that writes some rows and then fails must leave none."""

        real_upsert = pipeline.upsert_jobs

        def half_written(conn, source_key, source_name, records, seen=None):
            real_upsert(conn, source_key, source_name, records, seen)
            raise sqlite3.OperationalError("failed after writing")

        posting = [{
            "external_id": "x-1", "company": "G0", "title": "Intern",
            "location": "Austin, TX", "url": "https://example.com/x1",
            "description": "d" * 400, "posted_at": None,
        }]
        config = sources(("greenhouse", "G0", {}))
        with unittest.mock.patch.object(pipeline, "upsert_jobs", half_written):
            self.run_fetch(config, lambda s, t: posting, max_workers=1, max_per_host=1)
        remaining = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(remaining, 0, "the partial batch was committed alongside the error row")
        outcome = self.conn.execute("SELECT outcome FROM fetch_runs").fetchone()[0]
        self.assertEqual(outcome, "error")

    # --- determinism --------------------------------------------------------

    def test_undated_postings_share_one_observation_timestamp(self):
        """first_seen_at and last_seen_at are ranking keys.

        Reading the clock per source would make a posting with no posted_at
        rank by whichever source finished first, which under concurrency is
        arbitrary. Every source in a cycle must stamp the same instant.
        """

        def fetcher(source, _terms):
            time.sleep(0.02)
            return [{
                "external_id": f'{source["company"]}-1', "company": source["company"],
                "title": "Mechanical Engineering Intern", "location": "Austin, TX",
                "url": f'https://example.com/{source["company"]}',
                "description": "d" * 400, "posted_at": None,
            }]

        # The real clock cannot distinguish the two implementations here:
        # now_iso() has second precision, so eight fast sources produce the
        # same string whether it is read once or eight times. A clock that
        # advances on every read makes "read once per cycle" observable.
        counter = iter(range(1, 10_000))
        real_upsert = pipeline.upsert_jobs
        stamps: list[str] = []

        def recording_upsert(conn, source_key, source_name, records, seen=None):
            stamps.append(seen)
            return real_upsert(conn, source_key, source_name, records, seen)

        with unittest.mock.patch.object(
            pipeline, "now_iso", lambda: f"2026-09-20T00:00:{next(counter):02d}+00:00"
        ), unittest.mock.patch.object(pipeline, "upsert_jobs", recording_upsert):
            self.run_fetch(GREENHOUSE_8, fetcher, max_workers=4, max_per_host=4)

        self.assertEqual(len(stamps), 8)
        self.assertTrue(all(stamps), "a source upserted without an explicit cycle timestamp")
        self.assertEqual(
            len(set(stamps)), 1,
            f"the clock was read per source, not once per cycle: {sorted(set(stamps))}",
        )
        stored = {row[0] for row in self.conn.execute("SELECT DISTINCT first_seen_at FROM jobs")}
        self.assertEqual(stored, {stamps[0]})

    # --- progress output ----------------------------------------------------

    def test_each_source_emits_a_start_and_a_completion_line(self):
        _, out, _ = self.run_fetch(GREENHOUSE_8, lambda s, t: [], max_workers=4, max_per_host=4)
        starts = [line for line in out.splitlines() if line.startswith("Fetching ")]
        dones = [line for line in out.splitlines() if line.strip().startswith("Done ")]
        self.assertEqual(len(starts), 8)
        self.assertEqual(len(dones), 8, "progress cannot advance without one completion per source")

    def test_a_failed_source_still_emits_a_completion_line(self):
        def fetcher(source, _terms):
            raise RuntimeError("boom")

        _, out, _ = self.run_fetch(GREENHOUSE_8, fetcher, max_workers=4, max_per_host=4)
        dones = [line for line in out.splitlines() if line.strip().startswith("Done ")]
        self.assertEqual(len(dones), 8, "a failure must still close out its progress unit")


class HostDerivationTests(unittest.TestCase):
    def test_vendor_sources_share_a_host_and_workday_tenants_do_not(self):
        self.assertEqual(
            pipeline._source_host({"kind": "greenhouse", "token": "a"}),
            pipeline._source_host({"kind": "greenhouse", "token": "b"}),
        )
        self.assertNotEqual(
            pipeline._source_host({"kind": "workday", "tenant": "nvidia", "datacenter": "wd5", "site": "s"}),
            pipeline._source_host({"kind": "workday", "tenant": "boeing", "datacenter": "wd1", "site": "s"}),
        )

    def test_an_unknown_kind_is_throttled_rather_than_exempted(self):
        host = pipeline._source_host({"kind": "brand-new-ats"})
        self.assertTrue(host, "an unknown kind must still map to some throttling group")


class RateLimiterTests(unittest.TestCase):
    def test_requests_to_one_host_are_spaced_out(self):
        limiter = pipeline._HostRateLimiter(0.05)
        started = time.monotonic()
        for _ in range(4):
            limiter.acquire("example.com")
        self.assertGreater(time.monotonic() - started, 0.05,
                           "four requests to one host were not spaced at all")

    def test_different_hosts_do_not_wait_on_each_other(self):
        limiter = pipeline._HostRateLimiter(0.5)
        started = time.monotonic()
        limiter.acquire("a.example")
        limiter.acquire("b.example")
        self.assertLess(time.monotonic() - started, 0.4,
                        "a second host waited behind the first host's interval")

    def test_a_penalty_holds_every_thread_off_that_host(self):
        limiter = pipeline._HostRateLimiter(0.0)
        limiter.penalise("example.com", 0.2)
        started = time.monotonic()
        limiter.acquire("example.com")
        self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_retry_after_seconds_are_read_from_the_response(self):
        error = unittest.mock.Mock()
        error.headers = {"Retry-After": "12"}
        self.assertEqual(pipeline._retry_after_seconds(error), 12.0)
        self.assertEqual(pipeline._retry_after_seconds(None), 0.0)
        error.headers = {"Retry-After": "nonsense"}
        self.assertEqual(pipeline._retry_after_seconds(error), 0.0)

    def test_the_http_date_form_of_retry_after_is_honoured(self):
        """Both forms are legal HTTP; retrying earlier than asked is not ours to choose."""

        error = unittest.mock.Mock()
        error.headers = {
            "Retry-After": email.utils.format_datetime(
                datetime.now(timezone.utc) + timedelta(seconds=45)
            )
        }
        self.assertAlmostEqual(pipeline._retry_after_seconds(error), 45, delta=5)
        # A date already past asks for no delay, not a negative one.
        error.headers = {
            "Retry-After": email.utils.format_datetime(
                datetime.now(timezone.utc) - timedelta(seconds=45)
            )
        }
        self.assertEqual(pipeline._retry_after_seconds(error), 0.0)

    def test_the_limiter_bounds_the_rate_under_real_contention(self):
        """Single-threaded acquires would pass without the lock existing at all.

        Eight threads start together on a barrier and every acquisition is
        timestamped, so an implementation that let a burst through and delayed
        only once is visible in the gaps rather than hidden by the total.
        """

        limiter = pipeline._HostRateLimiter(0.05)
        barrier = threading.Barrier(8)
        stamps: list[float] = []
        stamps_lock = threading.Lock()

        def worker():
            barrier.wait(timeout=5)
            for _ in range(3):
                limiter.acquire("example.com")
                with stamps_lock:
                    stamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(len(stamps), 24)
        stamps.sort()
        # 24 acquisitions at a 0.05s floor cannot complete in under ~1.1s.
        self.assertGreater(stamps[-1] - stamps[0], 0.9, "the limiter let a burst through")
        # And no two consecutive acquisitions may share an instant.
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(gap > 0 for gap in gaps))

    def test_a_penalty_applied_while_threads_are_waiting_holds_them_all(self):
        limiter = pipeline._HostRateLimiter(0.0)
        limiter.penalise("example.com", 0.3)
        barrier = threading.Barrier(5)
        released: list[float] = []
        released_lock = threading.Lock()

        def worker():
            barrier.wait(timeout=5)
            limiter.acquire("example.com")
            with released_lock:
                released.append(time.monotonic())

        started = time.monotonic()
        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(released), 5)
        self.assertGreaterEqual(
            min(released) - started, 0.25, "a waiting thread slipped past the cooldown"
        )


if __name__ == "__main__":
    unittest.main()
