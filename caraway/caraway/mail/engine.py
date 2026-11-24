"""Outreach engine: RFQ threads, follow-ups, dispatch through the policy gate,
inbound processing and conversation memory.

Message lifecycle: draft -> ready (passes content checks) -> sent | blocked.
Nothing reaches a transport unless compliance.authorize_send() returns OK.
Inbound replies are untrusted data: they are classified and mined for
prices, never executed as instructions.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy as email_policy
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from .. import compliance, config, db
from . import classify, templates, transport

POLICY_PATH = config.PROJECT_ROOT / "config" / "comms_policy.json"
POLICY_LOCAL = config.PROJECT_ROOT / "config" / "comms_policy.local.json"
SOURCE_LABELS = {"delaware_osd": "State of Delaware Office of Supplier Diversity directory",
                 "nyc_sbs_certified": "NYC SBS certified business directory"}
CATEGORY_LABELS = {"janitorial": "janitorial / cleaning", "landscaping": "landscaping / grounds",
                   "facility_maintenance": "facility maintenance", "moving_hauling": "moving / hauling",
                   "pest_control": "pest control"}
OFFICIAL_CONTACT_SOURCES = set(SOURCE_LABELS)


def load_policy() -> dict:
    p = POLICY_LOCAL if POLICY_LOCAL.exists() else POLICY_PATH
    return json.loads(p.read_text())


def _thread(conn, opp_id: int, c_id: int, kind: str, subject: str) -> int:
    conn.execute("""INSERT OR IGNORE INTO threads(opportunity_id,contractor_id,kind,status,subject,created_at,last_activity_at)
                    VALUES(?,?,?,?,?,?,?)""", (opp_id, c_id, kind, "drafted", subject, db.now(), db.now()))
    return db.one(conn, "SELECT id FROM threads WHERE opportunity_id=? AND contractor_id=? AND kind=?", (opp_id, c_id, kind))["id"]


def _add_message(conn, thread_id: int, kind: str, to: Optional[str], subject: str, text: str) -> int:
    cur = conn.execute("""INSERT INTO messages(thread_id,direction,status,to_address,subject,body_text,body_html,classification,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?)""",
                       (thread_id, "outbound", "draft", to, subject, text, templates.to_html(text), kind, db.now()))
    return cur.lastrowid


def create_rfqs(conn, opp: dict, contractors: list, reply_days: int = 4) -> int:
    """Draft one RFQ thread per contractor. Idempotent per (opportunity, contractor)."""
    pol = load_policy()
    n = 0
    due = opp.get("due_at") or ""
    reply_by = (datetime.now(timezone.utc) + timedelta(days=reply_days)).strftime("%B %d")
    for c in contractors:
        if db.one(conn, "SELECT id FROM threads WHERE opportunity_id=? AND contractor_id=? AND kind='rfq'", (opp["id"], c["id"])):
            continue
        c = dict(c, contact_source_label=SOURCE_LABELS.get(c.get("contact_source"), "public business directory"),
                 category_label=CATEGORY_LABELS.get(c.get("categories"), "service"))
        subject, text = templates.rfq(opp, c, pol["sender"], reply_by)
        tid = _thread(conn, opp["id"], c["id"], "rfq", subject)
        _add_message(conn, tid, "rfq", c.get("email"), subject, text)
        conn.execute("INSERT INTO contractor_interactions(contractor_id,opportunity_id,kind,direction,summary,created_at) VALUES(?,?,?,?,?,?)",
                     (c["id"], opp["id"], "rfq_drafted", "outbound", subject, db.now()))
        n += 1
    return n


def content_checks(conn, msg: dict, contractor: dict, pol: dict) -> list:
    """Problems that make a message unsendable regardless of authorization."""
    problems = []
    s = pol["sender"]
    for f in ("name", "email", "physical_address"):
        if not s.get(f):
            problems.append(f"sender {f} missing in comms policy")
    if not msg.get("to_address"):
        problems.append("no email address on file for this contractor")
    if msg.get("to_address") and db.one(conn, "SELECT 1 FROM opt_outs WHERE address=?", (msg["to_address"].lower(),)):
        problems.append("recipient opted out")
    if contractor.get("do_not_contact"):
        problems.append("contractor marked do-not-contact")
    if pol.get("require_contact_source_official") and contractor.get("contact_source") not in OFFICIAL_CONTACT_SOURCES:
        problems.append("email not from an official directory")
    sent = db.one(conn, """SELECT COUNT(*) n FROM messages m JOIN threads t ON t.id=m.thread_id
                           WHERE t.contractor_id=? AND m.direction='outbound' AND m.status='sent'""", (contractor["id"],))["n"]
    if sent >= pol.get("per_contractor_max_messages", 4):
        problems.append("per-contractor message cap reached")
    if "[" in (msg.get("body_text") or "") and "set in config" in msg.get("body_text", ""):
        problems.append("template placeholders not filled")
    return problems


def prepare(conn) -> dict:
    """Re-render drafts with the current sender details and mark them ready or blocked."""
    pol = load_policy()
    out = {"ready": 0, "blocked": 0}
    rows = db.q(conn, """SELECT m.*, t.contractor_id, t.opportunity_id, t.kind tkind FROM messages m JOIN threads t ON t.id=m.thread_id
                         WHERE m.direction='outbound' AND m.status IN ('draft','ready','blocked')""")
    for m in rows:
        c = db.one(conn, "SELECT * FROM contractors WHERE id=?", (m["contractor_id"],))
        if m["classification"] in ("rfq", "followup") and m["status"] != "sent":
            m = _rerender(conn, m, c, pol)
        problems = content_checks(conn, m, c, pol)
        status = "blocked" if problems else "ready"
        conn.execute("UPDATE messages SET status=?, classification_detail=? WHERE id=?",
                     (status, "; ".join(problems) or None, m["id"]))
        if problems and m.get("approved_by_owner"):
            conn.execute("UPDATE messages SET approved_by_owner=0 WHERE id=?", (m["id"],))  # approval doesn't survive a failed check
        out[status] += 1
    return out


def _rerender(conn, m: dict, c: dict, pol: dict) -> dict:
    """Unsent drafts always reflect the current sender details and contact data."""
    opp = db.one(conn, "SELECT * FROM opportunities WHERE id=?", (m["opportunity_id"],))
    cc = dict(c, contact_source_label=SOURCE_LABELS.get(c.get("contact_source"), "public business directory"),
              category_label=CATEGORY_LABELS.get(c.get("categories"), "service"))
    if m["classification"] == "rfq":
        reply_by = (datetime.now(timezone.utc) + timedelta(days=4)).strftime("%B %d")
        subject, text = templates.rfq(opp, cc, pol["sender"], reply_by)
    else:
        n = db.one(conn, "SELECT followups_sent n FROM threads WHERE id=?", (m["thread_id"],))["n"] or 1
        subject, text = templates.followup(opp, cc, pol["sender"], n)
    conn.execute("UPDATE messages SET subject=?, body_text=?, body_html=?, to_address=? WHERE id=?",
                 (subject, text, templates.to_html(text), c.get("email"), m["id"]))
    return dict(m, subject=subject, body_text=text, body_html=templates.to_html(text), to_address=c.get("email"))


def dispatch(conn, transport_override=None, now: Optional[datetime] = None) -> dict:
    """Send ready messages that the standing policy authorizes. Returns counts and denial reasons."""
    pol = load_policy()
    prepare(conn)
    tr = transport_override or transport.get_transport()
    out = {"sent": 0, "held": 0, "reasons": {}}
    sent_today = db.one(conn, "SELECT COUNT(*) n FROM messages WHERE status='sent' AND sent_at >= ?",
                        ((now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT00:00:00"),))["n"]
    for m in db.q(conn, """SELECT m.*, t.contractor_id, t.opportunity_id FROM messages m JOIN threads t ON t.id=m.thread_id
                           WHERE m.status='ready' ORDER BY m.id"""):
        ok, why = compliance.authorize_send(conn, pol, m["classification"], tr, sent_today, now, bool(m.get("approved_by_owner")))
        if not ok:
            out["held"] += 1
            out["reasons"][why] = out["reasons"].get(why, 0) + 1
            continue
        mime = transport.build_mime(pol["sender"], m["to_address"], m["subject"], m["body_text"], m["body_html"], m["in_reply_to"])
        res = tr.send(mime)
        conn.execute("UPDATE messages SET status='sent', sent_at=?, message_id=?, from_address=? WHERE id=?",
                     (db.now(), mime["Message-ID"], pol["sender"]["email"], m["id"]))
        days = pol.get("followup_days", [3, 7])
        conn.execute("""UPDATE threads SET status='awaiting_reply', last_activity_at=?, next_followup_at=?,
                        gmail_thread_id=COALESCE(gmail_thread_id, ?) WHERE id=?""",
                     (db.now(), (datetime.now(timezone.utc) + timedelta(days=days[0])).replace(microsecond=0).isoformat(),
                      res.get("thread"), m["thread_id"]))
        conn.execute("UPDATE contractors SET relationship_stage='contacted', updated_at=? WHERE id=? AND relationship_stage='identified'",
                     (db.now(), m["contractor_id"]))
        db.audit(conn, "comms", "send_email", m["to_address"], "SENT", f"{m['classification']} via {tr.name}")
        sent_today += 1
        out["sent"] += 1
    return out


def schedule_followups(conn, now: Optional[datetime] = None) -> int:
    """Draft follow-ups for threads with no reply past their follow-up date."""
    pol = load_policy()
    now = now or datetime.now(timezone.utc)
    n = 0
    for t in db.q(conn, """SELECT * FROM threads WHERE status='awaiting_reply' AND next_followup_at <= ? AND followups_sent < ?""",
                  (now.replace(microsecond=0).isoformat(), pol.get("max_followups", 2))):
        opp = db.one(conn, "SELECT * FROM opportunities WHERE id=?", (t["opportunity_id"],))
        c = db.one(conn, "SELECT * FROM contractors WHERE id=?", (t["contractor_id"],))
        if not opp or opp.get("eligibility_status") != "candidate":
            conn.execute("UPDATE threads SET status='closed_opportunity_gone' WHERE id=?", (t["id"],))
            continue
        last = db.one(conn, "SELECT message_id FROM messages WHERE thread_id=? AND direction='outbound' AND status='sent' ORDER BY id DESC LIMIT 1", (t["id"],))
        k = t["followups_sent"] + 1
        subject, text = templates.followup(opp, c, pol["sender"], k)
        mid = _add_message(conn, t["id"], "followup", c.get("email"), subject, text)
        conn.execute("UPDATE messages SET in_reply_to=? WHERE id=?", (last and last["message_id"], mid))
        days = pol.get("followup_days", [3, 7])
        nxt = now + timedelta(days=days[min(k, len(days) - 1)] - days[min(k - 1, len(days) - 1)] or 4)
        conn.execute("UPDATE threads SET followups_sent=?, next_followup_at=? WHERE id=?",
                     (k, nxt.replace(microsecond=0).isoformat(), t["id"]))
        n += 1
    return n


def ingest_reply(conn, raw: bytes) -> dict:
    """Store an inbound email, match it to a thread, classify it, act on it."""
    msg = message_from_bytes(raw, policy=email_policy.default)
    mid = (msg.get("Message-ID") or "").strip()
    if mid and db.one(conn, "SELECT id FROM messages WHERE message_id=?", (mid,)):
        return {"status": "duplicate"}
    frm = parseaddr(msg.get("From", ""))[1].lower()
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    refs = " ".join(filter(None, [msg.get("In-Reply-To"), msg.get("References")]))
    thread = None
    for ref in refs.split():
        hit = db.one(conn, "SELECT thread_id FROM messages WHERE message_id=?", (ref.strip(),))
        if hit:
            thread = db.one(conn, "SELECT * FROM threads WHERE id=?", (hit["thread_id"],))
            break
    if not thread:
        c = db.one(conn, "SELECT id FROM contractors WHERE lower(email)=?", (frm,))
        if c:
            thread = db.one(conn, "SELECT * FROM threads WHERE contractor_id=? ORDER BY last_activity_at DESC LIMIT 1", (c["id"],))
    result = classify.classify(msg.get("Subject", ""), text, frm)
    label = result["label"]
    cur = conn.execute("""INSERT INTO messages(thread_id,direction,status,from_address,subject,body_text,message_id,in_reply_to,
                          classification,extracted_json,received_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (thread and thread["id"], "inbound", "received", frm, msg.get("Subject"), text[:20000], mid or None,
                        msg.get("In-Reply-To"), label, db.dumps(result), db.now(), db.now()))
    out = {"status": "stored", "label": label, "thread_id": thread and thread["id"]}
    if label == "unsubscribe":
        conn.execute("INSERT OR IGNORE INTO opt_outs(address,reason,created_at) VALUES(?,?,?)", (frm, "reply", db.now()))
        conn.execute("UPDATE contractors SET do_not_contact=1, relationship_stage='do_not_contact' WHERE lower(email)=?", (frm,))
    if not thread:
        out["status"] = "unmatched"
        return out
    new_status = {"bounce": "bounced", "unsubscribe": "closed_opted_out", "declined": "declined", "quote_provided": "quoted",
                  "interested": "engaged", "question": "engaged", "out_of_office": "awaiting_reply"}.get(label, "replied")
    conn.execute("UPDATE threads SET status=?, last_activity_at=?, next_followup_at=CASE WHEN ?='awaiting_reply' THEN next_followup_at ELSE NULL END WHERE id=?",
                 (new_status, db.now(), new_status, thread["id"]))
    stage = {"declined": "declined", "quote_provided": "quoted", "interested": "responded", "question": "responded"}.get(label)
    if stage:
        conn.execute("UPDATE contractors SET relationship_stage=?, updated_at=? WHERE id=?", (stage, db.now(), thread["contractor_id"]))
    conn.execute("INSERT INTO contractor_interactions(contractor_id,opportunity_id,kind,direction,summary,created_at) VALUES(?,?,?,?,?,?)",
                 (thread["contractor_id"], thread["opportunity_id"], f"reply_{label}", "inbound", (msg.get("Subject") or "")[:200], db.now()))
    if label == "quote_provided":
        best = max(result["amounts"], key=lambda a: a["amount"])
        conn.execute("""INSERT INTO quotes(contractor_id,opportunity_id,amount,period,scope,received_at,status,evidence,message_id,basis,verified)
                        VALUES(?,?,?,?,?,?,?,?,?,?,0)""",
                     (thread["contractor_id"], thread["opportunity_id"], best["amount"], best["period"],
                      "as stated in email; scope assumptions unconfirmed", db.now(), "received",
                      f"email {mid}: '{best['text']}'", cur.lastrowid, "email_extracted"))
        out["quote"] = best
        _maybe_negotiate(conn, thread, best)
    return out


def _maybe_negotiate(conn, thread: dict, quote: dict) -> None:
    """Draft (never auto-accept) a request for best price when policy allows and the quote is above target."""
    pol = load_policy()
    neg = pol.get("negotiation", {})
    if not neg.get("enabled"):
        return
    rounds = db.one(conn, "SELECT COUNT(*) n FROM messages WHERE thread_id=? AND classification='negotiation'", (thread["id"],))["n"]
    if rounds >= neg.get("max_rounds", 1):
        return
    target = db.one(conn, """SELECT json_extract(pricing_json,'$.target_subcontract_cost') t FROM bid_packages
                             WHERE opportunity_id=? ORDER BY version DESC LIMIT 1""", (thread["opportunity_id"],))
    if not target or not target["t"] or quote["amount"] <= target["t"] * (1 + neg.get("ask_for_best_price_when_above_target_pct", 10) / 100):
        return
    opp = db.one(conn, "SELECT * FROM opportunities WHERE id=?", (thread["opportunity_id"],))
    c = db.one(conn, "SELECT * FROM contractors WHERE id=?", (thread["contractor_id"],))
    subject, text = templates.negotiation(opp, c, pol["sender"], quote["amount"])
    _add_message(conn, thread["id"], "negotiation", c.get("email"), subject, text)


def import_eml_dir(conn, folder: str) -> list:
    """Manual inbound path: drop .eml files exported from any mail client into a folder."""
    return [dict(ingest_reply(conn, p.read_bytes()), file=p.name) for p in sorted(Path(folder).glob("*.eml"))]


def poll_gmail(conn) -> dict:
    tr = transport.get_transport()
    if not isinstance(tr, transport.GmailTransport):
        return {"status": "gmail_not_connected"}
    n = 0
    for gid in tr.list_inbound():
        res = ingest_reply(conn, tr.fetch_raw(gid))
        n += res["status"] != "duplicate"
    return {"status": "ok", "new": n}


def approve(conn, message_ids: list) -> int:
    """Owner approval for specific ready messages (used when a kind isn't in auto_send_kinds)."""
    n = 0
    for mid in message_ids:
        cur = conn.execute("UPDATE messages SET approved_by_owner=1 WHERE id=? AND status='ready'", (mid,))
        n += cur.rowcount
        if cur.rowcount:
            db.audit(conn, "owner", "approve_message", str(mid), "APPROVED")
    return n
