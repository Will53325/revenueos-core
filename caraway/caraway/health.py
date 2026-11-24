"""Operational health: is Caraway actually running, and what failed?

`check()` is run after every scan and by `caraway doctor`. Problems become
an owner task, a line in var/health.json, and (on macOS) a Notification
Center alert — at most once per problem per day.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from . import config, db

STALE_HOURS = 9  # scans run every 4h; two missed runs = stale


def launchd_status() -> dict:
    """Scheduler status: launchd on macOS, systemd units on Linux, nothing inside a container
    (the container runtime's restart policy and healthcheck cover it)."""
    import os
    if os.environ.get("CARAWAY_IN_CONTAINER") == "1":
        return {"platform": "container", "checked": False}
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        out = {}
        for unit in ("caraway-scan.timer", "caraway-dashboard.service", "caraway-backup.timer"):
            r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
            installed = subprocess.run(["systemctl", "cat", unit], capture_output=True, text=True).returncode == 0
            if installed:
                out[unit] = {"loaded": r.stdout.strip() == "active", "state": r.stdout.strip()}
        return out or {"platform": "linux", "checked": False}
    if sys.platform != "darwin":
        return {"platform": sys.platform, "checked": False}
    out = {}
    for label in ("com.caraway.scanner", "com.caraway.dashboard"):
        r = subprocess.run(["launchctl", "print", f"gui/{__import__('os').getuid()}/{label}"], capture_output=True, text=True)
        state = next((l.split("=", 1)[1].strip() for l in r.stdout.splitlines() if l.strip().startswith("state =")), None)
        last_exit = next((l.split("=", 1)[1].strip() for l in r.stdout.splitlines() if "last exit code" in l), None)
        out[label] = {"loaded": r.returncode == 0, "state": state, "last_exit": last_exit}
    return out


def check(conn) -> dict:
    problems = []
    now = datetime.now(timezone.utc)
    last_ok = db.one(conn, "SELECT finished_at FROM scan_runs WHERE status IN ('ok','partial') ORDER BY id DESC LIMIT 1")
    if not last_ok:
        problems.append("no successful scan yet")
    elif datetime.fromisoformat(last_ok["finished_at"]) < now - timedelta(hours=STALE_HOURS):
        problems.append(f"last successful scan {last_ok['finished_at']} is older than {STALE_HOURS}h")
    since = (now - timedelta(hours=24)).replace(microsecond=0).isoformat()
    failed = db.q(conn, "SELECT kind, payload, last_error, updated_at FROM jobs WHERE status='failed' AND updated_at >= ?", (since,))
    for j in failed:
        problems.append(f"job failed: {j['kind']} {json.loads(j['payload'] or '{}').get('name', '')}: {j['last_error']}")
    for s in db.q(conn, "SELECT name, consecutive_failures, last_error FROM sources WHERE consecutive_failures >= 3 AND status != 'blocked'"):
        problems.append(f"source {s['name']} failed {s['consecutive_failures']} times in a row: {s['last_error']}")
    integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        problems.append(f"database integrity: {integrity}")
    free_gb = shutil.disk_usage(config.HOME).free / 1e9
    if free_gb < 2:
        problems.append(f"low disk space: {free_gb:.1f} GB free")
    ld = launchd_status()
    for label, st in ld.items():
        if isinstance(st, dict) and st.get("loaded") is False:
            problems.append(f"{label} is not active" + (" (run scripts/install_macos.sh)" if label.startswith("com.") else ""))
    last_backup = db.control(conn, "last_backup_at")
    if last_ok and (not last_backup or datetime.fromisoformat(last_backup) < now - timedelta(hours=36)):
        problems.append(f"no database backup in the last 36h (last: {last_backup or 'never'})")
    report = {"checked_at": db.now(), "ok": not problems, "problems": problems,
              "last_successful_scan": last_ok and last_ok["finished_at"], "launchd": ld, "disk_free_gb": round(free_gb, 1)}
    config.ensure_dirs()
    (config.HOME / "health.json").write_text(json.dumps(report, indent=2))
    if problems:
        db.task(conn, "Caraway health: problems detected", "\n".join(problems) + "\nDetails: var/health.json, var/logs/", "owner", 1)
        notify(conn, problems)
    else:
        conn.execute("UPDATE tasks SET status='done', updated_at=? WHERE title='Caraway health: problems detected' AND status='open'", (db.now(),))
    return report


def notify(conn, problems: list) -> None:
    """At most one alert per distinct problem set per day: macOS notification and/or webhook."""
    import hashlib
    digest = hashlib.sha256("|".join(sorted(problems)).encode()).hexdigest()[:12]
    key = "notified:" + datetime.now().strftime("%Y-%m-%d") + ":" + digest
    if db.control(conn, key) == "1":
        return
    msg = problems[0][:180] + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else "")
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{msg.replace(chr(34), chr(39))}" with title "Caraway needs attention"'],
                       capture_output=True)
    if config.ALERT_WEBHOOK:
        from . import net
        try:
            net.post_json(config.ALERT_WEBHOOK, {"text": "Caraway needs attention: " + "; ".join(problems)[:1500]}, retries=1)
        except Exception as e:  # noqa: BLE001 - alerting must never break the scan
            db.log_error(conn, "health:webhook", str(e))
    db.set_control(conn, key, "1")
