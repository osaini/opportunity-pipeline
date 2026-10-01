"""Where a stored file may live: directly inside its storage folder, nowhere else.

Standard library only, so any module that writes or reads uploaded files can use
it. The resume, capture and mock-answer stores each keep their files flat in one
folder under a generated name, and each resolves a stored name through this before
touching the disk. A name that escapes the folder (``..``, an absolute path, a
symlink out, a nested path) is refused.
"""

from __future__ import annotations

from pathlib import Path


def confined_path(root: Path, name: str) -> Path | None:
    """``root/name`` resolved, when that is a direct child of ``root``; ``None`` otherwise.

    Callers raise their own error for ``None``: the API maps each store's
    exception to a different status, so the type and message stay with the caller.
    """
    resolved_root = root.expanduser().resolve()
    candidate = (resolved_root / name).resolve()
    if candidate.parent != resolved_root:
        return None
    return candidate
