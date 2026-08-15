# scheduler/runner.py
"""Long-running scheduler container. Phase 1: nightly DB backup.
Phase 3 adds the nightly per-artist source sweep here."""

import logging
import time

import schedule

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
        # A failing job must never kill the loop.
        logger.error(f"Backup failed: {e}", exc_info=True)


def main():
    logger.info("Scheduler starting.")

    # Nightly backup. Container TZ is set to Europe/Rome in compose so this
    # fires at a predictable local time.
    schedule.every().day.at("04:15").do(run_backup)

    # Self-healing catch-up: `schedule` has no misfire handling, so if the
    # container was down at 04:15 the day's file would simply be missing.
    # Taking one at startup whenever today's file is absent closes that hole
    # (the per-day filename makes repeats idempotent).
    if not todays_backup_path().exists():
        logger.info("No backup for today yet: taking one now.")
        run_backup()

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()
