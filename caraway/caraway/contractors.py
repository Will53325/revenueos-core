"""Contractor research engine.

Finds real service providers in official government directories, records
every fact with its source (contractor_evidence), and keeps one CRM row per
business. Contact details come only from directories where the business
itself registered them with a government agency. Nothing is guessed or
scraped from personal pages, and an opt-out is permanent.

Verification levels recorded per fact:
  directory_listed   - appears in an official directory (business-supplied data)
  government_verified- issued/maintained by the agency (license, certification, award)
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from . import config, db, net

SUFFIX_RE = re.compile(r"\b(l\.?l\.?c|inc|corp(oration)?|co|company|ltd|l\.?p|pllc|llp|the|dba)\b\.?", re.I)
NON_SERVICE_RE = re.compile(r"landscape architect|architecture|design services|cultural resource|holding company|"
                            r"staffing|aerial photo|drone|engineering firm", re.I)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def name_key(name: str) -> str:
    s = SUFFIX_RE.sub(" ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def clean_phone(p: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D", "", p or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else None


def clean_email(e: Optional[str]) -> Optional[str]:
    e = (e or "").strip().strip(".").lower()
    return e if EMAIL_RE.match(e) else None


def clean_url(u: Optional[str]) -> Optional[str]:
    u = (u or "").strip()
    if not u or " " in u or "." not in u:
        return None
    return u if u.startswith(("http://", "https://")) else "https://" + u


def category_for(naics: Optional[str], text: str = "") -> Optional[str]:
    if naics and naics[:6] in config.TARGET_NAICS:
        return config.TARGET_NAICS[naics[:6]]
    if naics and naics[:6] == "561740":
        return "janitorial"  # carpet & upholstery cleaning
    t = (text or "").lower()
    best, cat = 0, None
    for name, kws in config.TARGET_CATEGORIES.items():
        n = sum(1 for k in kws if k in t)
        if n > best:
            best, cat = n, name
    return cat if best >= 2 else None  # one stray word ("landscapes") is not evidence


def upsert(conn, rec: dict, evidence: list) -> tuple:
    """Merge a directory record into the CRM. Returns (contractor_id, created)."""
    key = name_key(rec["name"])
    if not key:
        return None, False
    row = db.one(conn, "SELECT * FROM contractors WHERE name_key=? AND state IS ?", (key, rec.get("state")))
    if not row:
        row = db.one(conn, "SELECT * FROM contractors WHERE name=? AND state IS ?", (rec["name"], rec.get("state")))
    created = row is None
    if created:
        conn.execute("""INSERT OR IGNORE INTO contractors(name,name_key,state,city,categories,source,source_url,evidence,
                        relationship_stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?, 'identified',?,?)""",
                     (rec["name"], key, rec.get("state"), rec.get("city"), rec.get("category"), rec["source"],
                      rec.get("source_url"), rec.get("summary"), db.now(), db.now()))
        row = db.one(conn, "SELECT * FROM contractors WHERE name_key=? AND state IS ?", (key, rec.get("state"))) or \
            db.one(conn, "SELECT * FROM contractors WHERE name=? AND state IS ?", (rec["name"], rec.get("state")))
    cid = row["id"]
    # Fill blanks; never overwrite a value with nothing. Directory contact data refreshes.
    updates = {}
    for f in ("email", "phone", "website", "address", "zip", "naics", "contact_name", "description", "city",
              "license_number", "license_status", "license_expires", "uei"):
        v = rec.get(f)
        if v and (not row.get(f) or f in ("email", "phone", "license_status", "license_expires")):
            updates[f] = v
    if rec.get("category") and not row.get("categories"):
        updates["categories"] = rec["category"]
    if rec.get("certifications"):
        certs = set(filter(None, (row.get("certifications") or "").split(","))) | set(rec["certifications"])
        updates["certifications"] = ",".join(sorted(certs))
    if rec.get("email") or rec.get("phone"):
        updates["contact_source"] = rec["source"]
    if updates:
        updates["updated_at"] = db.now()
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE contractors SET {sets} WHERE id=?", (*updates.values(), cid))
    for fact, value, level in evidence:
        conn.execute("""INSERT INTO contractor_evidence(contractor_id,fact,value,source,source_url,verified_at)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(contractor_id,fact,source) DO UPDATE SET value=excluded.value,
                        verified_at=excluded.verified_at""",
                     (cid, f"{level}:{fact}", str(value)[:500], rec["source"], rec.get("source_url"), db.now()))
    return cid, created


# ------------------------------------------------------------------ directories

def nyc_sbs(conn) -> dict:
    """NYC SBS Online Directory of Certified Businesses (M/WBE, EBE, LBE)."""
    url = "https://data.cityofnewyork.us/resource/ci93-uc8s.json"
    naics = ",".join(f"'{n}'" for n in list(config.TARGET_NAICS) + ["561740"])
    kw = " OR ".join(f"upper(business_description) like '%{k.upper()}%'" for k in
                     ("JANITOR", "CUSTODIAL", "LANDSCAP", "GROUNDS MAINT", "PEST CONTROL", "EXTERMINAT", "SNOW REMOVAL"))
    rows = net.get_json(url, {"$where": f"id6_digit_naics_code in ({naics}) OR {kw}", "$limit": 3000})
    n = new = 0
    for r in rows:
        name = r.get("vendor_formal_name") or r.get("vendor_dba")
        cat = category_for(r.get("id6_digit_naics_code"), r.get("business_description", ""))
        if not name or not cat:
            continue
        certs = [c.strip() for c in (r.get("certification") or "").split(",") if c.strip()]
        rec = dict(name=name, state="NY", city=r.get("city"), category=cat, source="nyc_sbs_certified",
                   source_url=f"{url}?account_number={r.get('account_number')}",
                   phone=clean_phone(r.get("telephone")), website=clean_url(r.get("website")),
                   address=", ".join(x for x in (r.get("address1"), r.get("city"), r.get("state"), r.get("zip")) if x),
                   zip=r.get("zip"), naics=r.get("id6_digit_naics_code"),
                   contact_name=" ".join(x for x in (r.get("first_name"), r.get("last_name")) if x) or None,
                   description=(r.get("business_description") or "")[:1500],
                   certifications=[f"NYC {c}" for c in certs],
                   summary="NYC SBS certified business directory")
        ev = [("listed_in", "NYC SBS Certified Business List", "directory_listed")]
        for c in certs:
            ev.append((f"certification_{c}", f"NYC {c}, renewal {r.get('cert_renewal_date')}", "government_verified"))
        for i, k in ((1, "largest_value_of_contract"), (2, "value_of_contract_job_exp_2"), (3, "value_of_contract_job_exp_3")):
            client = r.get(f"name_of_client_job_exp_{i}")
            if client:
                ev.append((f"reported_job_{i}", f"{client}: {(r.get(f'description_of_work_job_exp_{i}') or '')[:200]} "
                           f"(value {r.get(k)}, self-performed {r.get(f'percent_self_performed_job_exp_{i}')}%)", "directory_listed"))
        if r.get("enrolled_in_passport"):
            ev.append(("nyc_passport_enrolled", r["enrolled_in_passport"], "directory_listed"))
        _, created = upsert(conn, rec, ev)
        n += 1; new += int(created)
    return {"records": n, "new": new}


def delaware_osd(conn) -> dict:
    """Delaware Office of Supplier Diversity certified firms (includes email)."""
    url = "https://data.delaware.gov/resource/8dxf-6ahp.json"
    rows = net.get_json(url, {"$limit": 5000})
    n = new = 0
    for r in rows:
        codes = [r.get(f"naicscode{i}") for i in range(1, 32) if r.get(f"naicscode{i}")]
        desc = r.get("description") or ""
        primary = next((category_for(c) for c in codes[:3] if category_for(c)), None)
        text_cat = category_for(None, desc)
        any_code = next((category_for(c) for c in codes if category_for(c)), None)
        cat = primary or text_cat or (any_code if any_code and category_for(None, desc + " " + desc) == any_code else None)
        if NON_SERVICE_RE.search(desc[:400] + " " + (r.get("name") or "")):
            cat = None
        if not cat or not r.get("name"):
            continue
        certs = [lbl for k, lbl in (("ct_mbe", "MBE"), ("ct_wbe", "WBE"), ("ct_vobe", "VOBE"), ("ct_sdvobe", "SDVOBE"))
                 if (r.get(k) or "").strip().upper() == "YES"]
        rec = dict(name=r["name"].strip(), state="DE", city=r.get("city"), category=cat, source="delaware_osd",
                   source_url=f"{url}?certificatenumber={r.get('certificatenumber')}",
                   email=clean_email(r.get("email")), phone=clean_phone(r.get("phonenumber")),
                   address=", ".join(x.strip() for x in (r.get("address"), r.get("city"), r.get("zipcode")) if x),
                   zip=r.get("zipcode"), naics=",".join(codes[:6]), contact_name=r.get("primarycontactname"),
                   description=(r.get("description") or "")[:1500], certifications=[f"DE {c}" for c in certs],
                   summary="Delaware Office of Supplier Diversity directory")
        ev = [("listed_in", "Delaware OSD certified directory", "directory_listed"),
              ("osd_certificate", r.get("certificatenumber"), "government_verified")]
        ev += [(f"certification_{c}", f"Delaware {c}", "government_verified") for c in certs]
        _, created = upsert(conn, rec, ev)
        n += 1; new += int(created)
    return {"records": n, "new": new}


def ny_pesticide(conn) -> dict:
    """NY DEC registered pesticide businesses: a legal prerequisite for commercial pest/turf work in NY."""
    url = "https://data.ny.gov/resource/h8u2-6ejg.json"
    today = date.today().isoformat()
    rows = net.get_json(url, {"$where": f"registration_expiration_date >= '{today}' AND pesticide_category_desc in "
                                        "('Structural & Rodent','Ornamental & Turf','Termite','Public Health')",
                              "$limit": 5000})
    n = new = 0
    for r in rows:
        desc = r.get("pesticide_category_desc") or ""
        cat = "landscaping" if "Turf" in desc else "pest_control"
        rec = dict(name=(r.get("business_agency_name") or "").title(), state="NY", city=(r.get("city") or "").title(),
                   category=cat, source="ny_dec_pesticide", source_url=f"{url}?registration_number={r.get('registration_number')}",
                   zip=r.get("zip_code"), license_number=f"NY DEC pesticide business reg {r.get('registration_number')}",
                   license_status="active", license_expires=(r.get("registration_expiration_date") or "")[:10],
                   summary="NY DEC registered pesticide business")
        ev = [("pesticide_business_registration", f"#{r.get('registration_number')} {desc}, expires "
               f"{(r.get('registration_expiration_date') or '')[:10]}", "government_verified")]
        _, created = upsert(conn, rec, ev)
        n += 1; new += int(created)
    return {"records": n, "new": new}


def la_active_businesses(conn, per_naics: int = 250) -> dict:
    """City of LA active business tax registrations in service NAICS codes (address only, no contact data)."""
    url = "https://data.lacity.org/resource/6rrh-rzua.json"
    n = new = 0
    for code in ("561720", "561730", "561710", "561790"):
        rows = net.get_json(url, {"naics": code, "$order": "location_start_date ASC", "$limit": per_naics})
        for r in rows:
            name = r.get("business_name") or r.get("dba_name")
            if not name:
                continue
            rec = dict(name=name.title(), state="CA", city=(r.get("city") or "").title(), category=category_for(code),
                       source="la_active_business", source_url=f"{url}?location_account={r.get('location_account')}",
                       address=", ".join(x for x in (r.get("street_address"), r.get("city"), r.get("zip_code")) if x),
                       zip=r.get("zip_code"), naics=code, summary="LA City active business tax registration")
            ev = [("active_business_registration", f"LA City account {r.get('location_account')} since "
                   f"{(r.get('location_start_date') or '')[:10]}", "government_verified")]
            _, created = upsert(conn, rec, ev)
            n += 1; new += int(created)
    return {"records": n, "new": new}


DIRECTORIES = {
    "nyc_sbs_certified": nyc_sbs,
    "delaware_osd": delaware_osd,
    "ny_dec_pesticide": ny_pesticide,
    "la_active_business": la_active_businesses,
}


def score_all(conn) -> int:
    """Rank contractors as subcontract candidates from recorded evidence only."""
    rows = db.q(conn, "SELECT id, email, phone, website, certifications, federal_award_count, license_status, do_not_contact FROM contractors")
    ev_counts = {r["contractor_id"]: r["n"] for r in db.q(conn, """SELECT contractor_id, COUNT(*) n FROM contractor_evidence
                                                                  WHERE fact LIKE 'directory_listed:reported_job%' GROUP BY contractor_id""")}
    for r in rows:
        s = 0.0
        s += 25 if r["email"] else 0
        s += 15 if r["phone"] else 0
        s += 5 if r["website"] else 0
        s += 10 if r["certifications"] else 0
        s += min(20, 5 * (r["federal_award_count"] or 0))
        s += min(15, 5 * ev_counts.get(r["id"], 0))
        s += 10 if r["license_status"] == "active" else 0
        if r["do_not_contact"]:
            s = 0
        conn.execute("UPDATE contractors SET fit_score=? WHERE id=?", (s, r["id"]))
    return len(rows)


def research(conn, only: Optional[list] = None) -> dict:
    out = {}
    for name, fn in DIRECTORIES.items():
        if only and name not in only:
            continue
        try:
            out[name] = fn(conn)
        except net.NetError as e:
            db.log_error(conn, f"contractors:{name}", str(e))
            out[name] = {"error": str(e)}
    out["scored"] = score_all(conn)
    db.audit(conn, "contractor_research", "research", ",".join(only or DIRECTORIES), "OK", db.dumps(out))
    return out


def match(conn, category: str, state: Optional[str], limit: int = 5, need_email: bool = False) -> list:
    sql = """SELECT * FROM contractors WHERE categories=? AND (state IS ? OR ? IS NULL) AND do_not_contact=0
             AND (email IS NULL OR email NOT IN (SELECT address FROM opt_outs))"""
    if need_email:
        sql += " AND email IS NOT NULL"
    return db.q(conn, sql + " ORDER BY fit_score DESC, federal_award_count DESC LIMIT ?", (category, state, state, limit))
