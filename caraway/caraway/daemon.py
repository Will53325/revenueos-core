"""Long-running scheduler for containers and plain servers (systemd timers or launchd
do the same job on hosts that have them). Runs a scan every CARAWAY_SCAN_INTERVAL
seconds with jitter, a backup once a day, and exits cleanly on SIGTERM."""
from __future__ import annotations

import logging
import os
import random
import signal
import time
from datetime import datetime, timezone

from . import backup, config, db, scanner

log = logging.getLogger("caraway.daemon")
_stop = False


def _handle(signum, frame):
    global _stop
    _stop = True
    log.info("signal %s received; stopping after the current step", signum)


def run(interval: int = None, jitter: int = 600) -> None:
    interval = interval or int(os.environ.get("CARAWAY_SCAN_INTERVAL", "14400"))
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    next_scan = time.time() + int(os.environ.get("CARAWAY_FIRST_SCAN_DELAY", "60"))
    log.info("daemon started: scan every %ss (+/- jitter %ss)", interval, jitter)
    while not _stop:
        now = time.time()
        if now >= next_scan:
            try:
                res = scanner.run_scan("daemon")
                log.info("scan %s", res.get("status"))
            except scanner.Locked:
                log.info("scan skipped: lock held")
            except Exception as e:  # noqa: BLE001 - keep the daemon alive; health check reports it
                log.exception("scan crashed: %s", e)
            next_scan = time.time() + interval + random.randint(-jitter, jitter)
        conn = db.connect(); db.init(conn)
        try:
            last = db.control(conn, "last_backup_at")
            if not last or (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() > 86400:
                log.info("backup %s", backup.run(conn))
        except Exception as e:  # noqa: BLE001
            log.exception("backup failed: %s", e)
            db.log_error(conn, "backup", str(e))
        finally:
            conn.close()
        for _ in range(60):  # wake every second so SIGTERM is honored quickly
            if _stop:
                break
            time.sleep(1)
    log.info("daemon stopped")
