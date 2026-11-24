"""NYC City Record Online — official NYC Open Data (Socrata) API. No key needed.

Dataset dg92-zbpx: every notice published in The City Record, including
procurement solicitations with PIN, agency, due date and notice text.
Full bid documents live in NYC PASSPort, which requires a vendor login;
Caraway records that as a blocker rather than trying to get around it.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .. import net
from ..extract import clean_text
from .base import clean_solnum, normalized

NAME = "nyc_city_record"
DESCRIPTION = "NYC City Record Online (NYC Open Data, dataset dg92-zbpx)"
REQUIRES_KEY = False
ENDPOINT = "https://data.cityofnewyork.us/resource/dg92-zbpx.json"
RECORD_URL = ENDPOINT + "?request_id={rid}"
PORTAL_URL = "https://a856-cityrecord.nyc.gov/RequestDetail/{rid}"
PASSPORT_URL = "https://a0333-passportpublic.nyc.gov/rfx.html"
OPPORTUNITY_TYPES = ("Solicitation", "Request for Information", "Request for Expressions of Interest")
PREFIX_RE = re.compile(r"^\s*(bid extension|correction|cancellation|bid cancellation|addendum|amendment|re-?bid)\s*:\s*", re.I)
PAGE = 1000


def _due(raw):
    """Return (iso_due or None, note). Far-future dates are placeholders for open lists."""
    if not raw:
        return None, "no due date published"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", ""))
    except ValueError:
        return None, f"unparseable due date {raw!r}"
    if dt.year > datetime.now().year + 2:
        return None, f"placeholder due date {raw[:10]} (open-ended list or prequalification)"
    # City Record times are New York local time; stored with a fixed offset label.
    return dt.isoformat() + " America/New_York", ""


def normalize(r: dict) -> dict:
    rid = r.get("request_id")
    raw_title = (r.get("short_title") or "").strip()
    prefix = PREFIX_RE.match(raw_title)
    title = PREFIX_RE.sub("", raw_title).strip() or raw_title
    hint = None
    if prefix:
        word = prefix.group(1).lower()
        hint = "cancelled" if "cancel" in word else "amended"
    due_at, due_note = _due(r.get("due_date"))
    texts = [r.get(f"additional_description_{i}") for i in (1, 2, 3)] + \
            [r.get(f"other_info_{i}") for i in (1, 2, 3, 4)]
    scope = clean_text(" \n".join(t for t in texts if t))
    if due_note:
        scope = f"[Caraway note: {due_note}] " + scope
    pin = clean_solnum(r.get("pin"))
    contact = {k: r.get(k) for k in ("contact_name", "contact_phone", "email", "address_to_request",
                                      "street_address_1", "city", "state_code", "zip_code") if r.get(k)}
    docs = []
    if "passport" in scope.lower():
        docs.append({"name": "Solicitation documents (NYC PASSPort, vendor login required)", "url": PASSPORT_URL,
                     "access": "login_required"})
    loc_parts = [r.get("street_address_1"), r.get("city"), r.get("state_code")]
    location = ", ".join(p for p in loc_parts if p) or "New York City, NY"
    return normalized(
        dedupe_key=f"nyc:{pin}" if pin else f"nyc:req:{rid}",
        source=NAME, source_record_id=rid, title=title, solicitation_number=pin,
        agency_name=r.get("agency_name"), agency_level="municipal", jurisdiction="New York City",
        location=location, state="NY",
        source_url=RECORD_URL.format(rid=rid), portal_url=PORTAL_URL.format(rid=rid),
        published_at=r.get("start_date"), due_at=due_at, due_at_raw=r.get("due_date"),
        notice_type=r.get("type_of_notice_description"), category=r.get("category_description"),
        naics=None, set_aside=None, selection_method=r.get("selection_method_description"),
        scope_text=scope, contact=contact, documents=docs, lifecycle_hint=hint, raw=r,
    )


def fetch(conn=None, lookback_days: int = 120) -> list:
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT00:00:00")
    types = ",".join(f"'{t}'" for t in OPPORTUNITY_TYPES)
    where = (f"section_name='Procurement' AND type_of_notice_description in ({types}) "
             f"AND start_date >= '{since}'")
    rows, offset = [], 0
    while True:
        page = net.get_json(ENDPOINT, {"$where": where, "$order": "start_date ASC, request_id ASC",
                                       "$limit": PAGE, "$offset": offset})
        rows.extend(page)
        if len(page) < PAGE or offset > 20000:
            break
        offset += PAGE
    # Multiple notices share a PIN (original, extension, correction). Keep the
    # newest notice per PIN; older ones become version history.
    latest: dict = {}
    for r in rows:
        rec = normalize(r)
        prev = latest.get(rec["dedupe_key"])
        if prev is None or (rec["published_at"] or "") >= (prev["published_at"] or ""):
            if prev is not None:
                rec.setdefault("_superseded", []).extend(prev.get("_superseded", []) + [prev["source_record_id"]])
            latest[rec["dedupe_key"]] = rec
    return list(latest.values())
