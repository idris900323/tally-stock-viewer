"""Minimal Tally-to-cloud feeder.

Runs on the office machine that can reach Tally. It does NOT start the
website (no Flask server, no waitress) and needs no mappings.db, no image
folder, and no login/admin routes. It only:

  1. pulls the car master, hierarchy and item stock from Tally, and
  2. pushes that data to the cloud site (CLOUD_SYNC_URL / CLOUD_SYNC_TOKEN).

The Tally fetching and the cloud push are the site's own, already-hardened
functions in app.py (self-healing retries, XML sanitizing, multiple-instance
detection); this file only decides WHEN to call them. Importing app.py starts
no threads and, with FEEDER_MODE=1 (set below), never touches the database.

Schedule (same as the full site): a full refresh (car master + hierarchy +
stock) on startup, then a stock export every TALLY_EXPORT_INTERVAL seconds
(180 by default). The site only runs a full refresh at startup or when an
admin clicks Full Refresh; nobody can click that here, so this also repeats it
every FEEDER_FULL_REFRESH_HOURS (default 6) so new cars/items reach the cloud.

Every cycle is wrapped in try/except, so an unreachable Tally or a failed push
just logs and retries next cycle. Run it forever under Windows Task Scheduler
(see FEEDER_SETUP.md), which restarts it if the process ever dies.
"""
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# The site's cache files use paths relative to the project folder.
os.chdir(BASE_DIR)
os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)

# Must be set before app.py is imported.
os.environ["FEEDER_MODE"] = "1"
os.environ.setdefault("LOG_FILE", "logs/feeder.log")

import app as site  # noqa: E402  (module import only; nothing is served or scheduled)
from config import Config  # noqa: E402

import logging  # noqa: E402

logger = logging.getLogger("feeder")

FULL_REFRESH_INTERVAL_SECONDS = int(float(os.environ.get("FEEDER_FULL_REFRESH_HOURS", "6")) * 3600)


def push_to_cloud():
    """The site's own push (never raises; logs its own success/failure)."""
    site._push_data_to_cloud()


def run_full_refresh():
    """Car master + hierarchy + stock from Tally, saved locally, then pushed.
    Same steps and order as the site's run_full_refresh_job(), minus the
    in-memory reload the website itself needs."""
    car_names = site.fetch_car_master_from_tally()
    site.save_car_master_to_file(car_names)
    flat_rows = site.fetch_main_hierarchy_from_tally()
    site.save_main_hierarchy_to_file(flat_rows)
    site.fetch_item_stock_flat()
    logger.info("Full refresh from Tally completed")
    push_to_cloud()


def run_stock_export():
    """Item stock only (the site's recurring export), then pushed."""
    site.fetch_item_stock_flat()
    push_to_cloud()


def main():
    if not Config.CLOUD_SYNC_URL or not Config.CLOUD_SYNC_TOKEN:
        logger.error(
            "CLOUD_SYNC_URL and CLOUD_SYNC_TOKEN must both be set in .env -- "
            "there is nothing for the feeder to do without them. Exiting."
        )
        return 2

    logger.info(
        "Feeder started: Tally=%s, cloud=%s, stock every %ss, full refresh every %ss",
        Config.TALLY_URL, Config.CLOUD_SYNC_URL, site.ITEM_EXPORT_INTERVAL, FULL_REFRESH_INTERVAL_SECONDS,
    )

    last_full_refresh_ok = None
    while True:
        need_full = (
            last_full_refresh_ok is None
            or time.monotonic() - last_full_refresh_ok >= FULL_REFRESH_INTERVAL_SECONDS
        )
        try:
            if need_full:
                logger.info("Running full refresh")
                run_full_refresh()
                last_full_refresh_ok = time.monotonic()
            else:
                run_stock_export()
        except Exception as exc:  # one bad cycle must never end the process
            logger.exception(
                "%s failed (will retry in %ss): %s",
                "Full refresh" if need_full else "Stock export", site.ITEM_EXPORT_INTERVAL, exc,
            )
        time.sleep(site.ITEM_EXPORT_INTERVAL)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("Feeder stopped")
