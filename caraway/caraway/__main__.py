"""Command line: python3 -m caraway <command>

  scan [--no-intel] [--source NAME]   run one scan now (same as launchd)
  serve                               start the dashboard (http://127.0.0.1:8787)
  status                              print operational status
  leads [-n N]                        print ranked actionable leads
  pause | resume                      stop / allow all automated work
  report                              print a markdown summary of current results
  evaluate                            measure ranking accuracy against verified outcomes
  outcome ID OUTCOME [--value USD] [--evidence TEXT]   record a verified outcome
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from . import config, db, ranking, report, scanner


def _logging() -> None:
    config.ensure_dirs()
    handlers = [logging.StreamHandler(sys.stdout),
                RotatingFileHandler(config.LOG_DIR / "caraway.log", maxBytes=2_000_000, backupCount=3)]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=handlers)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="caraway")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("--no-intel", action="store_true"); s.add_argument("--source", action="append")
    s.add_argument("--trigger", default="manual")
    sub.add_parser("serve"); sub.add_parser("status"); sub.add_parser("pause"); sub.add_parser("resume")
    sub.add_parser("report"); sub.add_parser("evaluate")
    lp = sub.add_parser("leads"); lp.add_argument("-n", type=int, default=15)
    op = sub.add_parser("outcome"); op.add_argument("opportunity_id", type=int)
    op.add_argument("outcome", choices=["won", "lost", "no_bid", "cancelled", "worth_pursuing", "not_worth_pursuing", "shortlisted"])
    op.add_argument("--value", type=float); op.add_argument("--evidence", default="")
    a = ap.parse_args(argv)
    _logging()
    log = logging.getLogger("caraway")

    if a.cmd == "scan":
        try:
            res = scanner.run_scan(a.trigger, sources=a.source, intel=not a.no_intel)
        except scanner.Locked:
            log.info("scan skipped: another scan is running")
            return 0
        log.info("scan finished: %s", json.dumps(res, default=str))
        return 0 if res.get("status") in ("ok", "partial", "paused") else 1
    if a.cmd == "serve":
        from . import dashboard
        dashboard.serve()
        return 0
    conn = db.connect(); db.init(conn)
    if a.cmd == "pause":
        db.set_control(conn, "paused", "1"); db.audit(conn, "owner", "pause"); print("paused")
    elif a.cmd == "resume":
        db.set_control(conn, "paused", "0"); db.audit(conn, "owner", "resume"); print("resumed")
    elif a.cmd == "status":
        print(json.dumps(report.status(conn), indent=2, default=str))
    elif a.cmd == "leads":
        for r in report.leads(conn, a.n):
            print(f"{r['score']:5.1f}  {r['due_at'] or 'no firm due':<36} {r['title'][:70]}\n       {r['source_url']}")
    elif a.cmd == "report":
        print(report.markdown(conn))
    elif a.cmd == "evaluate":
        print(json.dumps({"active": ranking.evaluate(conn), "learning": ranking.propose_and_test(conn)}, indent=2))
    elif a.cmd == "outcome":
        conn.execute("INSERT INTO outcomes(opportunity_id,outcome,awarded_value,evidence,verified,recorded_at) VALUES(?,?,?,?,1,?)",
                     (a.opportunity_id, a.outcome, a.value, a.evidence, db.now()))
        if a.outcome == "won" and a.value:
            conn.execute("""INSERT INTO financial_facts(fact_type,basis,amount,as_of,opportunity_id,source,evidence,verified,created_at)
                            VALUES('awarded_value','actual',?,?,?,'owner',?,1,?)""", (a.value, db.now(), a.opportunity_id, a.evidence, db.now()))
        db.audit(conn, "owner", "record_outcome", str(a.opportunity_id), a.outcome, a.evidence)
        print("recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
