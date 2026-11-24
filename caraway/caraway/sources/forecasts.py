"""Procurement forecasts and planned buys. Stored in forecast_opportunities,
never in opportunities: a forecast is a reason to prepare, not something to bid.

Also reads Montgomery County's active-contract register to find county
contracts expiring within ~9 months (local recompete signals) and records
their incumbents as contractor leads with government-verified evidence.
"""
from __future__ import annotations

from datetime import date, timedelta

from .. import contractors, db, net
from ..extract import classify_category

NAME = "forecasts"
DESCRIPTION = "Procurement forecasts: Cook County IL FY2026 buying plan, San Diego County forecast, NYC M/WBE upcoming, Montgomery County expiring contracts"
REQUIRES_KEY = False


def _store(conn, key, source, title, agency, jurisdiction, state, when, value_text, fit, url, raw) -> bool:
    cat, heavy = classify_category(title or "", "")
    if heavy or not cat:
        return False
    conn.execute("""INSERT INTO forecast_opportunities(dedupe_key,source,title,agency,jurisdiction,state,anticipated_date,
                    estimated_value_text,target_category,small_business_fit,source_url,raw_json,first_seen_at,last_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO UPDATE SET anticipated_date=excluded.anticipated_date, title=excluded.title,
                    small_business_fit=excluded.small_business_fit, estimated_value_text=excluded.estimated_value_text,
                    last_seen_at=excluded.last_seen_at""",
                 (key, source, title, agency, jurisdiction, state, when, value_text, cat, fit, url, db.dumps(raw), db.now(), db.now()))
    return True


def cook_county(conn) -> int:
    url = "https://datacatalog.cookcountyil.gov/resource/cbhc-xvrm.json"
    n = 0
    for r in net.get_json(url, {"$limit": 2000}):
        title = f"{r.get('project_description') or ''} ({r.get('contract_commodity_category') or ''})"
        n += _store(conn, f"cook:{r.get('buying_plan_number')}", "cook_county_buying_plan", title, f"Cook County {r.get('department') or ''}",
                    "Cook County, IL", "IL", r.get("anticipated_advertise_date"), r.get("estimated_total_contract"),
                    r.get("proposed_procurement_method"), f"{url}?buying_plan_number={r.get('buying_plan_number')}", r)
    return n


def san_diego(conn) -> int:
    url = "https://internal-sandiegocounty.data.socrata.com/resource/rrhp-9sw9.json"
    n = 0
    for r in net.get_json(url, {"$limit": 2000}):
        title = r.get("description_of_goods_services") or r.get("current_contract_title") or ""
        key = f"sd:{(r.get('county_department_seeking') or '')[:40]}:{title[:80]}"
        n += _store(conn, key, "san_diego_county_forecast", title[:300], f"San Diego County {r.get('county_department_seeking') or ''}",
                    "San Diego County, CA", "CA", (r.get("anticipated_posting_date") or "")[:10], None,
                    f"good fit for small/local: {r.get('good_fit_for_small_and_local')}", url, r)
    return n


def nyc_mwbe_upcoming(conn) -> int:
    url = "https://data.cityofnewyork.us/resource/ww83-bcks.json"
    year = date.today().year
    n = 0
    for r in net.get_json(url, {"$where": f"fiscal_year >= '{year}'", "$limit": 2000}):
        n += _store(conn, f"nycmwbe:{r.get('fiscal_year')}:{r.get('procurement_name')}", "nyc_mwbe_upcoming",
                    r.get("procurement_name"), "NYC (various agencies)", "New York City", "NY", f"FY{r.get('fiscal_year')}",
                    None, f"M/WBE method: {r.get('mwbe_small_purchase_method')}", url, r)
    return n


def montgomery_expiring(conn) -> int:
    url = "https://data.montgomerycountymd.gov/resource/vmu2-pnrc.json"
    today = date.today()
    soon = (today + timedelta(days=270)).isoformat()
    n = 0
    for r in net.get_json(url, {"$where": f"expiration >= '{today.isoformat()}' AND expiration <= '{soon}'", "$limit": 3000}):
        desc = r.get("contractdesc") or ""
        stored = _store(conn, f"mcexp:{r.get('contractno')}", "montgomery_expiring_contract", f"Expiring: {desc}",
                        f"Montgomery County {r.get('deptname') or ''}", "Montgomery County, MD", "MD",
                        (r.get("expiration") or "")[:10], None,
                        f"current term ends {(r.get('expiration') or '')[:10]}; renewal options through {(r.get('extend') or 'n/a')[:10]} "
                        f"(the county may renew instead of re-bidding); incumbent {r.get('vendor')}; "
                        f"buyer {r.get('buyerfirst')} {r.get('buyerlast')} {r.get('buyeremail') or ''}",
                        f"{url}?contractno={r.get('contractno')}", r)
        if stored and r.get("vendor"):
            cat, _ = classify_category(desc, "")
            contractors.upsert(conn, dict(name=r["vendor"], state="MD", category=cat, zip=r.get("vendorzip"),
                                          source="montgomery_contracts", source_url=f"{url}?contractno={r.get('contractno')}",
                                          summary="Montgomery County contract holder"),
                               [("county_contract", f"#{r.get('contractno')} {desc[:120]} (expires {(r.get('expiration') or '')[:10]})",
                                 "government_verified")])
        n += stored
    return n


def sync(conn) -> dict:
    out = {}
    for name, fn in (("cook_county", cook_county), ("san_diego", san_diego), ("nyc_mwbe_upcoming", nyc_mwbe_upcoming),
                     ("montgomery_expiring", montgomery_expiring)):
        try:
            out[name] = fn(conn)
        except net.NetError as e:
            db.log_error(conn, f"forecasts:{name}", str(e))
            out[name] = f"error: {e}"
    contractors.score_all(conn)
    out["awards"] = sum(v for v in out.values() if isinstance(v, int))
    return out
