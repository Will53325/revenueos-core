"""Read-only views shared by the CLI, dashboard and morning report."""
from __future__ import annotations

import json

from . import config, db, eligibility


def status(conn) -> dict:
    last_ok = db.one(conn, "SELECT * FROM scan_runs WHERE status IN ('ok','partial') ORDER BY id DESC LIMIT 1")
    last = db.one(conn, "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1")
    counts = {r["eligibility_status"] or "unassessed": r["n"] for r in
              db.q(conn, "SELECT eligibility_status, COUNT(*) n FROM opportunities GROUP BY eligibility_status")}
    open_now = db.one(conn, "SELECT COUNT(*) n FROM opportunities WHERE lifecycle='open' AND eligibility_status NOT IN ('expired','not_an_opportunity')")
    return {
        "paused": db.control(conn, "paused") == "1" or config.STOP_FILE.exists(),
        "last_successful_scan": last_ok and {k: last_ok[k] for k in ("id", "started_at", "finished_at", "status", "trigger")},
        "last_scan": last and {k: last[k] for k in ("id", "started_at", "finished_at", "status", "trigger")},
        "opportunities_total": sum(counts.values()), "open_opportunities": open_now["n"] if open_now else 0,
        "by_eligibility": counts,
        "sources": db.q(conn, "SELECT name, kind, status, last_success_at, last_error, records_last_run, consecutive_failures FROM sources"),
        "open_blockers": db.q(conn, "SELECT action, reason, required_human_step FROM blocked_actions WHERE resolved_at IS NULL"),
        "award_intel_rows": db.one(conn, "SELECT COUNT(*) n FROM award_intel")["n"],
        "recompete_forecasts": db.one(conn, "SELECT COUNT(*) n FROM recompete_forecasts")["n"],
        "contractors": db.one(conn, "SELECT COUNT(*) n FROM contractors")["n"],
        "db_path": str(config.DB_PATH),
    }


def leads(conn, n: int = 15) -> list:
    rows = db.q(conn, """SELECT * FROM opportunities WHERE eligibility_status='candidate' AND lifecycle='open'
                         ORDER BY score DESC, due_at LIMIT ?""", (n,))
    for r in rows:
        r["blockers"] = json.loads(r["blockers_json"] or "[]")
        r["days_left"] = eligibility.days_left(r["due_at"])
    return rows


def rejected(conn, n: int = 300) -> list:
    rows = db.q(conn, """SELECT id,title,agency_name,due_at,eligibility_status,reasons_json,source_url FROM opportunities
                         WHERE eligibility_status IN ('not_eligible','expired','not_an_opportunity')
                         ORDER BY last_seen_at DESC LIMIT ?""", (n,))
    for r in rows:
        r["reasons"] = json.loads(r["reasons_json"] or "[]")
    return rows


def markdown(conn) -> str:
    st = status(conn)
    out = ["# Caraway scan summary", "",
           f"- Last successful scan: {(st['last_successful_scan'] or {}).get('finished_at', 'never')}",
           f"- Opportunities stored: {st['opportunities_total']} (open, not expired: {st['open_opportunities']})",
           f"- Eligibility: " + ", ".join(f"{k}: {v}" for k, v in st["by_eligibility"].items()),
           f"- Federal award records: {st['award_intel_rows']}; recompete forecasts: {st['recompete_forecasts']}; contractors: {st['contractors']}",
           "", "## Ranked candidate leads", ""]
    for r in leads(conn, 15):
        dl = f"{r['days_left']:.0f} days" if r["days_left"] is not None else "no firm deadline"
        out.append(f"- **{r['title']}** — {r['agency_name']} — due {r['due_at'] or 'n/a'} ({dl}) — score {r['score']}  \n"
                   f"  {r['source_url']}  \n  Blockers: {len(r['blockers'])}")
    return "\n".join(out)
