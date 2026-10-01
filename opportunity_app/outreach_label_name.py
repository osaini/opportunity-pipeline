"""The name the student's outreach threads are labelled with in Gmail: one setting, read here and nowhere heavier.

outreach_labels does the labelling (it reads and writes Gmail, and renaming the label makes every company be searched
again), and the Outreach tab's status line (outreach_gmail.gmail_drafts_status) shows the name. Each needs only the
setting, and outreach_labels imports the module that holds the Gmail send path, which imports this one, so the setting's
reader lives apart from the labelling, standard library plus settings_store only.
"""

from __future__ import annotations

import sqlite3

from .settings_store import get_setting

DEFAULT_LABEL = "opportunities"
# user_settings: no row means DEFAULT_LABEL, and '' means labelling is off.
LABEL_SETTING = "outreach_gmail_label"


def label_name(conn: sqlite3.Connection, user_id: str) -> str:
    """The name replies are labelled with; '' when the student turned labelling off."""
    value = get_setting(conn, user_id, LABEL_SETTING)
    return DEFAULT_LABEL if value is None else value
