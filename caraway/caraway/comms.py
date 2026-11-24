"""Communications agent — drafts only in v0.1.

Drafts are stored for owner review. Sending goes through
compliance.authorize('send_email'), which denies until the owner connects a
dedicated business email account and enables sending; even then this
version does not implement sending.
"""
from __future__ import annotations

from . import compliance, db


def draft_rfq(conn, opp: dict, contractor: dict) -> None:
    compliance.authorize(conn, "draft_message", f"opp:{opp['id']}")
    subject = f"Request for quote: {opp['title'][:80]} ({opp.get('solicitation_number') or 'no solicitation #'})"
    body = (
        f"Hello {contractor['name']} team,\n\n"
        f"Caraway Housing LLC is evaluating a public solicitation and is looking for a qualified "
        f"subcontractor partner:\n\n"
        f"  Opportunity: {opp['title']}\n"
        f"  Agency: {opp.get('agency_name') or 'see notice'}\n"
        f"  Location: {opp.get('location') or 'see notice'}\n"
        f"  Response due: {opp.get('due_at') or 'see notice'}\n"
        f"  Official notice: {opp.get('source_url')}\n\n"
        f"If this work fits your crew, could you share:\n"
        f"  1. A price for the scope in the solicitation (we can send the documents)\n"
        f"  2. Your insurance certificate and any licenses that apply\n"
        f"  3. Similar work you have performed (references welcome)\n\n"
        f"This is an inquiry only. No work is awarded and no commitment is made by either party.\n\n"
        f"Thank you,\nCaraway Housing LLC\n[owner name and business contact go here once a business email is set up]"
    )
    conn.execute("""INSERT OR IGNORE INTO comm_drafts(kind,to_name,to_address,subject,body,opportunity_id,contractor_id,status,created_at)
                    VALUES('rfq',?,?,?,?,?,?,'draft',?)""",
                 (contractor["name"], None, subject, body, opp["id"], contractor["id"], db.now()))


def send(conn, draft_id: int) -> None:
    compliance.authorize(conn, "send_email", f"draft:{draft_id}")  # raises in v0.1
