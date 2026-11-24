"""Database backups.

Uses SQLite's online backup API (safe while scans and the dashboard are running),
verifies the copy with an integrity check, gzips it, keeps CARAWAY_BACKUP_KEEP_DAYS
local copies, and optionally uploads to an Azure Blob container through a
write-only SAS URL (CARAWAY_BACKUP_SAS_URL). The SAS URL is a secret: it lives in
the env file, never in the repo or the database.
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, db, net

BACKUP_DIR_NAME = "backups"


def backup_dir() -> Path:
    d = config.HOME / BACKUP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot(src_path: Optional[Path] = None) -> Path:
    src_path = Path(src_path or config.DB_PATH)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw = backup_dir() / f"caraway-{stamp}.db"
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(raw))
    try:
        src.backup(dst, pages=1024, sleep=0.05)  # small steps: never blocks writers for long
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"backup integrity check failed: {ok}")
    finally:
        dst.close(); src.close()
    gz = raw.with_suffix(".db.gz")
    with open(raw, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    raw.unlink()
    return gz


def rotate(keep_days: int = None) -> int:
    keep_days = keep_days if keep_days is not None else config.BACKUP_KEEP_DAYS
    cutoff = time.time() - keep_days * 86400
    removed = 0
    files = sorted(backup_dir().glob("caraway-*.db.gz"))
    for f in files[:-3]:  # always keep the newest three regardless of age
        if f.stat().st_mtime < cutoff:
            f.unlink(); removed += 1
    return removed


def upload(path: Path, sas_url: str) -> str:
    """PUT a block blob into the container named in the SAS URL."""
    u = urllib.parse.urlparse(sas_url)
    blob_url = urllib.parse.urlunparse(u._replace(path=u.path.rstrip("/") + "/" + path.name))
    net.request(blob_url, method="PUT", data=path.read_bytes(),
                headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "application/gzip"}, retries=2, timeout=120)
    return f"{u.scheme}://{u.hostname}{u.path.rstrip('/')}/{path.name}"  # without the SAS token


def run(conn=None) -> dict:
    out: dict = {}
    gz = snapshot()
    out["file"], out["bytes"] = str(gz), gz.stat().st_size
    out["rotated"] = rotate()
    if config.BACKUP_SAS_URL:
        try:
            out["uploaded"] = upload(gz, config.BACKUP_SAS_URL)
        except net.NetError as e:
            out["upload_error"] = str(e)
    if conn is not None:
        db.audit(conn, "ops", "backup", out["file"], "ERROR" if "upload_error" in out else "OK",
                 db.dumps({k: v for k, v in out.items() if k != "file"}))
        db.set_control(conn, "last_backup_at", db.now())
    return out


def restore(src_gz: str, dest: Optional[Path] = None) -> Path:
    """Restore a backup to a new file (never overwrites the live DB in place)."""
    dest = Path(dest or str(config.DB_PATH) + ".restored")
    with gzip.open(src_gz, "rb") as fi, open(dest, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    c = sqlite3.connect(str(dest))
    ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    c.close()
    if ok != "ok":
        raise RuntimeError(f"restored file failed integrity check: {ok}")
    return dest
