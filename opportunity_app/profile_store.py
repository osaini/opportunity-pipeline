"""Read a student's stored profile without creating anything.

`profile.get_profile` provisions a profile row (and may read the profile file)
the first time it is asked, which a background step or a read-only check must
not do. `read_stored_profile` only reads: no row, an unreadable document, or one
that is not an object all give {}. It is a leaf so the low-level modules that
need the student's settings (auto-triage, the daily archive, résumé variants,
apply limits) do not import `profile`, which loads `pipeline` for scoring.

`profile._stored_profile` is a different function on purpose: it raises
LookupError for a user that does not exist, which the save path relies on.

Standard library plus `json_values` only.
"""

from __future__ import annotations

from typing import Any

from .json_values import json_dict


def read_stored_profile(conn: Any, user_id: str) -> dict[str, Any]:
    """The saved profile as a dict, or {} when there is none or it cannot be read. Never writes."""
    row = conn.execute("SELECT profile_json FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    return json_dict(row[0]) if row else {}
