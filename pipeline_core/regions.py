"""Match a posting's location to the student's target regions, standard library only.

Lives here rather than in ``pipeline.py`` so the web schema can bucket a
location for display without loading the whole legacy pipeline.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .identity import normalized

_PLACEHOLDER_LOCATION_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.IGNORECASE)


def is_uninformative_location(location: str) -> bool:
    """True when a location field names no place at all.

    Workday boards emit "3 Locations" for multi-site postings. That says nothing
    about where the role is, so — like a blank field — it must not be read as
    evidence the role sits outside the target regions.
    """
    stripped = (location or "").strip()
    return not stripped or bool(_PLACEHOLDER_LOCATION_RE.match(stripped))


def match_region(location: str, regions: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Match a posting's location against the configured target regions.

    There is no geocoding here: "radius" is expressed as the curated place list
    each region carries, so a close radius is simply a shorter list. Bare city
    names collide across states (Newark, Dublin, Richmond, Concord, Berkeley),
    so a place only counts when the location also names one of the region's
    state markers. Region aliases ("bay area") are unambiguous on their own and
    skip that requirement.
    """
    lower = normalized(location)
    if not lower:
        return None
    for region in regions or []:
        if not isinstance(region, dict):
            continue
        for alias in region.get("aliases") or []:
            if normalized(alias) in lower:
                return {"region": region, "matched": alias}
        markers = [normalized(marker) for marker in region.get("state_markers") or []]
        if not any(re.search(rf"\b{re.escape(marker)}\b", lower) for marker in markers if marker):
            continue
        for place in region.get("places") or []:
            needle = normalized(place)
            if needle and re.search(rf"\b{re.escape(needle)}\b", lower):
                return {"region": region, "matched": place}
    return None


def region_label(location: str, profile: dict[str, Any]) -> str:
    """Bucket a location into a target region, "Remote", or "Other" for display."""
    hit = match_region(location, profile.get("regions") or [])
    if hit:
        return str(hit["region"].get("name", "Target region"))
    if "remote" in location.lower():
        return "Remote"
    if is_uninformative_location(location):
        return "Unknown"
    return "Other"
