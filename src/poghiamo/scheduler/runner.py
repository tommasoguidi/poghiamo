# scheduler/runner.py
"""Long-running scheduler container.

Jobs:
  - nightly SQLite backup;
  - nightly DICE slug resolution from DICE's artist sitemap, when due;
  - the events scans, the only place where the app talks to the event sources,
    one artist at a time in two lanes: newly added artists within seconds, and
    a slow trickle of refreshes for artists whose SCAN_INTERVAL_DAYS ran out.
"""

import logging
import time

import schedule

from poghiamo.config import NEW_ARTIST_POLL_SECONDS, REFRESH_EVERY_MINUTES, SCAN_INTERVAL_DAYS
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


# A first scan can only fail on an internal error (a failing source is logged,
# not fatal), but the new lane polls every few seconds: keep a failed artist
# out of it for a while instead of retrying it in a tight loop.
NEW_ARTIST_RETRY_SECONDS = 600
_new_artist_retry_at: dict[int, float] = {}


def run_new_artist():
    """New lane: first scan of ONE never-scanned followed artist, if any. A bulk
    add (e.g. a future Spotify import) is thus drained one artist per tick."""
    from poghiamo.database.engine import SessionLocal
    from poghiamo.services.pipeline import new_artists_to_scan, scan_artist_by_id

    now = time.monotonic()
    db = SessionLocal()
    try:
        candidates = [a.id for a in new_artists_to_scan(db, limit=20)]
    finally:
        db.close()
    artist_id = next((i for i in candidates if _new_artist_retry_at.get(i, 0.0) <= now), None)
    if artist_id is None:
        return
    logger.info(f"New artist id={artist_id}: first scan.")
    if scan_artist_by_id(artist_id) is None:
        _new_artist_retry_at[artist_id] = now + NEW_ARTIST_RETRY_SECONDS
    else:
        _new_artist_retry_at.pop(artist_id, None)


def run_refresh():
    """Refresh lane: rescan ONE artist whose interval ran out, oldest first."""
    from poghiamo.database.engine import SessionLocal
    from poghiamo.services.pipeline import artists_due_for_scan, scan_artist_by_id

    db = SessionLocal()
    try:
        due = artists_due_for_scan(db, SCAN_INTERVAL_DAYS, limit=1)
        artist_id = due[0].id if due else None
    finally:
        db.close()
    if artist_id is not None:
        logger.info(f"Refresh: artist id={artist_id}.")
        scan_artist_by_id(artist_id)


def run_dice_slugs():
    """Match followed artists to DICE through its sitemap, when a crawl is due
    (resolve_slugs decides). Blocks this loop for up to an hour, hence a night slot."""
    from poghiamo.database.engine import SessionLocal
    from poghiamo.sources.dice import resolve_slugs

    db = SessionLocal()
    try:
        result = resolve_slugs(db)
        if result:
            logger.info(f"DICE slugs: {result}")
    except Exception as e:
        db.rollback()
        logger.error(f"DICE slug resolution failed: {e}", exc_info=True)
    finally:
        db.close()


def main():
    logger.info("Scheduler starting.")

    schedule.every().day.at("04:15").do(run_backup)
    schedule.every().day.at("03:00").do(run_dice_slugs)
    # Jobs run one after the other in this loop, so scans never overlap: a
    # single process, one artist at a time. After a downtime the due artists
    # are trickled through the refresh lane, not caught up in one burst.
    schedule.every(NEW_ARTIST_POLL_SECONDS).seconds.do(_guarded, run_new_artist)
    schedule.every(REFRESH_EVERY_MINUTES).minutes.do(_guarded, run_refresh)

    if not todays_backup_path().exists():
        logger.info("No backup for today yet: taking one now.")
        run_backup()

    while True:
        schedule.run_pending()
        time.sleep(1)


def _guarded(job):
    """Run a scan job so that an unexpected error is logged and the loop goes on."""
    try:
        job()
    except Exception as e:
        logger.error(f"{job.__name__} failed: {e}", exc_info=True)


if __name__ == "__main__":
    main()
