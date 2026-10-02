"""Encrypted database backups and restores, for the ops CLI and the platform tests.

A SQLite file is snapshotted with the online backup API, a PostgreSQL database with pg_dump, and
either is encrypted with a Fernet key before it is written. Restoring checks the integrity of the
result. Kept apart from accounts/operations.py (job queue, account export and deletion, retention), which
shares no helper with it.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from ..core.database import connect_product, is_postgres_target
from .operations import OperationsError


def encrypted_backup(source: Path, destination: Path, key: bytes) -> dict[str, Any]:
    from cryptography.fernet import Fernet

    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file() or destination == source:
        raise OperationsError("Backup source must be an existing SQLite file and destination must differ")
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory) / "snapshot.db"
        source_conn = sqlite3.connect(source)
        target_conn = None
        try:
            target_conn = sqlite3.connect(temporary)
            source_conn.backup(target_conn)
        finally:
            if target_conn is not None:
                target_conn.close()
            source_conn.close()
        plaintext = temporary.read_bytes()
    ciphertext = Fernet(key).encrypt(plaintext)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(ciphertext)
    return {"path": str(destination), "sha256": hashlib.sha256(ciphertext).hexdigest(), "encrypted": True}


def restore_backup(source: Path, destination: Path, key: bytes) -> dict[str, Any]:
    from cryptography.fernet import Fernet, InvalidToken

    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file() or destination == source:
        raise OperationsError("Restore source must exist and destination must differ")
    try:
        plaintext = Fernet(key).decrypt(source.read_bytes())
    except InvalidToken as exc:
        raise OperationsError("Backup key or payload is invalid") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(plaintext)
    with closing(sqlite3.connect(destination)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        destination.unlink(missing_ok=True)
        raise OperationsError("Restored database failed integrity verification")
    return {"path": str(destination), "integrity": integrity, "restored": True}


def encrypted_database_backup(source: Path | str, destination: Path, key: bytes) -> dict[str, Any]:
    if not is_postgres_target(source):
        return encrypted_backup(Path(source), destination, key)
    from cryptography.fernet import Fernet
    destination = destination.resolve()
    with tempfile.TemporaryDirectory() as directory:
        dump = Path(directory) / "postgres.dump"
        completed = subprocess.run(["pg_dump", "--format=custom", "--file", str(dump), str(source)], capture_output=True, text=True, timeout=900)
        if completed.returncode:
            raise OperationsError(f"pg_dump failed: {completed.stderr[-1000:]}")
        ciphertext = Fernet(key).encrypt(dump.read_bytes())
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(ciphertext)
    return {"path": str(destination), "sha256": hashlib.sha256(ciphertext).hexdigest(), "encrypted": True, "backend": "postgresql"}


def restore_database_backup(source: Path, destination: Path | str, key: bytes) -> dict[str, Any]:
    if not is_postgres_target(destination):
        return restore_backup(source, Path(destination), key)
    from cryptography.fernet import Fernet, InvalidToken
    try:
        plaintext = Fernet(key).decrypt(source.resolve().read_bytes())
    except InvalidToken as exc:
        raise OperationsError("Backup key or payload is invalid") from exc
    with tempfile.TemporaryDirectory() as directory:
        dump = Path(directory) / "postgres.dump"
        dump.write_bytes(plaintext)
        completed = subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--dbname", str(destination), str(dump)], capture_output=True, text=True, timeout=900)
        if completed.returncode:
            raise OperationsError(f"pg_restore failed: {completed.stderr[-1000:]}")
    with closing(connect_product(str(destination), read_only=True)) as conn:
        count = int(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])
    return {"target": "postgresql", "restored": True, "opportunity_count": count}
