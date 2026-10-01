"""Payload builders shared by more than one router module, each reading what it needs from the app context."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..schema import LOCAL_USER_ID
from ..outreach_discovery import scope_definitions as discovery_scope_definitions, last_runs as last_discovery_runs
from ..outreach_recontact import eligible_targets as recontact_eligible_targets
from .context import AppContext


def outreach_discovery_payload(ctx: AppContext, conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    return {
        "available": ctx.services.outreach_discovery_manager is not None and user_id == LOCAL_USER_ID,
        "scopes": [{"id": key, "label": value["label"]} for key, value in discovery_scope_definitions().items()],
        "runs": last_discovery_runs(conn, user_id=user_id),
        "active": ctx.services.outreach_discovery_manager.status() if ctx.services.outreach_discovery_manager is not None else None,
    }


def outreach_recontact_payload(ctx: AppContext, conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    return {
        "available": ctx.services.outreach_recontact_manager is not None and user_id == LOCAL_USER_ID,
        "eligible": len(recontact_eligible_targets(conn, user_id=user_id)),
        "active": ctx.services.outreach_recontact_manager.status() if ctx.services.outreach_recontact_manager is not None else None,
    }
