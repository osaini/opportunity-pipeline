"""Deciding a monitored email event: the student confirms it against an application or ignores it.

An email the app read from Gmail is decided by the inbox workflow together with its proposals; any
other event takes the plain path in connections.decide_event_directly. This is the one place that
chooses, and it sits above both, which is why it is not in connections (the inbox workflow imports
connections).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .inbox import decide_event
from ..mail.connections import decide_event_directly, monitored_event


def decide_monitored_event(conn: sqlite3.Connection, event_id: str, decision: str, application_id: str | None, *, user_id: str) -> dict[str, Any]:
    event = monitored_event(conn, event_id, user_id=user_id)
    if event["status"] != "pending":
        raise ValueError("This monitored event was already decided")
    if decision not in {"confirm", "ignore"}:
        raise ValueError("Decision must be confirm or ignore")
    if decision == "confirm" and not application_id:
        raise ValueError("Choose an application before confirming this update")
    if (event.get("payload") or {}).get("source") == "application_mail":
        # An email the app read from Gmail: its proposals are decided with it, so it is never decided twice.
        decided = decide_event(conn, event, decision, application_id, user_id=user_id)
        if decided is not None:
            return decided
    return decide_event_directly(conn, event, decision, application_id, user_id=user_id)
