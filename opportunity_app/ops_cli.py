"""Encrypted SQLite backup, restore, and restore-drill commands."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from . import DEFAULT_PLATFORM_DB
from .operations import encrypted_database_backup, restore_database_backup


def _key() -> bytes:
    value = os.environ.get("PIPELINE_BACKUP_KEY", "").encode()
    if not value:
        raise SystemExit("PIPELINE_BACKUP_KEY must contain a Fernet key; generate one with `python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`")
    Fernet(value)  # validate before touching any file
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("destination", type=Path)
    backup.add_argument("--db", default=str(DEFAULT_PLATFORM_DB), help="SQLite path or PostgreSQL URL")
    restore = sub.add_parser("restore")
    restore.add_argument("source", type=Path)
    restore.add_argument("destination", help="SQLite path or PostgreSQL URL")
    drill = sub.add_parser("drill")
    drill.add_argument("source", type=Path)
    drill.add_argument("--target", help="Disposable PostgreSQL URL for a hosted restore drill")
    args = parser.parse_args()
    if args.command == "backup":
        print(encrypted_database_backup(args.db, args.destination, _key()))
    elif args.command == "restore":
        print(restore_database_backup(args.source, args.destination, _key()))
    else:
        if args.target:
            print(restore_database_backup(args.source, args.target, _key()))
        else:
            with tempfile.TemporaryDirectory() as directory:
                print(restore_database_backup(args.source, Path(directory) / "restore-drill.db", _key()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
