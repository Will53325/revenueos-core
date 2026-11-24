"""USAspending.gov API — official federal award data. No key needed.

Used for intelligence, never presented as open opportunities:
  * award_intel: who wins janitorial / grounds / facility contracts, where, for how much
  * recompete_forecasts: contracts ending in the next ~9 months (likely re-solicited)
  * contractors: firms that have actually performed this work (teaming / subcontract leads)
"""
from __future__ import annotations

from datetime import date, timedelta

from .. import config, db, net

NAME = "usaspending"
DESCRIPTION = "USAspending.gov federal award history (official API v2)"
REQUIRES_KEY = False
ENDPOINT = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
FIELDS = ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "Awarding Sub Agency",
          "Start Date", "End Date", "Place of Performance State Code", "Description",
          "generated_internal_id", "NAICS", "Recipient UEI"]
AWARD_URL = "https://www.usaspending.gov/award/{gid}"
# Small enough for a new prime to plausibly manage with subcontractors.
SMALL_AWARD_MAX = 1_000_000


def _search(naics: str, start: str, end: str, pages: int = 2, sort: str = "Award Amount") -> list:
    out = []
    for page in range(1, pages + 1):
        data = net.post_json(ENDPOINT, {
            "filters": {"award_type_codes": ["A", "B", "C", "D"],
                        "naics_codes": {"require": [naics]},
                        "time_period": [{"start_date": start, "end_date": end}]},
            "fields": FIELDS, "limit": 100, "page": page, "sort": sort, "order": "desc"})
        out.extend(data.get("results") or [])
        if not (data.get("page_metadata") or {}).get("hasNext"):
            break
    return out


def sync(conn) -> dict:
    today = date.today()
    start = (today - timedelta(days=365 * 2)).isoformat()
    stats = {"awards": 0, "forecasts": 0, "contractors": 0}
    for naics, cat in sorted(config.TARGET_NAICS.items()):
        for r in _search(naics, start, today.isoformat()):
            gid = r.get("generated_internal_id")
            amount = r.get("Award Amount") or 0
            url = AWARD_URL.format(gid=gid) if gid else None
            row = (r.get("Award ID"), gid, r.get("Recipient Name"), r.get("Recipient UEI"), amount,
                   r.get("Awarding Agency"), r.get("Awarding Sub Agency"), r.get("Start Date"),
                   r.get("End Date"), r.get("Place of Performance State Code"),
                   (r.get("Description") or "")[:1000], naics, cat, url, db.now(), db.now())
            conn.execute("""INSERT INTO award_intel(award_id,generated_id,recipient,recipient_uei,amount,agency,
                 sub_agency,start_date,end_date,state,description,naics,target_category,url,first_seen_at,last_seen_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(award_id) DO UPDATE SET amount=excluded.amount,end_date=excluded.end_date,
                   last_seen_at=excluded.last_seen_at""", row)
            stats["awards"] += 1
            end = r.get("End Date") or ""
            if end and today.isoformat() <= end <= (today + timedelta(days=270)).isoformat():
                small = 0 < amount <= SMALL_AWARD_MAX
                fit = (60 if small else 25) + (20 if cat in ("janitorial", "landscaping") else 5)
                conn.execute("""INSERT INTO recompete_forecasts(award_id,recipient,amount,agency,sub_agency,end_date,
                     state,description,naics,target_category,url,fit_score,note,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(award_id) DO UPDATE SET end_date=excluded.end_date, amount=excluded.amount,
                       fit_score=excluded.fit_score, updated_at=excluded.updated_at""",
                     (r.get("Award ID"), r.get("Recipient Name"), amount, r.get("Awarding Agency"),
                      r.get("Awarding Sub Agency"), end, r.get("Place of Performance State Code"),
                      (r.get("Description") or "")[:600], naics, cat, url, fit,
                      "Forecast only: current contract ends soon. Watch SAM.gov for the re-solicitation "
                      "or a sources-sought notice. Not an open solicitation.", db.now()))
                stats["forecasts"] += 1
            if r.get("Recipient Name") and 0 < amount <= SMALL_AWARD_MAX:
                cur = conn.execute("""INSERT INTO contractors(name,uei,state,categories,source,source_url,evidence,
                      federal_award_count,federal_award_total,created_at,updated_at)
                      VALUES(?,?,?,?,?,?,?,1,?,?,?)
                      ON CONFLICT(name,state) DO UPDATE SET updated_at=excluded.updated_at""",
                      (r["Recipient Name"], r.get("Recipient UEI"), r.get("Place of Performance State Code"), cat,
                       NAME, url, f"Federal award {r.get('Award ID')} (${amount:,.0f}, NAICS {naics})",
                       amount, db.now(), db.now()))
                stats["contractors"] += cur.rowcount
    # Recompute contractor award totals from stored intel (idempotent).
    conn.execute("""UPDATE contractors SET
        federal_award_count=(SELECT COUNT(*) FROM award_intel a WHERE a.recipient=contractors.name AND a.state IS contractors.state),
        federal_award_total=(SELECT COALESCE(SUM(amount),0) FROM award_intel a WHERE a.recipient=contractors.name AND a.state IS contractors.state)
        WHERE source='usaspending'""")
    conn.execute("DELETE FROM recompete_forecasts WHERE end_date < ?", (today.isoformat(),))
    return stats
