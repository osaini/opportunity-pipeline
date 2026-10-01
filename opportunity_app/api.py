"""Role-scoped API and web surfaces for the opportunity platform."""

from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Any

from . import DEFAULT_PLATFORM_DB
from .web.app import create_app
from .web.context import LOOPBACK_HOSTS


_DEFAULT_APP_LOCK = threading.Lock()


def __getattr__(name: str) -> Any:
    """Build the default app on first access to `app`, not at import.

    `uvicorn opportunity_app.api:app` (the Dockerfile) resolves the attribute with getattr, which lands here.
    Importing this module builds nothing and reads no .env; a server start through `main()` or
    `launch.serve()` builds its own configured app exactly once.
    """
    if name == "app":
        with _DEFAULT_APP_LOCK:
            if "app" not in globals():
                globals()["app"] = create_app()
            return globals()["app"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=DEFAULT_PLATFORM_DB)
    parser.add_argument("--database-url", help="PostgreSQL URL; overrides --db")
    parser.add_argument("--token", help="Local access token; defaults to PIPELINE_WEB_TOKEN or a generated value")
    parser.add_argument("--employer-token", help="Employer bearer token; defaults to PIPELINE_EMPLOYER_TOKEN")
    parser.add_argument("--admin-token", help="Admin bearer token; defaults to PIPELINE_ADMIN_TOKEN")
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Allow binding beyond loopback. Use only behind HTTPS and a trusted reverse proxy.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_network:
        raise SystemExit(
            "Refusing a non-loopback bind without --allow-network. "
            "The local session cookie is not configured for public HTTP."
        )
    configured_app = create_app(
        db_path=args.db,
        database_url=args.database_url,
        access_token=args.token,
        employer_token=args.employer_token,
        admin_token=args.admin_token,
        # A loopback server answers only to loopback host names, so a page on
        # another site cannot reach it by re-pointing its own DNS name here.
        allowed_hosts=None if args.allow_network else list(LOOPBACK_HOSTS),
    )
    print(f"Opportunity app: http://{args.host}:{args.port}")
    print(f"Access token: {configured_app.state.access_token}")
    print(f"Employer API token: {configured_app.state.employer_token}")
    print(f"Admin API token: {configured_app.state.admin_token}")
    import uvicorn

    uvicorn.run(configured_app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
