"""Automation notices as desktop pop-ups, for a student who turned them on.

``show`` pops one up on this computer and never raises: Windows' toast API
through PowerShell, osascript on macOS, notify-send on Linux. The title and
body never become code. On Windows the script is fixed text, and the words are
XML-escaped and handed to it base64-encoded in environment variables; on macOS
and Linux they are plain arguments.

``deliver_desktop_notices`` is one pass of the AutomationWorker: each student
with desktop_notifications on gets their notices not yet shown, outside their
quiet hours. Only notices that are still unread, from the last two days, and
made after the switch was last turned on: turning pop-ups on never brings back
a backlog, and a notice already read in the app never pops up. Pause does not
hold them back. Pause stops what the app does on its own, and a notice (such
as "Gmail needs reconnecting") only informs the student, so the gate is the
switch's own mode, not automation.is_enabled. A pop-up carries the notice's
title and body and nothing else, and never a link or an address. One that
fails is tried again on the next pass; after three failed passes it is marked
shown anyway, so it never loops.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

from . import automation
from .connections import ensure_preferences
from .notifications import in_quiet_hours
from .settings_store import setting_updated_at
from .user_time import user_timezone

LOGGER = logging.getLogger(__name__)

Notifier = Callable[[str, str], bool]

TIMEOUT_SECONDS = 10
# PowerShell's own AppUserModelID. Windows shows toasts under it without the app registering one.
WINDOWS_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
TITLE_ENV, BODY_ENV, APP_ENV = "PIPELINE_TOAST_TITLE", "PIPELINE_TOAST_BODY", "PIPELINE_TOAST_APP"
# One line, no double quotes: it is passed to -Command as a single argument. It reads the words
# only from the environment, so nothing in them is ever parsed as PowerShell.
_WINDOWS_SCRIPT = "; ".join((
    "$ErrorActionPreference = 'Stop'",
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null",
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null",
    "function Read-Text([string] $name) { [System.Text.Encoding]::UTF8.GetString("
    "[System.Convert]::FromBase64String([System.Environment]::GetEnvironmentVariable($name))) }",
    f"$title = Read-Text '{TITLE_ENV}'",
    f"$body = Read-Text '{BODY_ENV}'",
    "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
    "$xml.LoadXml('<toast><visual><binding template=''ToastText02''><text id=''1''>' + $title + "
    "'</text><text id=''2''>' + $body + '</text></binding></visual></toast>')",
    "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml",
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    f"[System.Environment]::GetEnvironmentVariable('{APP_ENV}')).Show($toast)",
))
_MAC_SCRIPT = ("on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run")


def _platform() -> str:
    return sys.platform


def _powershell() -> str:
    """Windows PowerShell by its full path, so a program named powershell.exe elsewhere on PATH is never run."""
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    path = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(path) if path.is_file() else "powershell.exe"


def _encoded(text: str) -> str:
    """XML-escaped, then base64: safe inside the toast's XML and inert in the environment."""
    return base64.b64encode(escape(text, {'"': "&quot;", "'": "&apos;"}).encode("utf-8")).decode("ascii")


def command(title: str, body: str, *, platform: str | None = None) -> tuple[list[str], dict[str, str]] | None:
    """The command that shows one pop-up and the environment variables it reads, or None where there is none."""
    platform = platform or _platform()
    if platform == "win32":
        return (
            [_powershell(), "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SCRIPT],
            {TITLE_ENV: _encoded(title), BODY_ENV: _encoded(body), APP_ENV: WINDOWS_APP_ID},
        )
    if platform == "darwin":
        return (["osascript", *(part for line in _MAC_SCRIPT for part in ("-e", line)), title, body], {})
    if platform.startswith("linux"):
        path = shutil.which("notify-send")
        # "--": a title that starts with a dash is still a title.
        return ([path, "--", title, body], {}) if path else None
    return None


def show(title: str, body: str) -> bool:
    """Show one desktop pop-up. True when the system took it; False on any failure. Never raises."""
    try:
        built = command(title, body)
        if built is None:
            return False
        argv, extra = built
        completed = subprocess.run(
            argv, env={**os.environ, **extra} if extra else None, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=TIMEOUT_SECONDS, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode == 0
    except Exception:  # noqa: BLE001 - a pop-up that fails is tried again; it never breaks the worker
        return False


# --- Delivering notices ----------------------------------------------------------------

# Older notices never pop up: one held back (quiet hours, a failing desktop) is stale after this.
RECENT = timedelta(days=2)
SWITCH = "desktop_notifications"
MAX_FAILED_PASSES = 3
MAX_TEXT = 200
_LINK = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_ADDRESS = re.compile(r"""[^\s@<>"'(),;:]+@[^\s@<>"'(),;:]+""")
# Failed passes per notice id, in memory: a restart only means a few more tries.
_FAILED: dict[str, int] = {}
_FAILED_LOCK = threading.Lock()


def _clip(text: str) -> str:
    """Only the notice's own words, on one line, with no link or address, and at most MAX_TEXT characters."""
    text = _ADDRESS.sub("[address]", _LINK.sub("[link]", str(text or "")))
    return " ".join(text.replace("\x00", " ").split())[:MAX_TEXT]


def _mark_shown(conn: sqlite3.Connection, notice_id: str, stamp: str) -> None:
    with conn:
        conn.execute("UPDATE automation_notices SET desktop_at=? WHERE id=? AND desktop_at IS NULL", (stamp, notice_id))


def _parse(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _switched_on_at(conn: sqlite3.Connection, user_id: str) -> datetime | None:
    """When desktop_notifications was last turned on, or None when it is off.

    The mode is read through the registry (automation.mode), so a value the
    switch does not take is off. Its user_settings row's updated_at moves only
    when the switch is saved, which the Automation panel does only on a change.
    (automation.on_since keeps a dedicated record that a same-value re-save does
    not move; auto_triage uses it. This reads updated_at on purpose, unchanged.)
    """
    if automation.mode(conn, user_id, SWITCH) != "on":
        return None
    stamp = setting_updated_at(conn, user_id, SWITCH)
    return _parse(stamp) if stamp is not None else None


def deliver_desktop_notices(conn: sqlite3.Connection, *, notifier: Notifier | None = None, now: datetime | None = None) -> int:
    """Show each waiting notice as a pop-up, for every student who turned pop-ups on. Returns how many were shown.

    Waiting means not shown yet, not read in the app, from the last two days,
    and made after the switch was last turned on. ``notifier`` (``show`` by
    default) gets the notice's title and body only. Opens its own
    transactions, so it is never called inside one.
    """
    notifier = notifier or show
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    recent = now - RECENT
    stamp = now.isoformat(timespec="microseconds")
    users = [row[0] for row in conn.execute(
        "SELECT DISTINCT user_id FROM automation_notices WHERE desktop_at IS NULL AND read_at IS NULL AND created_at>=? ORDER BY user_id",
        (recent.isoformat(timespec="microseconds"),),
    ).fetchall()]
    shown = 0
    for user_id in users:
        switched_on = _switched_on_at(conn, user_id)
        if switched_on is None:
            continue
        preferences = ensure_preferences(conn, user_id=user_id)
        if in_quiet_hours(preferences, now, zone=user_timezone(conn, user_id)):
            continue  # they wait for the morning
        since = max(recent, switched_on)
        notices = [notice for notice in conn.execute(
            "SELECT id, title, body, created_at FROM automation_notices WHERE user_id=? AND desktop_at IS NULL AND read_at IS NULL "
            "AND created_at>=? ORDER BY created_at, id",
            (user_id, recent.isoformat(timespec="microseconds")),
        ).fetchall() if (_parse(notice["created_at"]) or recent) >= since]
        for notice in notices:
            notice_id = str(notice["id"])
            try:
                ok = bool(notifier(_clip(notice["title"]), _clip(notice["body"])))
            except Exception:  # noqa: BLE001 - a notifier that raises counts as one that failed
                ok = False
            if ok:
                _mark_shown(conn, notice_id, stamp)
                with _FAILED_LOCK:
                    _FAILED.pop(notice_id, None)
                shown += 1
                continue
            with _FAILED_LOCK:
                failures = _FAILED.get(notice_id, 0) + 1
                given_up = failures >= MAX_FAILED_PASSES
                if given_up:
                    _FAILED.pop(notice_id, None)
                else:
                    _FAILED[notice_id] = failures
            if given_up:
                LOGGER.warning("A desktop notice could not be shown after %d tries; it stays in the app only", failures)
                _mark_shown(conn, notice_id, stamp)
    return shown
