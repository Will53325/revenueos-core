"""Executive brain: progress toward revenue, not activity.

Each cycle:
  1. derive every opportunity's funnel stage from evidence
       discovered -> qualified -> contractors_identified -> quote_requested -> quote_received
       -> bid_ready -> submitted -> awarded -> collected
  2. evaluate earlier decisions (did the opportunity advance?)
  3. enumerate possible next actions, value them, pick the best
       value = P(action advances the stage) x value of advancing x urgency
     P starts from a prior and is replaced by the measured advancement rate once
     an action type has enough evaluated decisions.
  4. execute what Caraway may do alone (research, drafts, bid packages, follow-ups)
     and hand the rest to the owner as specific tasks
  5. snapshot the funnel

Every choice is a row in `decisions` with its expected value and later result.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import bids, compliance, config, contractors as crm, db, eligibility
from .mail import engine as mail

STAGES = ["discovered", "qualified", "contractors_identified", "quote_requested", "quote_received",
          "bid_ready", "submitted", "awarded", "collected"]
RANK = {s: i for i, s in enumerate(STAGES)}
# Prior probability that an action moves the opportunity to the next stage within a week.
PRIORS = {"research_contractors": 0.6, "draft_rfqs": 0.3, "send_rfqs": 0.5, "call_contractors": 0.35,
          "follow_up": 0.25, "prepare_bid": 0.4, "verify_blockers": 0.2, "submit_bid": 0.5}
AUTONOMOUS = {"research_contractors", "draft_rfqs", "follow_up", "prepare_bid"}
MIN_EVALUATED_TO_LEARN = 10
EVALUATE_AFTER_DAYS = 7
DEFAULT_ANNUAL_VALUE = {"janitorial": 150000, "landscaping": 120000, "facility_maintenance": 100000,
                        "moving_hauling": 80000, "pest_control": 40000}


def stage_of(conn, o: dict) -> str:
    oid = o["id"]
    if db.one(conn, "SELECT 1 FROM financial_facts WHERE opportunity_id=? AND fact_type='collected_cash' AND basis='actual' AND verified=1", (oid,)):
        return "collected"
    if db.one(conn, "SELECT 1 FROM outcomes WHERE opportunity_id=? AND outcome='won' AND verified=1", (oid,)):
        return "awarded"
    p = db.one(conn, "SELECT stage FROM pursuits WHERE opportunity_id=?", (oid,))
    if p and p["stage"] in ("submitted_by_owner", "won", "lost"):
        return "submitted"
    if db.one(conn, "SELECT 1 FROM bid_packages WHERE opportunity_id=? AND compliant=1", (oid,)):
        return "bid_ready"
    if db.one(conn, "SELECT 1 FROM quotes WHERE opportunity_id=? AND status='received'", (oid,)):
        return "quote_received"
    if db.one(conn, """SELECT 1 FROM messages m JOIN threads t ON t.id=m.thread_id WHERE t.opportunity_id=?
                       AND m.direction='outbound' AND m.status='sent'""", (oid,)) or \
       db.one(conn, "SELECT 1 FROM contractor_interactions WHERE opportunity_id=? AND kind='phone_quote_requested'", (oid,)):
        return "quote_requested"
    if o["eligibility_status"] == "candidate":
        if crm.match(conn, o["target_category"], o["state"], 1):
            return "contractors_identified"
        return "qualified"
    return "discovered"


def update_stages(conn) -> dict:
    counts = {s: 0 for s in STAGES}
    for o in db.q(conn, "SELECT id, eligibility_status, target_category, state, funnel_stage FROM opportunities"):
        st = stage_of(conn, o)
        counts[st] += 1
        if st != o["funnel_stage"]:
            conn.execute("UPDATE opportunities SET funnel_stage=?, funnel_updated_at=? WHERE id=?", (st, db.now(), o["id"]))
    return counts


def success_rates(conn) -> dict:
    rates = {}
    for r in db.q(conn, """SELECT action, COUNT(*) n, SUM(status='advanced') adv FROM decisions
                           WHERE status IN ('advanced','no_progress') GROUP BY action"""):
        if r["n"] >= MIN_EVALUATED_TO_LEARN:
            rates[r["action"]] = {"rate": r["adv"] / r["n"], "n": r["n"]}
    return rates


def evaluate_decisions(conn, now: Optional[datetime] = None) -> int:
    """A decision 'advanced' if its opportunity moved past the stage it was in, within the window."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=EVALUATE_AFTER_DAYS)).replace(microsecond=0).isoformat()
    n = 0
    for d in db.q(conn, "SELECT d.*, o.funnel_stage cur FROM decisions d JOIN opportunities o ON o.id=d.opportunity_id WHERE d.status IN ('executed','handed_to_owner')"):
        advanced = RANK.get(d["cur"] or "discovered", 0) > RANK.get(d["stage_before"], 0)
        if advanced or d["created_at"] <= cutoff:
            conn.execute("UPDATE decisions SET status=?, stage_after=?, evaluated_at=? WHERE id=?",
                         ("advanced" if advanced else "no_progress", d["cur"], db.now(), d["id"]))
            n += 1
    return n


def opportunity_value(conn, o: dict) -> tuple:
    """Estimated annual gross profit if won (an estimate, labeled with its basis)."""
    pkg = db.one(conn, "SELECT pricing_json FROM bid_packages WHERE opportunity_id=? ORDER BY version DESC LIMIT 1", (o["id"],))
    margin = bids.policy()["target_margin_pct"]
    if pkg:
        p = json.loads(pkg["pricing_json"])
        if p.get("gross_profit"):
            return p["gross_profit"], "bid package"
        if p.get("benchmark", {}).get("median_annual"):
            return p["benchmark"]["median_annual"] * margin, "federal award benchmark"
    return DEFAULT_ANNUAL_VALUE.get(o["target_category"], 50000) * margin, "category default"


def candidate_actions(conn, o: dict) -> list:
    st = o["funnel_stage"]
    acts = []
    has_pkg = db.one(conn, "SELECT 1 FROM bid_packages WHERE opportunity_id=?", (o["id"],))
    threads = db.one(conn, "SELECT COUNT(*) n FROM threads WHERE opportunity_id=?", (o["id"],))["n"]
    if st == "qualified":
        acts.append(("research_contractors", f"no {o['target_category']} contractors on file in {o['state']}"))
    if st == "contractors_identified":
        if crm.match(conn, o["target_category"], o["state"], 1, need_email=True) and threads == 0:
            acts.append(("draft_rfqs", "contractors with official-directory emails exist and none have been asked"))
        if db.one(conn, "SELECT 1 FROM messages m JOIN threads t ON t.id=m.thread_id WHERE t.opportunity_id=? AND m.status='ready'", (o["id"],)):
            acts.append(("send_rfqs", "RFQ drafts are ready and waiting for sending authorization"))
        acts.append(("call_contractors", "phone numbers on file from official directories"))
    if st == "quote_requested":
        acts.append(("follow_up", "follow-up due on unanswered RFQs"))
    if st in ("contractors_identified", "quote_requested", "quote_received") and not has_pkg:
        acts.append(("prepare_bid", "bid package gives the target subcontract price and the compliance gaps"))
    if st == "quote_received":
        acts.append(("prepare_bid", "a quote arrived: price the bid and re-run compliance"))
    if st in ("quote_received", "contractors_identified", "quote_requested"):
        acts.append(("verify_blockers", "registration/insurance blockers stop every bid at the finish line"))
    if st == "bid_ready":
        acts.append(("submit_bid", "compliant package ready for owner review and submission"))
    return acts


def plan(conn, limit: int = 15, now: Optional[datetime] = None) -> list:
    now = now or datetime.now(timezone.utc)
    rates = success_rates(conn)
    ranked = []
    for o in db.q(conn, """SELECT * FROM opportunities WHERE eligibility_status='candidate' AND lifecycle='open'"""):
        dl = eligibility.days_left(o["due_at"], now)
        urgency = 1.0 if dl is None else (1.6 if dl <= 10 else 1.3 if dl <= 21 else 1.0)
        value, basis = opportunity_value(conn, o)
        for action, why in candidate_actions(conn, o):
            p = rates.get(action, {}).get("rate", PRIORS[action])
            ev = round(p * value * urgency / max(1, len(STAGES) - RANK[o["funnel_stage"]]), 2)
            ranked.append({"opportunity_id": o["id"], "title": o["title"], "stage": o["funnel_stage"], "action": action,
                           "expected_value": ev, "p": round(p, 3), "p_source": "measured" if action in rates else "prior",
                           "value_basis": basis, "why": why, "days_left": dl})
    ranked.sort(key=lambda a: a["expected_value"], reverse=True)
    return ranked[:limit]


def execute(conn, a: dict, profile: dict) -> str:
    o = db.one(conn, "SELECT * FROM opportunities WHERE id=?", (a["opportunity_id"],))
    act = a["action"]
    if act == "research_contractors":
        res = crm.research(conn)
        return f"research run: {sum(v.get('new', 0) for v in res.values() if isinstance(v, dict))} new contractors"
    if act == "draft_rfqs":
        subs = crm.match(conn, o["target_category"], o["state"], 5, need_email=True)
        n = mail.create_rfqs(conn, o, subs)
        return f"{n} RFQ drafts created"
    if act == "follow_up":
        return f"{mail.schedule_followups(conn)} follow-ups drafted"
    if act == "prepare_bid":
        r = bids.prepare(conn, o["id"], profile)
        return f"bid package v{r['version']}: {r['status']}"
    raise ValueError(act)


def hand_to_owner(conn, a: dict) -> str:
    o = db.one(conn, "SELECT * FROM opportunities WHERE id=?", (a["opportunity_id"],))
    act = a["action"]
    if act == "call_contractors":
        subs = crm.match(conn, o["target_category"], o["state"], 25)
        callable_ = [c for c in subs if c["phone"]][:8]
        if callable_:
            lines = [f"- #{c['id']} {c['name']}: {c['phone']}{' · ' + c['email'] if c['email'] else ''} "
                     f"(fit {(c['fit_score'] or 0):.0f}, source {c['contact_source'] or c['source']})" for c in callable_]
            title = f"Call for quotes: lead #{o['id']} {o['title'][:60]}"
            head = f"Ask for a price on the scope in {o['source_url']} (due {o['due_at']})."
        else:
            lines = [f"- #{c['id']} {c['name']}, {c['address'] or c['city'] or c['state']} (source {c['source']})" for c in subs[:8]]
            title = f"Find contact details for subcontractors: lead #{o['id']} {o['title'][:50]}"
            head = (f"No official directory lists a phone or email for {o['target_category']} firms in {o['state']} yet. "
                    "Look these registered businesses up by name/address, then record what you find.")
        db.task(conn, title, head + "\n" + "\n".join(lines) +
                "\nRecord a quote: python3 -m caraway quote add --opportunity "
                f"{o['id']} --contractor <ID> --amount <USD> --period month|year --evidence 'call notes'", "owner", 1, o["id"])
        conn.execute("DELETE FROM tasks WHERE status='open' AND opportunity_id=? AND title LIKE 'Call for quotes:%' AND title != ?", (o["id"], title))
        return "call list handed to owner" if callable_ else "contact research handed to owner"
    if act == "send_rfqs":
        db.task(conn, "Authorize outbound email for RFQs",
                "RFQ drafts are ready. To let Caraway send them: connect Gmail (python3 -m caraway mail connect), fill sender details "
                "in config/comms_policy.local.json, set enabled=true, then python3 -m caraway mail enable. Review drafts on the Drafts tab.",
                "owner", 1)
        return "sending authorization requested"
    if act == "verify_blockers":
        leads = db.q(conn, """SELECT o.id, o.title, b.checks_json FROM opportunities o JOIN bid_packages b ON b.opportunity_id=o.id
                              WHERE o.eligibility_status='candidate' AND b.version=(SELECT MAX(version) FROM bid_packages WHERE opportunity_id=o.id)""")
        fails: dict = {}
        for l in leads:
            for c in json.loads(l["checks_json"]):
                if c["status"] == "fail" and c["check"] in ("vendor registration", "insurance"):
                    fails.setdefault(c["check"], []).append(f"#{l['id']} {l['title'][:50]}")
        detail = "\n".join(f"{k}: blocks {len(v)} leads ({'; '.join(v[:4])}{'…' if len(v) > 4 else ''})" for k, v in fails.items())
        db.task(conn, "Clear company-wide bid blockers (registrations, insurance)",
                detail + "\nThese stop every bid at the finish line no matter how good the price is.", "owner", 1)
        conn.execute("DELETE FROM tasks WHERE title LIKE 'Clear bid blockers for lead #%' AND status='open'")
        return "company-wide blocker task updated"
    if act == "submit_bid":
        db.task(conn, f"Review and submit bid for lead #{o['id']}", f"Package is compliant on all checks. Submission is owner-only. {o['source_url']}", "owner", 1, o["id"])
        return "submission handed to owner"
    raise ValueError(act)


def cycle(conn, profile: Optional[dict] = None, max_auto: int = 8) -> dict:
    profile = profile or config.load_profile()
    if compliance.paused(conn):
        return {"status": "paused"}
    update_stages(conn)
    evaluated = evaluate_decisions(conn)
    actions = plan(conn)
    done, handed, seen = [], [], set()
    for a in actions:
        key = (a["opportunity_id"], a["action"])
        if key in seen:
            continue
        seen.add(key)
        recent = db.one(conn, """SELECT 1 FROM decisions WHERE opportunity_id=? AND action=? AND status IN ('executed','handed_to_owner')""",
                        (a["opportunity_id"], a["action"]))
        if recent:
            continue  # already in flight; let it be evaluated before repeating
        if a["action"] == "research_contractors" and any(x["action"] == "research_contractors" for x in done):
            continue
        try:
            if a["action"] in AUTONOMOUS and len(done) < max_auto:
                result, status = execute(conn, a, profile), "executed"
                done.append(a)
            elif a["action"] not in AUTONOMOUS:
                result, status = hand_to_owner(conn, a), "handed_to_owner"
                handed.append(a)
            else:
                continue
        except compliance.Denied as e:
            result, status = f"denied: {e}", "denied"
        conn.execute("""INSERT INTO decisions(opportunity_id,action,stage_before,expected_value,rationale,status,executed_by,result,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                     (a["opportunity_id"], a["action"], a["stage"], a["expected_value"],
                      f"{a['why']} (p={a['p']} {a['p_source']}, value basis: {a['value_basis']})", status,
                      "caraway" if status == "executed" else "owner", result, db.now()))
    counts = update_stages(conn)
    conn.execute("INSERT INTO funnel_snapshots(taken_at,counts_json) VALUES(?,?)", (db.now(), json.dumps(counts)))
    db.audit(conn, "brain", "cycle", "", "OK", json.dumps({"executed": len(done), "handed": len(handed), "evaluated": evaluated}))
    return {"funnel": counts, "executed": [(a["action"], a["opportunity_id"]) for a in done],
            "handed_to_owner": [(a["action"], a["opportunity_id"]) for a in handed], "evaluated": evaluated}


def funnel_report(conn) -> dict:
    counts = update_stages(conn)
    cum = {}
    for s in STAGES:  # "reached at least this stage"
        cum[s] = sum(c for k, c in counts.items() if RANK[k] >= RANK[s])
    conv = {f"{a}->{b}": (round(cum[b] / cum[a], 3) if cum[a] else None) for a, b in zip(STAGES, STAGES[1:])}
    collected = db.one(conn, "SELECT COALESCE(SUM(amount),0) s FROM financial_facts WHERE fact_type='collected_cash' AND basis='actual' AND verified=1")["s"]
    return {"at_stage": counts, "reached": cum, "conversion": conv, "collected_cash_verified": collected,
            "decision_learning": success_rates(conn)}
