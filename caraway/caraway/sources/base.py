from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

FIELDS = [
    "dedupe_key", "source", "source_record_id", "title", "solicitation_number", "agency_name",
    "agency_level", "jurisdiction", "location", "state", "source_url", "portal_url",
    "published_at", "due_at", "due_at_raw", "notice_type", "category", "naics", "set_aside",
    "selection_method", "scope_text", "contact", "documents", "lifecycle_hint", "raw",
]


def normalized(**kw: Any) -> dict:
    rec = {k: kw.get(k) for k in FIELDS}
    rec["documents"] = rec["documents"] or []
    return rec


def content_hash(rec: dict) -> str:
    keys = ["title", "due_at_raw", "scope_text", "notice_type", "set_aside", "documents", "selection_method"]
    blob = json.dumps({k: rec.get(k) for k in keys}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def clean_solnum(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    return "".join(ch for ch in s.upper() if ch.isalnum() or ch in "-_") or None
