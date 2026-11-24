"""Official government open-bid datasets published through Socrata open-data APIs. No keys.

Each dataset is described by a small config, so adding another government
is a config entry plus an allowlisted host. Found via the Socrata
Discovery API (api.us.socrata.com) and checked by hand for: official
publisher, current updates, and a due-date column.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import net
from .base import clean_solnum, normalized

REQUIRES_KEY = False


def _url(v):
    return v.get("url") if isinstance(v, dict) else v


def _due_utc(raw):
    """LA publishes UTC timestamps."""
    return raw.replace(".000", "") + "+00:00" if raw else None


def _due_date_only(raw):
    """Date-only deadlines: use 00:00 local on that date, the earliest the deadline could be."""
    return raw[:10] + "T00:00:00" if raw else None


DATASETS = {
    "la_ramp": {
        "description": "City of Los Angeles RAMP open bids (data.lacity.org hf3r-utnq)",
        "endpoint": "https://data.lacity.org/resource/hf3r-utnq.json",
        "where": "stagename='Open'",
        "order": "bidpost DESC",
        "jurisdiction": "Los Angeles, CA", "state": "CA", "level": "municipal",
        "map": lambda r: dict(
            source_record_id=r.get("rampid"), title=r.get("title"), solicitation_number=clean_solnum(r.get("rampid")),
            agency_name=r.get("department"), published_at=r.get("bidpost"), due_at=_due_utc(r.get("closedate")),
            due_at_raw=r.get("closedate"), notice_type=r.get("type"), category=r.get("category"),
            source_url=_url(r.get("url")), selection_method=r.get("type"), scope_text="",
            documents=[{"name": "RAMP opportunity page (vendor account may be needed for documents)", "url": _url(r.get("url")), "access": "public_page"}]),
    },
    "montgomery_md": {
        "description": "Montgomery County, MD solicitations (data.montgomerycountymd.gov eeq6-nnwe)",
        "endpoint": "https://data.montgomerycountymd.gov/resource/eeq6-nnwe.json",
        "where": "status='Active'",
        "order": "issuancedate DESC",
        "jurisdiction": "Montgomery County, MD", "state": "MD", "level": "county",
        "map": lambda r: dict(
            source_record_id=r.get("number"), title=r.get("description"), solicitation_number=clean_solnum(r.get("number")),
            agency_name=f"Montgomery County {r.get('department') or ''}".strip(), published_at=r.get("issuancedate"),
            due_at=_due_date_only(r.get("closingdate")), due_at_raw=r.get("closingdate"), notice_type=r.get("type"),
            category="Construction" if r.get("construction") == "Y" else "Non-construction",
            source_url=f"https://data.montgomerycountymd.gov/resource/eeq6-nnwe.json?number={r.get('number')}",
            selection_method=r.get("type"),
            set_aside="Local Small Business Reserve" if r.get("lsbrpindicator") == "Y" else None,
            scope_text=("Postponed or cancelled: " + r["cancelledpostponed"]) if r.get("cancelledpostponed") else "",
            contact={"buyer": r.get("buyer"), "department_contact": r.get("deptcontact")},
            lifecycle_hint="cancelled" if "cancel" in (r.get("cancelledpostponed") or "").lower() else None,
            documents=[{"name": "Montgomery County procurement portal (search by solicitation number)",
                        "url": "https://www.montgomerycountymd.gov/PRO/", "access": "public_page"}]),
    },
    "delaware": {
        "description": "State of Delaware open bids (data.delaware.gov 2hnj-zwix, weekly)",
        "endpoint": "https://data.delaware.gov/resource/2hnj-zwix.json",
        "where": None,
        "order": "opendate DESC",
        "jurisdiction": "State of Delaware", "state": "DE", "level": "state",
        "map": lambda r: dict(
            source_record_id=r.get("contractnumber"), title=r.get("contracttitle"),
            solicitation_number=clean_solnum(r.get("contractnumber")), agency_name=f"Delaware {r.get('agencycode') or ''}".strip(),
            published_at=r.get("opendate"), due_at=_due_date_only(r.get("deadlinedate")), due_at_raw=r.get("deadlinedate"),
            notice_type="Bid", category=f"UNSPSC {r.get('unspsc')}" if r.get("unspsc") else None,
            source_url=_url(r.get("bidurl")), selection_method=None, scope_text="",
            documents=[{"name": "Delaware Bids & Contracts page", "url": _url(r.get("bidurl")), "access": "public_page"}]),
    },
}


class Source:
    def __init__(self, name: str):
        self.NAME = name
        self.cfg = DATASETS[name]
        self.DESCRIPTION = self.cfg["description"]
        self.REQUIRES_KEY = False

    def fetch(self, conn=None) -> list:
        cfg, out, offset = self.cfg, [], 0
        while True:
            params = {"$limit": 1000, "$offset": offset, "$order": cfg["order"]}
            if cfg["where"]:
                params["$where"] = cfg["where"]
            rows = net.get_json(cfg["endpoint"], params)
            for r in rows:
                m = cfg["map"](r)
                key = m.get("solicitation_number") or m.get("source_record_id")
                out.append(normalized(
                    dedupe_key=f"{self.NAME}:{key}", source=self.NAME, agency_level=cfg["level"],
                    jurisdiction=cfg["jurisdiction"], location=cfg["jurisdiction"], state=cfg["state"],
                    portal_url=None, naics=None, raw=r, **{k: v for k, v in m.items()}))
            if len(rows) < 1000 or offset > 10000:
                break
            offset += 1000
        return out


SOURCES = {name: Source(name) for name in DATASETS}
