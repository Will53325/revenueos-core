"""Executive layer: turns the database state into the next concrete actions.

It proposes; compliance disposes. Every decision is written to agent_actions.
Human tasks are only created for things software genuinely cannot do.
"""
from __future__ import annotations

import json

from . import comms, compliance, config, db

PREREQS = [
    (lambda p: p["registrations"].get("sam_gov_active") != "verified", 1,
     "Verify or complete SAM.gov entity registration",
     "Federal bids require an active SAM.gov registration (free at sam.gov; beware paid look-alike services). "
     "Record the UEI in config/owner_profile.local.json and set sam_gov_active to 'verified'."),
    (lambda p: not __import__("os").environ.get(config.SAM_API_KEY_ENV), 1,
     "Get a free SAM.gov public API key",
     "Unlocks the federal opportunity feed, where most janitorial and grounds work is posted. "
     "sam.gov > sign in > Account Details > API key. Add it to the launchd plist (README, 'Enable SAM.gov')."),
    (lambda p: p["finance"].get("business_bank_account") != "established", 1,
     "Open a business bank account for Caraway Housing LLC",
     "Needed to receive government payments (federal payment goes by EFT to the account in SAM.gov)."),
    (lambda p: p["insurance"].get("general_liability") != "verified", 2,
     "Get general liability and workers' comp insurance quotes",
     "Almost every services solicitation requires a certificate of insurance. Record the coverage limits once bound."),
    (lambda p: not (p.get("home_base") or {}).get("state"), 2,
     "Set Caraway's home base and travel radius",
     "Geography is currently unknown, so leads cannot be ranked by distance. Fill home_base in owner_profile.local.json."),
    (lambda p: p["registrations"].get("nyc_passport_vendor_account") != "verified", 3,
     "Decide whether to pursue NYC work (PASSPort vendor account)",
     "NYC solicitations are viewed and answered in PASSPort, which needs a vendor account. Only worth it if NYC is in range."),
    (lambda p: p["certifications"].get("sdvosb") == "unverified", 2,
     "Check SDVOSB/VOSB certification eligibility (SBA VetCert)",
     "Many federal janitorial/grounds contracts are set aside for service-disabled veteran-owned firms. "
     "Caraway must not claim this status until SBA VetCert certifies it."),
]


def plan(conn, profile: dict) -> dict:
    out = {"tasks": 0, "drafts": 0}
    for cond, prio, title, detail in PREREQS:
        try:
            needed = cond(profile)
        except (KeyError, TypeError):
            needed = True
        if needed:
            compliance.authorize(conn, "create_task", title)
            db.task(conn, title, detail, "owner", prio)
            out["tasks"] += 1
        else:
            conn.execute("UPDATE tasks SET status='done', updated_at=? WHERE title=? AND status='open'", (db.now(), title))

    leads = db.q(conn, """SELECT * FROM opportunities WHERE eligibility_status='candidate'
                          ORDER BY score DESC LIMIT 10""")
    for o in leads:
        title = f"Review lead #{o['id']}: {o['title'][:70]}"
        blockers = json.loads(o["blockers_json"] or "[]")
        db.task(conn, title, f"{o['next_action']}\nDue: {o['due_at'] or 'not firm'}\nSource: {o['source_url']}\n"
                f"Open blockers: {len(blockers)}", "owner", 2, o["id"])
        out["tasks"] += 1
        conn.execute("INSERT OR IGNORE INTO pursuits(opportunity_id,stage,updated_at) VALUES(?,?,?)",
                     (o["id"], "identified", db.now()))
        # Subcontractor matching: firms with federal awards in the same category and state.
        subs = db.q(conn, """SELECT * FROM contractors WHERE categories=? AND state=?
                             ORDER BY federal_award_count DESC LIMIT 3""", (o["target_category"], o["state"]))
        for c in subs:
            comms.draft_rfq(conn, o, c)
            out["drafts"] += 1
        if not subs:
            db.task(conn, f"Find subcontractors for lead #{o['id']}",
                    f"No {o['target_category']} firms in {o['state']} in the contractor database yet. "
                    "Search the state's licensing board and the SBA Dynamic Small Business Search.", "system", 3, o["id"])
    # Close review tasks for leads that dropped out (expired, amended out of scope).
    conn.execute("""UPDATE tasks SET status='obsolete', updated_at=? WHERE status='open' AND opportunity_id IS NOT NULL
                    AND opportunity_id NOT IN (SELECT id FROM opportunities WHERE eligibility_status='candidate')""", (db.now(),))
    db.audit(conn, "executive", "plan", "", "OK", json.dumps(out))
    return out
