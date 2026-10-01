"""The `.env` line rules, written once, standard library only.

`pipeline_core.config.load_env_file` (fills gaps in the process environment, first line wins)
and `opportunity_app.setup.read_env` (a dict, last line wins) both read through
this, so the two cannot drift on what a line means. Duplicate keys are left to the
caller on purpose: the two readers have always differed there.
"""

from __future__ import annotations

from pathlib import Path


def iter_env_pairs(path: Path) -> list[tuple[str, str]]:
    """Every ``KEY=value`` line of a dotenv file, in file order; an unreadable file has none.

    Blank lines, ``#`` comments and lines with no ``=`` are skipped. The key and
    value are stripped and one matching pair of surrounding quotes is removed from
    the value. An empty key is kept: dropping it is the caller's call.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        pairs.append((key.strip(), value))
    return pairs
