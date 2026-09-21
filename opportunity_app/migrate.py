"""Create/update the product database from the legacy pipeline database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import DEFAULT_LEGACY_DB, DEFAULT_PLATFORM_DB, DEFAULT_PROFILE
from .schema import migrate_legacy_database, result_dict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_LEGACY_DB)
    parser.add_argument("--target", default=str(DEFAULT_PLATFORM_DB), help="SQLite path or PostgreSQL URL")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = migrate_legacy_database(args.source, args.target, args.profile)
    print(json.dumps(result_dict(result), indent=2))
    if result.active_unique_source != result.active_unique_target or not result.top_ids_match:
        print("Parity check failed: the product read model differs from the legacy pipeline.")
        return 1
    print("Parity check passed: counts and the first 200 ranked IDs match the legacy pipeline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
