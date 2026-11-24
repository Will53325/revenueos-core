"""Scan orchestration: lock -> queue jobs -> run with retries -> ingest -> assess -> plan.

Safe to run from launchd every few hours: a file lock prevents overlapping
runs, stale jobs from a crash are requeued, and each source fails
independently (one broken source never stops the others).
"""
from __future__ import annotations

import fcntl
import time as _time
import json
import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import compliance, config, db, eligibility, executive, extract, llm, ranking
from .sources import INTEL_SOURCES, OPPORTUNITY_SOURCES, base, sam_gov

log = logging.getLogger("caraway.scan")
LLM_BUDGET_PER_RUN = 12  # summaries per run; keeps CPU/RAM use modest on 8 GB


class Locked(Exception):
    pass


def _register_sources(conn) -> None:
    for name, mod in list(OPPORTUNITY_SOURCES.items()) + list(INTEL_SOURCES.items()):
        conn.execute("""INSERT INTO sources(name,kind,description,requires_key) VALUES(?,?,?,?)
                        ON CONFLICT(name) DO UPDATE SET description=excluded.description""",
                     (name, "opportunities" if name in OPPORTUNITY_SOURCES else "intelligence",
                      mod.DESCRIPTION, int(mod.REQUIRES_KEY)))


def recover(conn) -> int:
    """Requeue jobs left 'running' by a crash or sleep."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    cur = conn.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='running' AND updated_at < ?",
                       (db.now(), cutoff))
    conn.execute("UPDATE scan_runs SET status='interrupted', finished_at=? WHERE status='running' AND started_at < ?",
                 (db.now(), cutoff))
    return cur.rowcount


def enqueue(conn, kind: str, payload: dict, scan_run_id: int, max_attempts: int = 3) -> None:
    conn.execute("""INSERT INTO jobs(kind,payload,status,attempts,max_attempts,next_run_at,created_at,updated_at,scan_run_id)
                    VALUES(?,?, 'queued',0,?,?,?,?,?)""",
                 (kind, json.dumps(payload), max_attempts, db.now(), db.now(), db.now(), scan_run_id))


def _source_ok(conn, name: str, n: int) -> None:
    conn.execute("""UPDATE sources SET status='ok', last_attempt_at=?, last_success_at=?, last_error=NULL,
                    consecutive_failures=0, records_last_run=? WHERE name=?""", (db.now(), db.now(), n, name))


def _source_fail(conn, name: str, status: str, err: str) -> None:
    conn.execute("""UPDATE sources SET status=?, last_attempt_at=?, last_error=?,
                    consecutive_failures=consecutive_failures+1 WHERE name=?""", (status, db.now(), err[:500], name))


# ---------------------------------------------------------------- ingestion

def _agency_id(conn, rec: dict) -> Optional[int]:
    if not rec.get("agency_name"):
        return None
    conn.execute("INSERT OR IGNORE INTO agencies(name,level,jurisdiction) VALUES(?,?,?)",
                 (rec["agency_name"], rec.get("agency_level"), rec.get("jurisdiction")))
    r = db.one(conn, "SELECT id FROM agencies WHERE name=? AND jurisdiction IS ?", (rec["agency_name"], rec.get("jurisdiction")))
    return r["id"] if r else None


def ingest(conn, rec: dict, profile: dict, stats: dict) -> int:
    compliance.authorize(conn, "store_record", rec["dedupe_key"])
    h = base.content_hash(rec)
    existing = db.one(conn, "SELECT id, content_hash FROM opportunities WHERE dedupe_key=?", (rec["dedupe_key"],))
    # Cross-source duplicate: same solicitation number already stored from another source.
    if not existing and rec.get("solicitation_number"):
        dup = db.one(conn, "SELECT id, dedupe_key FROM opportunities WHERE solicitation_number=? AND source<>?",
                     (rec["solicitation_number"], rec["source"]))
        if dup:
            stats["cross_source_duplicates"] = stats.get("cross_source_duplicates", 0) + 1
            conn.execute("UPDATE opportunities SET last_seen_at=? WHERE id=?", (db.now(), dup["id"]))
            return dup["id"]
    raw = db.dumps(rec.get("raw"))
    if existing:
        oid = existing["id"]
        if existing["content_hash"] == h:
            conn.execute("UPDATE opportunities SET last_seen_at=? WHERE id=?", (db.now(), oid))
            stats["unchanged"] = stats.get("unchanged", 0) + 1
            return oid
        conn.execute("INSERT INTO opportunity_versions(opportunity_id,content_hash,seen_at,change_note,raw_json) VALUES(?,?,?,?,?)",
                     (oid, h, db.now(), f"Notice changed ({rec.get('lifecycle_hint') or 'updated'})", raw))
        stats["amended"] = stats.get("amended", 0) + 1
        conn.execute("UPDATE opportunities SET version_count=version_count+1, last_changed_at=?, llm_summary=NULL WHERE id=?",
                     (db.now(), oid))
    else:
        conn.execute("INSERT INTO opportunities(dedupe_key, source, first_seen_at, last_changed_at) VALUES(?,?,?,?)",
                     (rec["dedupe_key"], rec["source"], db.now(), db.now()))
        oid = db.one(conn, "SELECT id FROM opportunities WHERE dedupe_key=?", (rec["dedupe_key"],))["id"]
        conn.execute("INSERT INTO opportunity_versions(opportunity_id,content_hash,seen_at,change_note,raw_json) VALUES(?,?,?,?,?)",
                     (oid, h, db.now(), "first seen", raw))
        stats["new"] = stats.get("new", 0) + 1
    conn.execute("""UPDATE opportunities SET source_record_id=?, title=?, solicitation_number=?, agency_id=?, agency_name=?,
        location=?, state=?, source_url=?, portal_url=?, published_at=?, due_at=?, due_at_raw=?, notice_type=?,
        category=?, naics=?, set_aside=?, selection_method=?, scope_text=?, documents_json=?, contact_json=?,
        content_hash=?, last_seen_at=?, raw_json=?, lifecycle=? WHERE id=?""",
        (rec["source_record_id"], rec["title"], rec["solicitation_number"], _agency_id(conn, rec), rec["agency_name"],
         rec["location"], rec["state"], rec["source_url"], rec["portal_url"], rec["published_at"], rec["due_at"],
         rec["due_at_raw"], rec["notice_type"], rec["category"], rec["naics"], rec["set_aside"],
         rec["selection_method"], rec["scope_text"], db.dumps(rec["documents"]), db.dumps(rec["contact"]),
         h, db.now(), raw, "cancelled" if rec.get("lifecycle_hint") == "cancelled" else "open", oid))
    return oid


def assess_one(conn, oid: int, profile: dict) -> dict:
    opp = db.one(conn, "SELECT * FROM opportunities WHERE id=?", (oid,))
    opp["lifecycle_hint"] = "cancelled" if opp["lifecycle"] == "cancelled" else None
    req = extract.extract_requirements(f"{opp['title']}. {opp['scope_text'] or ''}")
    a = eligibility.assess(opp, req, profile)
    s, p, version = ranking.score(conn, oid, opp, a, req)
    lifecycle = opp["lifecycle"]
    if a["eligibility_status"] == "expired":
        lifecycle = "expired"
    verification = "official record; requirements from notice text only (full solicitation not reviewed)"
    confidence = 0.6 if opp["scope_text"] else 0.4
    conn.execute("""UPDATE opportunities SET eligibility_status=?, go_no_go=?, reasons_json=?, blockers_json=?,
        target_category=?, next_action=?, score=?, predicted_prob=?, score_model=?, lifecycle=?,
        site_visit=?, site_visit_mandatory=?, site_visit_at=?, bonding_requirements=?, insurance_requirements=?,
        subcontracting_restrictions=?, eligibility_conditions=?, estimated_value=?, estimated_value_basis=?,
        evidence_json=?, verification_status=?, confidence=? WHERE id=?""",
        (a["eligibility_status"], a["go_no_go"], db.dumps(a["reasons"]), db.dumps(a["blockers"]),
         a["target_category"], a["next_action"], s, p, version, lifecycle,
         req["site_visit"], req["site_visit_mandatory"], req["site_visit_at"], req["bonding_requirements"],
         req["insurance_requirements"], req["subcontracting_restrictions"], db.dumps(req["eligibility_conditions"]),
         req["estimated_value"], req["estimated_value_basis"], db.dumps(req["evidence"]), verification, confidence, oid))
    return a


def summarize_top(conn, provider: llm.Provider) -> int:
    if not provider.available():
        return 0
    rows = db.q(conn, """SELECT id, title, scope_text FROM opportunities WHERE eligibility_status='candidate'
                         AND llm_summary IS NULL ORDER BY score DESC LIMIT ?""", (LLM_BUDGET_PER_RUN,))
    n = 0
    for r in rows:
        compliance.authorize(conn, "local_llm_summary", f"opp:{r['id']}")
        out = provider.summarize(r["title"], r["scope_text"] or "")
        if out:
            conn.execute("UPDATE opportunities SET llm_summary=?, llm_model=? WHERE id=?",
                         (out["summary"], f"{provider.name}:{getattr(provider, 'model', '')} (category guess: {out['category']})", r["id"]))
            n += 1
    return n


# ---------------------------------------------------------------- jobs

def _run_job(conn, job: dict, profile: dict, stats: dict) -> None:
    kind = job["kind"]
    payload = json.loads(job["payload"] or "{}")
    if kind == "source":
        name = payload["name"]
        mod = OPPORTUNITY_SOURCES[name]
        compliance.authorize(conn, "fetch_public_source", name)
        try:
            records = mod.fetch(conn)
        except sam_gov.MissingKey as e:
            _source_fail(conn, name, "blocked", str(e))
            db.block(conn, "sam_gov_api", "SAM.gov's official API needs a free public API key; none is configured.",
                     f"Sign in at sam.gov, open Account Details, request a public API key, then add "
                     f"{config.SAM_API_KEY_ENV}=<key> to the launchd plist (see README).")
            stats.setdefault("blocked_sources", []).append(name)
            return
        if name == "sam_gov":
            db.unblock(conn, "sam_gov_api")
        sstats: dict = {}
        for rec in records:
            ingest(conn, rec, profile, sstats)
        _source_ok(conn, name, len(records))
        stats.setdefault("sources", {})[name] = {"fetched": len(records), **sstats}
    elif kind == "intel":
        name = payload["name"]
        compliance.authorize(conn, "fetch_public_source", name)
        res = INTEL_SOURCES[name].sync(conn)
        _source_ok(conn, name, res.get("awards", 0))
        stats.setdefault("intel", {})[name] = res
    elif kind == "assess":
        counts: dict = {}
        for r in db.q(conn, "SELECT id FROM opportunities WHERE lifecycle IN ('open','cancelled')"):
            a = assess_one(conn, r["id"], profile)
            counts[a["eligibility_status"]] = counts.get(a["eligibility_status"], 0) + 1
        stats["assessment"] = counts
        stats["llm_summaries"] = summarize_top(conn, llm.get_provider())
    elif kind == "plan":
        stats["plan"] = executive.plan(conn, profile)
    elif kind == "contractors":
        from . import contractors as crm
        stats["contractors"] = crm.research(conn)
    elif kind == "brain":
        from . import brain
        from .mail import engine as mail
        stats["brain"] = brain.cycle(conn, profile)
        stats["mail"] = {"followups": mail.schedule_followups(conn), "dispatch": mail.dispatch(conn), "inbound": mail.poll_gmail(conn)}
    else:
        raise ValueError(f"unknown job kind {kind}")


def process_queue(conn, profile: dict, stats: dict) -> None:
    while True:
        if compliance.paused(conn):
            stats["paused_midway"] = True
            return
        job = db.one(conn, "SELECT * FROM jobs WHERE status='queued' AND next_run_at<=? ORDER BY id LIMIT 1", (db.now(),))
        if not job:
            # Retries scheduled a few seconds out belong to this run: wait for them (bounded).
            pending = db.one(conn, "SELECT MIN(next_run_at) t FROM jobs WHERE status='queued'")
            if pending and pending["t"]:
                wait = (datetime.fromisoformat(pending["t"]) - datetime.now(timezone.utc)).total_seconds()
                if wait <= 90:
                    _time.sleep(max(0.5, wait))
                    continue
            return
        conn.execute("UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?", (db.now(), job["id"]))
        try:
            _run_job(conn, job, profile, stats)
            conn.execute("UPDATE jobs SET status='done', updated_at=? WHERE id=?", (db.now(), job["id"]))
        except compliance.Denied as e:
            conn.execute("UPDATE jobs SET status='denied', last_error=?, updated_at=? WHERE id=?", (str(e), db.now(), job["id"]))
        except Exception as e:  # noqa: BLE001 - one failing job must not kill the scan
            attempts = job["attempts"] + 1
            detail = traceback.format_exc()
            db.log_error(conn, f"job:{job['kind']}", str(e), detail)
            log.warning("job %s failed (attempt %s): %s", job["kind"], attempts, e)
            name = json.loads(job["payload"] or "{}").get("name")
            if name:
                _source_fail(conn, name, "error", str(e))
            if attempts >= job["max_attempts"]:
                conn.execute("UPDATE jobs SET status='failed', last_error=?, updated_at=? WHERE id=?", (str(e)[:500], db.now(), job["id"]))
                stats.setdefault("failed_jobs", []).append(f"{job['kind']}:{name or ''}: {e}")
            else:
                retry_at = (datetime.now(timezone.utc) + timedelta(seconds=20 * attempts)).replace(microsecond=0).isoformat()
                conn.execute("UPDATE jobs SET status='queued', last_error=?, next_run_at=?, updated_at=? WHERE id=?",
                             (str(e)[:500], retry_at, db.now(), job["id"]))



def run_scan(trigger: str = "manual", conn=None, sources: Optional[list] = None, intel: bool = True) -> dict:
    config.ensure_dirs()
    own = conn is None
    conn = conn or db.connect()
    db.init(conn)
    lock_fh = open(config.LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.close()
        raise Locked("another scan is running")
    try:
        if compliance.paused(conn):
            db.audit(conn, "executive", "scan", trigger, "SKIPPED", "Paused")
            return {"status": "paused"}
        _register_sources(conn)
        recovered = recover(conn)
        profile = config.load_profile()
        cur = conn.execute("INSERT INTO scan_runs(started_at,status,trigger) VALUES(?,?,?)", (db.now(), "running", trigger))
        run_id = cur.lastrowid
        stats: dict = {"recovered_jobs": recovered}
        enabled = {r["name"] for r in db.q(conn, "SELECT name FROM sources WHERE enabled=1")}
        for name in (sources or list(OPPORTUNITY_SOURCES)):
            if name in enabled:
                enqueue(conn, "source", {"name": name}, run_id)
        if intel:
            for name in INTEL_SOURCES:
                if name in enabled:
                    enqueue(conn, "intel", {"name": name}, run_id, max_attempts=2)
        enqueue(conn, "assess", {}, run_id, max_attempts=1)
        enqueue(conn, "plan", {}, run_id, max_attempts=1)
        if intel:
            enqueue(conn, "contractors", {}, run_id, max_attempts=2)
        enqueue(conn, "brain", {}, run_id, max_attempts=1)
        process_queue(conn, profile, stats)
        failed = stats.get("failed_jobs")
        got_any = any(v.get("fetched") for v in stats.get("sources", {}).values())
        if not failed and db.one(conn, "SELECT COUNT(*) n FROM jobs WHERE scan_run_id=? AND status NOT IN ('done','denied')", (run_id,))["n"]:
            failed = stats["failed_jobs"] = ["jobs left unfinished"]
        status = "ok" if not failed else ("partial" if got_any else "failed")
        conn.execute("UPDATE scan_runs SET finished_at=?, status=?, stats_json=? WHERE id=?",
                     (db.now(), status, db.dumps(stats), run_id))
        db.audit(conn, "executive", "scan", trigger, status, db.dumps(stats)[:1500])
        stats["status"], stats["scan_run_id"] = status, run_id
        try:
            from . import health
            stats["health_ok"] = health.check(conn)["ok"]
        except Exception as e:  # noqa: BLE001 - health reporting must never break a scan
            db.log_error(conn, "health", str(e))
        return stats
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
        if own:
            conn.close()
