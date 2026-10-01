"""Static assets: content-hash version stamps, the pages that carry them, and which responses may be cached forever.

Asset URLs carry a version derived from the file's own bytes. The pages used to hard-code one string ("?v=20260918-outreach-split")
that a human had to remember to bump, and three of the four pages carried no version at all -- so styles.css was fetched at two
different URLs, one versioned and one not. A content hash cannot go stale: changing a file changes its URL, which is what makes
handing out immutable caching safe.

Everything here reads the app's static folder and its version cache from the app context, so each app caches its own hashes.
"""

from __future__ import annotations

import hashlib
import os
import re

from fastapi import Request
from fastapi.responses import HTMLResponse

from .context import AppContext

ASSET_NAME = re.compile(r"[A-Za-z0-9._-]+")
ASSET_REFERENCE = re.compile(r"""/assets/([A-Za-z0-9._-]+)(?:\?v=[^"']*)?""")


def asset_version(ctx: AppContext, name: str) -> str:
    # Only a plain file name directly inside static_dir has a version. A
    # name with a separator or an absolute path would make pathlib leave
    # static_dir, turning this into a hash oracle for any readable file.
    static_dir = ctx.config.static_dir
    if not ASSET_NAME.fullmatch(name) or name in {".", ".."}:
        return "0"
    path = static_dir / name
    try:
        resolved = path.resolve()
        # Case-insensitive volumes open styles.css for STYLES.CSS, and Windows for
        # "styles.css.." too, and macOS does not correct the case on resolve(). Only a
        # name exactly as the directory lists it is a version key, or every alias would
        # be one more cache entry and one more full read of the file.
        if resolved.parent != static_dir.resolve() or name not in os.listdir(static_dir):
            return "0"
        stat = path.stat()
    except OSError:
        return "0"
    if not path.is_file():
        return "0"
    signature = (stat.st_mtime_ns, stat.st_size)
    versions = ctx.runtime.asset_versions
    cached = versions.get(name)
    if cached is not None and cached[0] == signature:
        return cached[1]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    # Only listed names reach here, so this cap is a backstop; the cache simply starts over.
    if len(versions) >= 256:
        versions.clear()
    versions[name] = (signature, digest)
    return digest


def versioned_page(ctx: AppContext, name: str) -> HTMLResponse:
    """Serve a page with every /assets/ reference version-stamped."""

    html = ASSET_REFERENCE.sub(
        lambda match: f"/assets/{match.group(1)}?v={asset_version(ctx, match.group(1))}",
        (ctx.config.static_dir / name).read_text(encoding="utf-8"),
    )
    return HTMLResponse(html)


def cache_control(ctx: AppContext, request: Request, status_code: int) -> str:
    """The Cache-Control a response gets when it set none: immutable for an asset at its current hash, revalidate otherwise."""
    # An asset requested at its current content hash cannot go
    # stale: changing the file changes the URL. Anything else --
    # no version, or an old one -- revalidates as before.
    asset = request.url.path.removeprefix("/assets/")
    # A 404 or any other error is never immutable, but a 304
    # revalidation must be: browsers refresh their stored headers
    # from it. Unknown or foreign names have version "0", which
    # `?v=0` must not be able to match.
    if (
        request.url.path.startswith("/assets/")
        and status_code in (200, 304)
        and ASSET_NAME.fullmatch(asset)
        and (version := asset_version(ctx, asset)) != "0"
        and request.query_params.get("v") == version
    ):
        return "public, max-age=31536000, immutable"
    return "no-cache"
