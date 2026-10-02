"""Which Apply for me claims are still held: the one rule the run ledger and the automation ledger share.

A claim row (application_submit_claims, and the run rows that carry the same columns) says which server process made it
(``instance``), under which token, and when that process last showed it was alive (``heartbeat_at``). Whether a run may
still be working under it is answered here, from the row and from this process's own set of running tokens, so
automation.in_flight and automation.unconfirmed can ask it without importing the run ledger (apply_runs imports the
automation ledger, not the other way round).

apply_runs adds a token to RUNNING when it claims, and takes it out when the result is recorded or the runner ends.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import SERVER_INSTANCE
from .core.timestamps import parse_app_instant

# A claim, or a run, that another server process holds is held while its heartbeat is this fresh.
HELD_HEARTBEAT = timedelta(minutes=2)
# Tokens of the claims whose runs are working in this process. A claim is added by claim() and leaves when its
# result is recorded (settle, record_result) or the runner calls forget(); a claim this process made that is not
# in here was left by a run that ended without settling it.
RUNNING: set[str] = set()


def claim_held(row: Any, *, now: datetime | None = None) -> bool:
    """Whether a run may still be working under this claim (any row with token, instance and heartbeat_at).

    One this process made is held while its token is in RUNNING: when it is not, the run ended without settling
    it. One another server process made is held while its heartbeat is fresh, so a claim that process left when it
    died is recovered, and one it is still working is not.
    """
    if row["instance"] == SERVER_INSTANCE:
        return row["token"] in RUNNING
    beat = parse_app_instant(row["heartbeat_at"])
    return beat is not None and (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - beat < HELD_HEARTBEAT


def forget(token: str) -> None:
    """The run under this claim has ended in this process (the runner's finally)."""
    RUNNING.discard(token)
