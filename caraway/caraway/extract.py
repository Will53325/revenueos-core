"""Deterministic requirement extraction from solicitation text.

Every extracted fact carries the excerpt it came from. If nothing is found
the field is 'unknown', never 'no'. Absence of a mention in a notice summary
does not prove absence of the requirement in the full solicitation.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Optional

from . import config

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", text))).strip()


def excerpt(text: str, start: int, end: int, pad: int = 140) -> str:
    a, b = max(0, start - pad), min(len(text), end + pad)
    return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def find(text: str, pattern: str) -> list:
    return [(m, excerpt(text, m.start(), m.end())) for m in re.finditer(pattern, text, re.I)]


DATE_PAT = r"(20\d\d-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?|[A-Z][a-z]+ \d{1,2}, 20\d\d(?:,? (?:at )?\d{1,2}:\d\d ?[AP]M)?)"


def parse_loose_date(s: str) -> Optional[str]:
    s = s.strip().replace(" at ", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%B %d, %Y %I:%M %p",
                "%B %d, %Y, %I:%M %p", "%B %d, %Y %I:%M%p", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return None


def extract_requirements(text: str) -> dict:
    """Return requirement fields with evidence. Values: 'yes' | 'no' | 'unknown'."""
    t = clean_text(text)
    out: dict = {"evidence": []}

    def ev(field: str, snippet: str) -> None:
        out["evidence"].append({"field": field, "excerpt": snippet})

    # Site visits / pre-bid conferences.
    out["site_visit"], out["site_visit_mandatory"], out["site_visit_at"] = "unknown", "unknown", None
    hits = find(t, r"(site visit|site inspection|walk[- ]?through|pre[- ]?(bid|proposal|solicitation) (conference|meeting))")
    if hits:
        out["site_visit"] = "yes"
        ev("site_visit", hits[0][1])
        window = t[max(0, hits[0][0].start() - 200): hits[0][0].end() + 600]
        # NYC City Record structured form: "Mandatory: noDate/Time - 2026-11-05 10:00:00" (no space).
        structured = re.search(r"Mandatory:\s*(yes|no)(?=[^a-z]|Date|$)", window, re.I)
        if structured:
            out["site_visit_mandatory"] = structured.group(1).lower()
        elif re.search(r"\(optional\)|optional|not mandatory|non-mandatory", window, re.I):
            out["site_visit_mandatory"] = "no"
        elif re.search(r"mandatory|required|must attend", window, re.I):
            out["site_visit_mandatory"] = "yes"
        m = re.search(r"Date/Time\s*-\s*(20\d\d-\d\d-\d\d \d\d:\d\d(?::\d\d)?)", window) or re.search(DATE_PAT, window)
        if m:
            out["site_visit_at"] = parse_loose_date(m.group(1))

    # Bonding.
    out["bonding_requirements"] = "unknown"
    hits = find(t, r"(bid bond|performance bond|payment bond|bid security|surety)")
    if hits:
        out["bonding_requirements"] = "mentioned: " + ", ".join(sorted({h[0].group(1).lower() for h in hits}))
        ev("bonding", hits[0][1])

    # Insurance.
    out["insurance_requirements"] = "unknown"
    hits = find(t, r"(general liability|certificate of insurance|insurance requirement|workers'? compensation|umbrella (insurance|liability)|additional insured)")
    if hits:
        out["insurance_requirements"] = "mentioned"
        ev("insurance", hits[0][1])

    # Subcontracting limits / participation goals.
    sub = []
    for pat, label in ((r"limitations? on subcontracting|52\.219-14", "FAR limitations on subcontracting"),
                       (r"(M/?WBE|MBE|WBE|DBE|SBE)[^.]{0,80}goal[^.]{0,40}\d{1,3}\s?%", "participation goal"),
                       (r"subcontract(ing|ors?) (is |are )?(not permitted|prohibited)", "subcontracting prohibited"),
                       (r"self[- ]perform", "self-performance requirement")):
        hits = find(t, pat)
        if hits:
            sub.append(label)
            ev("subcontracting", hits[0][1])
    out["subcontracting_restrictions"] = "; ".join(sub) if sub else "unknown"

    # Eligibility / set-asides / prequalification.
    elig = []
    for pat, label in ((r"service[- ]disabled veteran|SDVOSB", "SDVOSB set-aside"),
                       (r"\b8\(a\)", "8(a) set-aside"),
                       (r"HUBZone", "HUBZone set-aside"),
                       (r"women[- ]owned|WOSB", "WOSB set-aside"),
                       (r"M/WBE[- ]only|MWBE[- ]only|certified M/WBE", "NYC M/WBE-only"),
                       (r"total small business|small business set[- ]aside", "small business set-aside"),
                       (r"pre[- ]?qualified list|\bPQL\b", "pre-qualified list (PQL)"),
                       (r"PASSPort", "NYC PASSPort vendor account required"),
                       (r"license[d]? (in|by) the state|state licen[sc]e|licensed (pest|electric|plumb)", "state license"),
                       (r"past performance|years? of experience|minimum (of )?\d+ years", "experience / past performance"),
                       (r"security clearance|background check", "clearance / background checks")):
        hits = find(t, pat)
        if hits:
            elig.append(label)
            ev("eligibility", hits[0][1])
    out["eligibility_conditions"] = elig

    # Published dollar value (only if literally stated with an estimate cue).
    out["estimated_value"], out["estimated_value_basis"] = None, None
    m = re.search(r"(estimated|anticipated|not[- ]to[- ]exceed|contract value|budget)[^$.]{0,60}\$\s?([\d,]+(?:\.\d\d)?)\s?(million|m\b)?", t, re.I)
    if m:
        val = float(m.group(2).replace(",", "")) * (1_000_000 if m.group(3) else 1)
        out["estimated_value"] = val
        out["estimated_value_basis"] = "stated in notice text"
        ev("estimated_value", excerpt(t, m.start(), m.end()))
    return out


def classify_category(title: str, scope: str, naics: Optional[str] = None) -> tuple:
    """Return (target_category or None, heavy_scope_terms_found)."""
    if naics and naics[:6] in config.TARGET_NAICS:
        cat = config.TARGET_NAICS[naics[:6]]
    else:
        cat = None
    text = f"{title} {scope}".lower()
    title_l = (title or "").lower()
    if not cat:
        best = 0
        for name, kws in config.TARGET_CATEGORIES.items():
            # The title must name the work, or the body must mention at least two
            # distinct category keywords. One stray body word ("healthcare
            # landscape") is not evidence of scope.
            title_hits = sum(1 for k in kws if k in title_l)
            body_hits = sum(1 for k in kws if k in text)
            if title_hits == 0 and body_hits < 2:
                continue
            score = title_hits * 3 + body_hits
            if score > best:
                best, cat = score, name
    heavy = sorted({h.strip() for h in config.HEAVY_SCOPE_TERMS if h in title_l})
    return cat, heavy
