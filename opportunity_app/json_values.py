"""Decode a JSON column without ever raising.

Rows keep small JSON documents as text (`detail_json`, `profile_json`,
`evidence_json`). A reader that only wants the document, and treats anything
unreadable as "nothing there", uses these two. They are for that and no other
reader: a bare `json.loads(text or "{}")` that RAISES on corrupt text is
deliberate in many places (a corrupt row should be loud), and must not be
turned into one of these.

Standard library only.
"""

from __future__ import annotations

import json
from typing import Any


def json_dict(text: Any) -> dict[str, Any]:
    """The JSON object in ``text``; {} for empty, malformed or non-object text."""
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def json_as(text: Any, default: Any) -> Any:
    """The JSON value in ``text`` when it has the same type as ``default``, else ``default``.

    Empty, malformed and wrongly typed text all give ``default``. The check is
    ``isinstance(value, type(default))``, so a ``[]`` default accepts lists and a
    ``{}`` default accepts objects.
    """
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default
