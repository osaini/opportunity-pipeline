"""Web/API product surface for the internship opportunity pipeline."""

from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEGACY_DB = ROOT / "data" / "pipeline.db"
DEFAULT_PLATFORM_DB = ROOT / "data" / "platform.db"
DEFAULT_PROFILE = ROOT / "config" / "profile.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"
# Where Apply for me keeps each student's screenshots.
APPLY_ROOT = ROOT / "data" / "private" / "apply"
# One id per server process, so a claim left by an earlier process can be told from one this process holds. Outreach
# send claims and apply claims share it.
SERVER_INSTANCE = uuid4().hex

__all__ = [
    "APPLY_ROOT",
    "DEFAULT_LEGACY_DB",
    "DEFAULT_PLATFORM_DB",
    "DEFAULT_PROFILE",
    "ROOT",
    "SERVER_INSTANCE",
    "STATIC_DIR",
]
