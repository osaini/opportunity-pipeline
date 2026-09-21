"""Track a company's job board from the web app: ``pipeline.py discover --write``.

Looking a company up probes Greenhouse, Ashby and Lever exactly as the CLI
does (pipeline.discover_ats) and writes nothing. Adding one appends the entry
that lookup found to config/sources.local.json, the student's own overlay, so
the next refresh fetches it.

The identity rule is the CLI's. Only Greenhouse names the company that owns a
board; a match there is added on one click. A Greenhouse board with a different
name, and every Ashby or Lever board, is added only when the student says,
after seeing its postings, that it is the right company. The entry written is
always the one the lookup found; the client names a lookup, never a slug.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

Lookup = Callable[[list[str], dict[str, Any], list[str]], list[dict[str, Any]]]

LOOKUP_TTL_SECONDS = 15 * 60
SAMPLE_TITLES = 6


class BoardLookupExpired(LookupError):
    pass


class BoardTracker:
    def __init__(
        self,
        *,
        sources_path: Path | None = None,
        local_path: Path | None = None,
        lookup: Lookup | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        import pipeline

        self.sources_path = sources_path or pipeline.SOURCES_PATH
        self.local_path = local_path or pipeline.SOURCES_LOCAL_PATH
        self._lookup = lookup or pipeline.discover_ats
        self._clock = clock
        self._lock = threading.Lock()
        self._found: dict[str, tuple[float, dict[str, Any]]] = {}

    def _config(self) -> dict[str, Any]:
        import pipeline

        return pipeline.load_sources(self.sources_path, self.local_path)

    def look_up(self, company: str) -> dict[str, Any]:
        company = " ".join(company.split())
        if not company:
            raise ValueError("Enter a company name")
        config = self._config()
        (result,) = self._lookup([company], config, list(config.get("discovery_title_terms", [])))
        lookup_id = uuid4().hex
        with self._lock:
            now = self._clock()
            self._found = {key: value for key, value in self._found.items() if now - value[0] < LOOKUP_TTL_SECONDS}
            if result["status"] == "resolved":
                self._found[lookup_id] = (now, result)
        view: dict[str, Any] = {"company": company, "status": result["status"]}
        if result["status"] == "already-configured":
            view.update(kind=result["kind"], slug=result["slug"])
        elif result["status"] == "resolved":
            view.update(
                lookup_id=lookup_id,
                kind=result["kind"],
                slug=result["slug"],
                board_name=result["board_name"],
                identity=result["identity"],
                total=result["total"],
                matching=len(result["matching"]),
                # Matching titles first: they are what the student is here for,
                # and they show best whose postings these are.
                sample_titles=(result["matching"] + [title for title in result["titles"] if title not in result["matching"]])[:SAMPLE_TITLES],
            )
        return view

    def add(self, lookup_id: str, *, student_confirmed: bool = False) -> dict[str, Any]:
        import pipeline

        with self._lock:
            found = self._found.get(lookup_id)
            if not found or self._clock() - found[0] >= LOOKUP_TTL_SECONDS:
                raise BoardLookupExpired("That lookup expired; look the company up again")
            result = found[1]
        if result["identity"] != "confirmed" and not student_confirmed:
            raise ValueError("This board does not name its company. Confirm it is the right one before adding it.")
        entry = dict(result["entry"])
        field = result["field"]
        # The catalog may have gained this board since the lookup (another tab, a git pull).
        for source in self._config().get("ats_sources", []):
            if source.get("kind") == entry["kind"] and str(source.get(field, "")).lower() == str(entry[field]).lower():
                with self._lock:
                    self._found.pop(lookup_id, None)
                return {"added": False, "already_tracked": True, "entry": entry}
        pipeline._write_discovered_sources([entry], self.local_path)
        with self._lock:
            self._found.pop(lookup_id, None)
        return {"added": True, "already_tracked": False, "entry": entry}
