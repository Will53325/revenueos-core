"""SAM.gov Get Opportunities Public API (v2) — the official federal source.

Requires a free public API key from a SAM.gov account (Account Details page).
The key is read from the CARAWAY_SAM_API_KEY environment variable and sent
as a header. Without a key the source records a blocker and is skipped;
Caraway never uses SAM.gov's undocumented website endpoints instead.
Docs: https://open.gsa.gov/api/get-opportunities-public-api/
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from .. import config, net
from .base import clean_solnum, normalized

NAME = "sam_gov"
DESCRIPTION = "SAM.gov Contract Opportunities (official API v2)"
REQUIRES_KEY = True
ENDPOINT = "https://api.sam.gov/opportunities/v2/search"
# o = solicitation, k = combined synopsis/solicitation, p = presolicitation, r = sources sought
NOTICE_TYPES = "o,k,p,r"
PAGE = 1000


class MissingKey(Exception):
    pass


def api_key() -> str:
    return os.environ.get(config.SAM_API_KEY_ENV, "").strip()


def normalize(r: dict) -> dict:
    pop = r.get("placeOfPerformance") or {}
    city = ((pop.get("city") or {}).get("name")) or ""
    state = ((pop.get("state") or {}).get("code")) or ""
    poc = (r.get("pointOfContact") or [{}])
    docs = [{"name": u.rsplit("/", 1)[-1], "url": u, "access": "public"} for u in (r.get("resourceLinks") or [])]
    if r.get("description", "").startswith("http"):
        docs.append({"name": "Full description (SAM.gov API, key required)", "url": r["description"], "access": "api_key"})
    ntype = r.get("type") or r.get("baseType")
    return normalized(
        dedupe_key=f"sam:{r.get('noticeId')}",
        source=NAME, source_record_id=r.get("noticeId"), title=r.get("title"),
        solicitation_number=clean_solnum(r.get("solicitationNumber")),
        agency_name=r.get("fullParentPathName") or r.get("department"), agency_level="federal",
        jurisdiction="United States",
        location=", ".join(p for p in (city, state) if p) or "unspecified", state=state or None,
        source_url=r.get("uiLink") or f"https://sam.gov/opp/{r.get('noticeId')}/view", portal_url=None,
        published_at=r.get("postedDate"), due_at=r.get("responseDeadLine"), due_at_raw=r.get("responseDeadLine"),
        notice_type=ntype, category=r.get("classificationCode"), naics=r.get("naicsCode"),
        set_aside=r.get("typeOfSetAsideDescription") or r.get("typeOfSetAside"),
        selection_method=None,
        scope_text=(r.get("description") if not str(r.get("description", "")).startswith("http") else "") or "",
        contact=poc[0] if poc else {}, documents=docs,
        lifecycle_hint=None if r.get("active", "Yes") in ("Yes", True) else "closed", raw=r,
    )


def fetch(conn=None, lookback_days: int = 30) -> list:
    key = api_key()
    if not key:
        raise MissingKey(f"set {config.SAM_API_KEY_ENV} to enable SAM.gov")
    today = datetime.now(timezone.utc)
    out = []
    for naics in sorted(config.TARGET_NAICS):
        offset = 0
        while True:
            data = net.get_json(ENDPOINT, {
                "postedFrom": (today - timedelta(days=lookback_days)).strftime("%m/%d/%Y"),
                "postedTo": today.strftime("%m/%d/%Y"),
                "ncode": naics, "ptype": NOTICE_TYPES, "limit": PAGE, "offset": offset,
            }, headers={"X-Api-Key": key})
            rows = data.get("opportunitiesData") or []
            out.extend(normalize(r) for r in rows)
            if len(rows) < PAGE:
                break
            offset += PAGE
    return out
