"""Web/API product surface for the internship opportunity pipeline."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEGACY_DB = ROOT / "data" / "pipeline.db"
DEFAULT_PLATFORM_DB = ROOT / "data" / "platform.db"
DEFAULT_PROFILE = ROOT / "config" / "profile.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"

__all__ = [
    "DEFAULT_LEGACY_DB",
    "DEFAULT_PLATFORM_DB",
    "DEFAULT_PROFILE",
    "ROOT",
    "STATIC_DIR",
]
