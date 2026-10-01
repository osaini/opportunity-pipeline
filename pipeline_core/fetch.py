"""The fetch scheduler: runs every enabled source on a bounded, host-aware thread pool and stores the results."""

from __future__ import annotations

import sqlite3
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as futures_wait
from datetime import datetime, timezone
from typing import Any

from .clock import now_iso
from .config import source_key
from .http import _source_host, TransientFetchError
from .sources import _fetch_source
from .store import FatalDatabaseError, upsert_jobs


# Concurrency ceilings for the fetch. 34 of the 73 enabled sources share
# boards-api.greenhouse.io and 21 share api.ashbyhq.com, so a purely global
# pool would put a third of its workers on one hostname. Four per host is
# lighter than a person with a few tabs open; twelve overall keeps the pool
# busy across the other hosts while Greenhouse works through its queue.
FETCH_MAX_WORKERS = 12
FETCH_MAX_PER_HOST = 4


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _succeeded_since(conn: sqlite3.Connection, since: datetime) -> set[str]:
    """Source keys with a successful fetch that started at or after `since`."""
    succeeded = set()
    for row in conn.execute("SELECT source_key, started_at FROM fetch_runs WHERE outcome='success'"):
        try:
            if _parse_utc(row["started_at"]) >= since:
                succeeded.add(row["source_key"])
        except ValueError:
            continue
    return succeeded


def fetch_all(
    conn: sqlite3.Connection,
    sources_config: dict[str, Any],
    resume_since: str | None = None,
    *,
    max_workers: int = FETCH_MAX_WORKERS,
    max_per_host: int = FETCH_MAX_PER_HOST,
) -> int:
    """Fetch every enabled source; return how many failed transiently.

    Sources are fetched concurrently, but never more than `max_per_host` at a
    time against one hostname -- 34 of the enabled sources share Greenhouse's
    API host and 21 share Ashby's, so an unbounded pool would hammer two
    servers. The scheduler below submits a source only when both a global and a
    host slot are free; blocking a worker on a semaphore instead would let
    queued Greenhouse work occupy the pool and starve every other host.

    Only worker threads do network I/O. Every database write happens here, on
    the calling thread, so the single sqlite connection is never shared.

    With `resume_since`, sources that already succeeded since that moment are
    skipped, so a run interrupted by sleep or shutdown picks up where it
    stopped instead of refetching everything.
    """
    terms = sources_config["discovery_title_terms"]
    enabled = [source for source in sources_config["ats_sources"] if source.get("enabled", True)]
    done = _succeeded_since(conn, _parse_utc(resume_since)) if resume_since else set()
    if done:
        print(f"Resuming: {len(done)} source(s) already fetched in this run", flush=True)

    queue = []
    for source in enabled:
        key = source_key(source)
        if key not in done:
            queue.append((source, key))
    # One observation timestamp for the whole cycle. See upsert_jobs: reading
    # the clock per source would make undated postings rank by completion
    # order, which under concurrency is arbitrary.
    cycle_seen = now_iso()
    transient_failures = 0
    host_active: dict[str, int] = {}
    in_flight: dict[Any, tuple[dict[str, Any], str, int, str]] = {}

    def record_outcome(
        run_id: int, outcome: str, *, count: int = 0, listed: int | None = None, error: str = ""
    ) -> None:
        """Write a source's terminal state.

        Raises FatalDatabaseError if even this cannot be written: at that point
        the run can no longer report honestly and must stop rather than finish
        looking complete.
        """
        try:
            if outcome == "success":
                conn.execute(
                    "UPDATE fetch_runs SET finished_at=?, outcome='success', fetched_count=?, "
                    "listed_count=? WHERE id=?",
                    (now_iso(), count, listed, run_id),
                )
            else:
                conn.execute(
                    "UPDATE fetch_runs SET finished_at=?, outcome='error', error=? WHERE id=?",
                    (now_iso(), error[:1000], run_id),
                )
            conn.commit()
        except sqlite3.Error as exc:
            raise FatalDatabaseError(f"cannot record fetch outcome: {exc}") from exc

    pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="fetch")
    try:
        while queue or in_flight:
                # Submit everything that currently fits under both ceilings. The
                # queue is scanned rather than popped in order, so a host at its
                # limit does not block sources behind it that could run now.
                index = 0
                while index < len(queue) and len(in_flight) < max_workers:
                    source, run_key = queue[index]
                    host = _source_host(source)
                    if host_active.get(host, 0) >= max_per_host:
                        index += 1
                        continue
                    queue.pop(index)
                    try:
                        run_id = conn.execute(
                            "INSERT INTO fetch_runs(source_key, started_at, outcome) VALUES (?, ?, 'running')",
                            (run_key, now_iso()),
                        ).lastrowid
                        conn.commit()
                    except sqlite3.Error as exc:
                        raise FatalDatabaseError(f"cannot open a fetch run: {exc}") from exc
                    # Printed immediately before submitting, so this line agrees
                    # with started_at and an interrupted run leaves a visible
                    # `running` row for the source that was in flight.
                    print(f"Fetching {source['company']} ({source['kind']})…", flush=True)
                    future = pool.submit(_fetch_source, source, terms)
                    in_flight[future] = (source, run_key, run_id, host)
                    host_active[host] = host_active.get(host, 0) + 1

                if not in_flight:
                    # Every remaining source sits behind a host limit while nothing
                    # is running. Only reachable with a limit of zero, which would
                    # otherwise spin here forever.
                    raise ValueError("fetch concurrency limits admit no work")

                finished, _ = futures_wait(in_flight, return_when=FIRST_COMPLETED)
                for future in finished:
                    source, run_key, run_id, host = in_flight.pop(future)
                    host_active[host] -= 1
                    label = f"{source['company']} ({source['kind']})"
                    try:
                        records = future.result()
                    except Exception as exc:  # Keep other sources useful if one employer is down.
                        record_outcome(run_id, "error", error=str(exc))
                        if isinstance(exc, TransientFetchError):
                            transient_failures += 1
                        print(f"  ERROR {label}: {exc}", file=sys.stderr, flush=True)
                        print(f"  Done {label}: failed", flush=True)
                        continue
                    try:
                        # The duplicate_of pass stays inside each source's upsert:
                        # a failure in it rolls that source back (inserts,
                        # retirements and links together) and records an error,
                        # which a single pass after the last source could not do
                        # once earlier sources were marked successful.
                        count = upsert_jobs(
                            conn, run_key, source.get("label", source["company"]), records, cycle_seen
                        )
                    except FatalDatabaseError:
                        raise
                    except Exception as exc:
                        # Roll the partial batch back before recording the failure.
                        # Without this, committing the error row also commits
                        # however many postings landed before the error.
                        try:
                            conn.rollback()
                        except Exception as rollback_exc:
                            raise FatalDatabaseError(
                                f"cannot roll back a failed source: {rollback_exc}"
                            ) from exc
                        record_outcome(run_id, "error", error=str(exc))
                        print(f"  ERROR {label}: {exc}", file=sys.stderr, flush=True)
                        print(f"  Done {label}: failed", flush=True)
                        continue
                    record_outcome(run_id, "success", count=count, listed=getattr(records, "listed", None))
                    print(f"  Done {label}: {count} candidate postings saved", flush=True)
    finally:
        # A fatal database error must not wait on eleven other sources first.
        # Queued work is dropped immediately; anything already inside a socket
        # read still finishes that request, which Python cannot interrupt.
        for pending_future in in_flight:
            pending_future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
    return transient_failures
