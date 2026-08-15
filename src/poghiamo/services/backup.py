# services/backup.py
"""Nightly SQLite backups: consistent snapshot via the sqlite3 backup API,
gzip-compressed, with simple retention. lesgoski's biggest gap, fixed here."""

import gzip
import logging
import os
import sqlite3
from datetime import date
from pathlib import Path

from poghiamo.config import BACKUP_DIR, BACKUP_KEEP, DATABASE_URL

logger = logging.getLogger(__name__)


def _db_path() -> Path | None:
    """Extract the filesystem path from a sqlite:/// URL; None for non-file DBs."""
    prefix = "sqlite:///"
    if not DATABASE_URL.startswith(prefix):
        return None
    path = DATABASE_URL[len(prefix):]
    return Path(path) if path and path != ":memory:" else None


def todays_backup_path(backup_dir: str | Path | None = None) -> Path:
    out_dir = Path(backup_dir if backup_dir is not None else BACKUP_DIR)
    return out_dir / f"poghiamo-{date.today().isoformat()}.db.gz"


def backup_database(backup_dir: str | Path | None = None, keep: int | None = None) -> Path | None:
    """Snapshot the DB to <backup_dir>/poghiamo-YYYY-MM-DD.db.gz and prune old files.

    Uses sqlite3's online backup API, which is safe against the live WAL-mode
    database (a plain file copy is not). The final .gz appears atomically via
    os.replace, and temp files are always cleaned up (a leftover from a killed
    run must never wedge the next one). Returns the written path, or None if
    the database is not a file (e.g. in-memory during tests).
    """
    src = _db_path()
    if src is None or not src.exists():
        logger.info("Backup skipped: no database file to back up.")
        return None

    out_dir = Path(backup_dir if backup_dir is not None else BACKUP_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = todays_backup_path(out_dir)

    # Clear any leftovers from previously killed runs.
    for stale in out_dir.glob(".backup-in-progress*"):
        stale.unlink(missing_ok=True)

    tmp_db = out_dir / f".backup-in-progress-{os.getpid()}.db"
    tmp_gz = out_dir / f".backup-in-progress-{os.getpid()}.gz"
    try:
        src_conn = sqlite3.connect(src)
        try:
            dst_conn = sqlite3.connect(tmp_db)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

        with open(tmp_db, "rb") as f_in, gzip.open(tmp_gz, "wb") as f_out:
            f_out.writelines(f_in)
        os.replace(tmp_gz, out_path)
    finally:
        tmp_db.unlink(missing_ok=True)
        tmp_gz.unlink(missing_ok=True)

    _prune(out_dir, keep if keep is not None else BACKUP_KEEP)
    logger.info(f"Backup written: {out_path}")
    return out_path


def _prune(out_dir: Path, keep: int):
    """Keep only the newest `keep` backups (names sort chronologically)."""
    backups = sorted(out_dir.glob("poghiamo-*.db.gz"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
        logger.info(f"Pruned old backup: {old}")
