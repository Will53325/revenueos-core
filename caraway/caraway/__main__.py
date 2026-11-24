"""Command line: python3 -m caraway <command>

Discovery
  scan [--no-intel] [--source NAME]   run one full cycle now (same as launchd)
  status | leads [-n N] | report      what Caraway knows
Pipeline
  brain cycle|plan|funnel             executive brain: act, show next actions, funnel metrics
  contractors research|list [--category C --state S]
  bid prepare OPP_ID                  build a bid package (pricing, cash flow, compliance)
  quote add --opportunity ID --contractor ID --amount USD --period month|year|visit --evidence TEXT
  outcome OPP_ID OUTCOME [--value USD] [--evidence TEXT]
Email (nothing is sent until you enable it)
  mail status | drafts | approve ID... | enable | disable
  mail connect --client-secret PATH   authorize Gmail (macOS Keychain)
  mail import FOLDER                  ingest replies exported as .eml files
Operations
  serve | doctor | pause | resume | evaluate
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
    for name in ("serve", "status", "pause", "resume", "report", "evaluate", "doctor"):
        sub.add_parser(name)
    lp = sub.add_parser("leads"); lp.add_argument("-n", type=int, default=15)
    op = sub.add_parser("outcome"); op.add_argument("opportunity_id", type=int)
    op.add_argument("outcome", choices=["won", "lost", "no_bid", "cancelled", "worth_pursuing", "not_worth_pursuing", "shortlisted"])
    op.add_argument("--value", type=float); op.add_argument("--evidence", default="")
    bp = sub.add_parser("brain"); bp.add_argument("action", choices=["cycle", "plan", "funnel"])
    cp = sub.add_parser("contractors"); cp.add_argument("action", choices=["research", "list"])
    cp.add_argument("--category"); cp.add_argument("--state"); cp.add_argument("-n", type=int, default=20)
    bdp = sub.add_parser("bid"); bdp.add_argument("action", choices=["prepare"]); bdp.add_argument("opportunity_id", type=int)
    qp = sub.add_parser("quote"); qp.add_argument("action", choices=["add"])
    qp.add_argument("--opportunity", type=int, required=True); qp.add_argument("--contractor", type=int, required=True)
    qp.add_argument("--amount", type=float, required=True); qp.add_argument("--period", default="year")
    qp.add_argument("--evidence", required=True); qp.add_argument("--verified", action="store_true")
    mp = sub.add_parser("mail"); mp.add_argument("action", choices=["status", "drafts", "approve", "enable", "disable", "connect", "import"])
    mp.add_argument("args", nargs="*"); mp.add_argument("--client-secret")
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
    out = None
    if a.cmd == "pause":
        db.set_control(conn, "paused", "1"); db.audit(conn, "owner", "pause"); out = "paused"
    elif a.cmd == "resume":
        db.set_control(conn, "paused", "0"); db.audit(conn, "owner", "resume"); out = "resumed"
    elif a.cmd == "status":
        out = report.status(conn)
    elif a.cmd == "doctor":
        from . import health
        out = health.check(conn)
    elif a.cmd == "leads":
        for r in report.leads(conn, a.n):
            print(f"{r['score']:5.1f}  #{r['id']:<5} {r['due_at'] or 'no firm due':<36} {r['title'][:70]}\n       {r['source_url']}")
    elif a.cmd == "report":
        out = report.markdown(conn)
    elif a.cmd == "evaluate":
        from . import brain
        out = {"ranking": ranking.evaluate(conn), "ranking_learning": ranking.propose_and_test(conn),
               "action_learning": brain.success_rates(conn)}
    elif a.cmd == "outcome":
        conn.execute("INSERT INTO outcomes(opportunity_id,outcome,awarded_value,evidence,verified,recorded_at) VALUES(?,?,?,?,1,?)",
                     (a.opportunity_id, a.outcome, a.value, a.evidence, db.now()))
        if a.outcome == "won" and a.value:
            conn.execute("""INSERT INTO financial_facts(fact_type,basis,amount,as_of,opportunity_id,source,evidence,verified,created_at)
                            VALUES('awarded_value','actual',?,?,?,'owner',?,1,?)""", (a.value, db.now(), a.opportunity_id, a.evidence, db.now()))
        db.audit(conn, "owner", "record_outcome", str(a.opportunity_id), a.outcome, a.evidence); out = "recorded"
    elif a.cmd == "brain":
        from . import brain
        out = {"cycle": lambda: brain.cycle(conn), "plan": lambda: brain.plan(conn), "funnel": lambda: brain.funnel_report(conn)}[a.action]()
    elif a.cmd == "contractors":
        from . import contractors
        if a.action == "research":
            out = contractors.research(conn)
        else:
            for c in contractors.match(conn, a.category, a.state, a.n) if a.category else \
                    db.q(conn, "SELECT * FROM contractors ORDER BY fit_score DESC LIMIT ?", (a.n,)):
                print(f"#{c['id']:<6} {c['fit_score'] or 0:>4.0f}  {c['name'][:40]:<40} {c['state'] or '':<3} {c['categories'] or '':<20} "
                      f"{c['email'] or '':<32} {c['phone'] or ''}")
    elif a.cmd == "bid":
        from . import bids
        r = bids.prepare(conn, a.opportunity_id)
        out = {k: r[k] for k in ("status", "compliant", "path")} | {"failing": [c["check"] for c in r["checks"] if c["status"] != "pass"]}
    elif a.cmd == "quote":
        conn.execute("""INSERT INTO quotes(contractor_id,opportunity_id,amount,period,scope,received_at,status,evidence,basis,verified)
                        VALUES(?,?,?,?,?,?, 'received',?, 'owner_entered',?)""",
                     (a.contractor, a.opportunity, a.amount, a.period, "as quoted", db.now(), a.evidence, int(a.verified)))
        conn.execute("UPDATE contractors SET relationship_stage='quoted', updated_at=? WHERE id=?", (db.now(), a.contractor))
        db.audit(conn, "owner", "record_quote", f"opp:{a.opportunity}", "RECORDED", a.evidence); out = "quote recorded"
    elif a.cmd == "mail":
        from .mail import engine, transport
        if a.action == "status":
            out = {"sending_enabled_by_owner": db.control(conn, "email_sending_enabled") == "1",
                   "policy_enabled": engine.load_policy().get("enabled"),
                   "gmail_connected": transport.GmailTransport().configured(),
                   "messages": db.q(conn, "SELECT status, classification, COUNT(*) n FROM messages GROUP BY 1,2"),
                   "threads": db.q(conn, "SELECT status, COUNT(*) n FROM threads GROUP BY 1")}
        elif a.action == "drafts":
            engine.prepare(conn)
            for m in db.q(conn, "SELECT id,status,to_address,subject,classification_detail FROM messages WHERE direction='outbound' AND status!='sent'"):
                print(f"#{m['id']} [{m['status']}] {m['to_address']}: {m['subject']}\n     {m['classification_detail'] or ''}")
        elif a.action == "approve":
            out = f"approved {engine.approve(conn, [int(x) for x in a.args])} message(s)"
        elif a.action in ("enable", "disable"):
            db.set_control(conn, "email_sending_enabled", "1" if a.action == "enable" else "0")
            db.audit(conn, "owner", f"email_{a.action}", "", "SET"); out = f"email sending {a.action}d by owner"
        elif a.action == "connect":
            from .mail import oauth
            out = oauth.connect(a.client_secret)
        elif a.action == "import":
            out = engine.import_eml_dir(conn, a.args[0])
    if out is not None:
        print(out if isinstance(out, str) else json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
