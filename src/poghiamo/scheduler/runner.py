# scheduler/runner.py
"""Long-running scheduler container.

Jobs:
  - nightly SQLite backup;
  - periodic events sweep over followed artists (each artist scanned across all
    enabled sources, deduplicated across users, on a weekly-ish cadence).
"""

import logging
import time

import schedule

from poghiamo.config import SCAN_BATCH_SIZE, SCAN_INTERVAL_DAYS
from poghiamo.services.backup import backup_database, todays_backup_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run_backup():
    try:
        backup_database()
    except Exception as e:
        logger.error(f"Backup failed: {e}", exc_info=True)


def run_scan_batch():
    """Scan one batch of due artists. Each artist opens its own transaction so a
    failure on one never rolls back the others."""
    from poghiamo.database.engine import SessionLocal
    from poghiamo.database.models import Artist
    from poghiamo.services.pipeline import artists_due_for_scan, scan_artist

    db = SessionLocal()
    try:
        due_ids = [a.id for a in artists_due_for_scan(db, SCAN_INTERVAL_DAYS, SCAN_BATCH_SIZE)]
    finally:
        db.close()

    if not due_ids:
        return
    logger.info(f"Events sweep: {len(due_ids)} artist(s) due.")
    for artist_id in due_ids:
        db = SessionLocal()
        try:
            artist = db.get(Artist, artist_id)
            summary = scan_artist(db, artist)
            db.commit()
            logger.info(f"Scanned '{artist.name}': {summary}")
        except Exception as e:
            db.rollback()
            logger.error(f"Scan failed for artist id={artist_id}: {e}", exc_info=True)
        finally:
            db.close()


def main():
    logger.info("Scheduler starting.")

    schedule.every().day.at("04:15").do(run_backup)
    # Sweep runs often but only touches artists whose cadence is due, so the
    # real per-artist frequency is SCAN_INTERVAL_DAYS; batching keeps each tick
    # small and polite to the sources.
    schedule.every(2).hours.do(run_scan_batch)

    if not todays_backup_path().exists():
        logger.info("No backup for today yet: taking one now.")
        run_backup()

    # Catch up on any due scans shortly after startup.
    try:
        run_scan_batch()
    except Exception as e:
        logger.error(f"Initial scan batch failed: {e}", exc_info=True)

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()
