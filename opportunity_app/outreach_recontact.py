"""Look again for a person to write to at targets that only have a shared inbox, or nothing.

The deep search finds contacts once, when a company is added. This pass re-reads
the company's site (in a browser when the plain HTML names no one), puts guesses
to the mail server, searches other sites, and then asks choose_contact() the
same question an unattended run asks.

It only ever upgrades, and only where the student has not committed to anything:

- the target is not sent and its draft is not approved (an approved draft is
  one the student reviewed for that recipient);
- its current address is empty or a shared inbox (a personal address, however
  it was found or typed, is left alone);
- the new choice is a person: a confirmed address or a guess.

Without apply=True nothing about the contact changes; candidates are still
refreshed so the student can see them. With redraft=True a draft that is not
approved is written again for the new recipient, so it greets them by name.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any, Callable

from .outreach import get_target
from .outreach_contacts import SafeFetcher, _is_generic, apply_choice, choose_contact, default_fetcher, find_contacts, list_candidates
from .schema import connect_product, utc_now

Runner = Callable[[str], str]

PERSON_BASES = {"confirmed", "strong_guess", "weak_guess"}


def upgradeable(target: dict[str, Any]) -> bool:
    if target["sent_at"] or target["status"] not in {"not_started", "drafted"}:
        return False
    if target["draft_status"] == "approved" or not target["website"]:
        return False
    return not target["contact_email"] or _is_generic(target["contact_email"])


def eligible_targets(conn: sqlite3.Connection, *, user_id: str) -> list[str]:
    """Ids of the targets a recontact pass would look at, in company order."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id FROM outreach_targets WHERE user_id=? ORDER BY company COLLATE NOCASE", (user_id,),
    ).fetchall()
    return [row["id"] for row in rows if upgradeable(get_target(conn, row["id"], user_id=user_id))]


def recontact_targets(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    fetcher: SafeFetcher,
    runner: Runner | None = None,
    verifier: Any = None,
    renderer: Any = None,
    target_ids: list[str] | None = None,
    limit: int | None = None,
    apply: bool = False,
    redraft: bool = False,
    provider_factory: Callable[[str, str], Any] | None = None,
    draft_provider: str | None = None,
    contact_delay: float = 1.0,
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id FROM outreach_targets WHERE user_id=? ORDER BY company COLLATE NOCASE", (user_id,),
    ).fetchall()
    chosen = set(target_ids) if target_ids is not None else None
    due = [
        row["id"] for row in rows
        if (chosen is None or row["id"] in chosen) and upgradeable(get_target(conn, row["id"], user_id=user_id))
    ]
    if limit is not None:
        due = due[:max(0, limit)]

    errors: dict[str, str] = {}
    for target_id in due:
        try:
            find_contacts(conn, target_id, user_id=user_id, fetcher=fetcher, delay=contact_delay,
                          renderer=renderer, verifier=verifier)
        except (ValueError, LookupError) as exc:
            errors[target_id] = str(exc)[:300]
    search: dict[str, Any] = {"searched": 0, "found": 0, "results": []}
    if runner is not None:
        # Imported here: outreach_discovery owns the error handling for a search run.
        from .outreach_discovery import _search_other_sites

        search = _search_other_sites(conn, due, user_id=user_id, runner=runner, fetcher=fetcher, verifier=verifier)

    results = _decide(
        conn, due, user_id=user_id, errors=errors, apply=apply, redraft=redraft,
        provider_factory=provider_factory, draft_provider=draft_provider,
    )
    return {
        "checked": len(due),
        "upgraded": sum(1 for result in results if result["to"]),
        "applied": apply,
        "email_search": search,
        "results": results,
    }


def apply_recontact(
    conn: sqlite3.Connection,
    choices: dict[str, str],
    *,
    user_id: str,
    redraft: bool = False,
    provider_factory: Callable[[str, str], Any] | None = None,
    draft_provider: str | None = None,
) -> dict[str, Any]:
    """Apply the upgrades a report already showed, without searching again.

    choices maps target id to the address the student saw in the report. The
    candidates stored by that report decide again here, and a target whose
    choice no longer matches what was shown (the student found someone else in
    between, or the stored candidates changed) is skipped rather than given an
    address the student never saw.
    """
    conn.row_factory = sqlite3.Row
    results = _decide(
        conn, list(choices), user_id=user_id, errors={}, apply=True, redraft=redraft,
        provider_factory=provider_factory, draft_provider=draft_provider, expected=choices,
    )
    return {
        "checked": len(results),
        "upgraded": sum(1 for result in results if result["applied"]),
        "applied": True,
        "results": results,
    }


def _decide(
    conn: sqlite3.Connection,
    due: list[str],
    *,
    user_id: str,
    errors: dict[str, str],
    apply: bool,
    redraft: bool,
    provider_factory: Callable[[str, str], Any] | None,
    draft_provider: str | None,
    expected: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    results = []
    for target_id in due:
        try:
            target = get_target(conn, target_id, user_id=user_id)
        except LookupError:
            results.append({"target_id": target_id, "company": "", "was": None, "to": None, "cc": None,
                            "basis": None, "applied": False, "draft": None, "skipped": "no longer tracked"})
            continue
        choice = choose_contact(list_candidates(conn, target_id, user_id=user_id))
        result: dict[str, Any] = {
            "target_id": target_id, "company": target["company"], "was": target["contact_email"],
            "to": None, "cc": None, "basis": None, "applied": False, "draft": None,
        }
        if target_id in errors:
            result["error"] = errors[target_id]
        if not upgradeable(target):
            # The student changed it while this ran: a new address, an approval, a send.
            result["skipped"] = "changed while the search ran"
        elif expected is not None and (not choice or choice["to"]["email"].lower() != expected[target_id].lower()):
            result["skipped"] = "the suggested contact changed since the report"
        elif choice and choice["basis"] in PERSON_BASES and choice["to"]["email"] != target["contact_email"]:
            result.update(
                to=choice["to"]["email"], cc=choice["cc"]["email"] if choice["cc"] else None, basis=choice["basis"],
                name=choice["to"].get("name") or "", role=choice["to"].get("role") or "",
            )
            if apply:
                apply_choice(conn, target_id, choice, user_id=user_id)
                result["applied"] = True
                if redraft and provider_factory is not None:
                    from .outreach_drafting import generate_draft

                    try:
                        generate_draft(conn, target_id, user_id=user_id, provider_factory=provider_factory, provider=draft_provider)
                        result["draft"] = "generated"
                    except (ValueError, RuntimeError) as exc:
                        result["draft"] = f"failed: {exc}"[:300]
        results.append(result)
    return results


class RecontactBusy(RuntimeError):
    pass


class RecontactManager:
    """Runs one recontact pass at a time in the background for the web app.

    A pass is either a report, which searches and stores candidates but changes
    no contact, or an apply, which takes the upgrades the student ticked in
    that report and searches nothing.
    """

    def __init__(
        self,
        platform_target: Path | str,
        *,
        runner: Runner | None = None,
        email_search: bool = True,
        client_factory: Callable[[], SafeFetcher] = default_fetcher,
        renderer_factory: Callable[[], Any] = lambda: None,
        verifier_factory: Callable[[], Any] = lambda: None,
        provider_factory: Callable[[str, str], Any] | None = None,
        draft_provider: str | None = None,
        contact_delay: float = 1.0,
    ) -> None:
        self.platform_target = platform_target
        self._runner = runner
        self._email_search = email_search
        self._client_factory = client_factory
        self._renderer_factory = renderer_factory
        self._verifier_factory = verifier_factory
        self._provider_factory = provider_factory
        self._draft_provider = draft_provider
        self._contact_delay = contact_delay
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "state": "idle", "mode": None, "started_at": None, "finished_at": None, "error": None, "result": None,
        }
        self._thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def start_report(self, *, user_id: str) -> dict[str, Any]:
        return self._start("report", lambda conn: self._report(conn, user_id))

    def start_apply(self, *, user_id: str, choices: dict[str, str], redraft: bool) -> dict[str, Any]:
        return self._start("apply", lambda conn: apply_recontact(
            conn, choices, user_id=user_id, redraft=redraft,
            provider_factory=self._provider_factory, draft_provider=self._draft_provider,
        ))

    def _report(self, conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
        runner = self._runner
        if runner is None and self._email_search:
            # Imported here: outreach_discovery owns the research runners.
            from .outreach_discovery import RUNNERS, claude_runner

            runner = RUNNERS.get(os.environ.get("PIPELINE_OUTREACH_DISCOVERY_PROVIDER") or "claude-code", claude_runner)
        with ExitStack() as stack:
            fetcher = stack.enter_context(self._client_factory())
            renderer = self._renderer_factory()
            verifier = self._verifier_factory()
            return recontact_targets(
                conn, user_id=user_id, fetcher=fetcher, runner=runner if self._email_search else None,
                verifier=stack.enter_context(verifier) if verifier is not None else None,
                renderer=stack.enter_context(renderer) if renderer is not None else None,
                contact_delay=self._contact_delay,
            )

    def _start(self, mode: str, work: Callable[[sqlite3.Connection], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            if self._state["state"] == "running":
                raise RecontactBusy("A contact search is already running")
            self._state = {
                "state": "running", "mode": mode, "started_at": utc_now(), "finished_at": None, "error": None, "result": None,
            }

        def run() -> None:
            result, error = None, None
            try:
                with closing(connect_product(self.platform_target)) as conn:
                    result = work(conn)
            except Exception as exc:  # noqa: BLE001 - reported to the UI
                error = str(exc)[:1_000]
            with self._lock:
                self._state.update(state="failed" if error else "succeeded", error=error, result=result, finished_at=utc_now())

        self._thread = threading.Thread(target=run, name=f"outreach-recontact-{mode}", daemon=True)
        self._thread.start()
        return self.status()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
