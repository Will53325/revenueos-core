"""Deterministic eligibility screening. Fails closed.

Outputs
  eligibility_status: candidate | needs_verification | not_eligible | expired | not_an_opportunity
  go_no_go:           INVESTIGATE | HOLD | NO-GO       (there is no automatic GO)
  reasons:  why it was rejected or held (hard facts)
  blockers: prerequisites that are unverified for Caraway (things a human must confirm)

"candidate" means the work fits what Caraway can plausibly deliver through
subcontractors and nothing disqualifying was found in the notice text. It
does NOT mean Caraway is eligible to bid: blockers list what must be
verified first, and compliance.gate() decides bid authorization separately.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from . import config
from .extract import classify_category


def _parse_due(due_at: Optional[str]) -> Optional[datetime]:
    if not due_at:
        return None
    s = due_at.replace(" America/New_York", "").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Naive local times: compare as UTC-5 (conservative for NYC; at worst ~1h early).
        from datetime import timedelta
        dt = dt.replace(tzinfo=timezone(timedelta(hours=-5)))
    return dt


def _is_goods_purchase(title: str) -> bool:
    t = title.lower()
    return bool(re.search(r"\b(materials?|supplies|products|equipment purchase|deodorizer|pesticides)\b", t)) \
        and "service" not in t


def days_left(due_at: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    dt = _parse_due(due_at)
    if not dt:
        return None
    now = now or datetime.now(timezone.utc)
    return (dt - now).total_seconds() / 86400


def assess(opp: dict, req: dict, profile: dict, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    reasons: list = []
    blockers: list = []
    certs = profile.get("certifications", {})
    regs = profile.get("registrations", {})
    limits = profile.get("pursuit_limits", {})
    title = opp.get("title") or ""
    cat, heavy = classify_category(title, opp.get("scope_text") or "", opp.get("naics"))
    notice = (opp.get("notice_type") or "").lower()

    def result(status: str, gng: str, action: str) -> dict:
        return {"eligibility_status": status, "go_no_go": gng, "reasons": reasons, "blockers": blockers,
                "target_category": cat if not heavy else None, "next_action": action,
                "days_left": days_left(opp.get("due_at"), now)}

    # 1. Lifecycle.
    if opp.get("lifecycle_hint") == "cancelled":
        reasons.append("Notice is a cancellation.")
        return result("not_eligible", "NO-GO", "None. Solicitation cancelled.")
    if "award" in notice and "intent" not in notice and "solicitation" not in notice:
        reasons.append(f"Notice type '{opp.get('notice_type')}' is an award, not an open solicitation.")
        return result("not_an_opportunity", "NO-GO", "Use as market intelligence only.")

    # 2. Deadline.
    dl = days_left(opp.get("due_at"), now)
    if dl is not None and dl < 0:
        reasons.append(f"Response deadline passed ({opp.get('due_at')}).")
        return result("expired", "NO-GO", "None. Quarantined as expired.")
    min_days = limits.get("min_days_to_deadline", 7)
    if dl is not None and dl < min_days:
        reasons.append(f"Only {dl:.1f} days to deadline; Caraway needs at least {min_days} days to verify "
                       "eligibility, find a subcontractor and price the work.")
    if dl is None:
        blockers.append("No firm response deadline in the notice (open-ended list or not published). Confirm in the solicitation.")

    # 3. Mandatory site visit already missed.
    if req.get("site_visit_mandatory") == "yes" and req.get("site_visit_at"):
        sv = _parse_due(req["site_visit_at"])
        if sv and sv < now:
            reasons.append(f"Mandatory site visit/pre-bid on {req['site_visit_at']} has passed.")
    elif req.get("site_visit_mandatory") == "yes":
        blockers.append("Mandatory site visit or pre-bid conference: date and attendance plan required.")

    # 4. Scope fit.
    if heavy:
        reasons.append("Scope needs construction or licensed-trade capability Caraway does not have: "
                       + ", ".join(heavy) + ".")
    elif not cat:
        reasons.append("Outside Caraway's initial service categories (janitorial, grounds, light facility "
                       "maintenance, moving/hauling, pest control).")
    elif (opp.get("category") or "").strip().lower() in config.EXCLUDED_NOTICE_CATEGORIES:
        reasons.append(f"Agency classifies this as '{opp.get('category')}', outside Caraway's market.")
    elif _is_goods_purchase(title):
        reasons.append("Title indicates a purchase of materials/supplies, not a service contract "
                       "(goods resale is outside the initial model).")

    ncat = (opp.get("category") or "").lower()
    if ncat.startswith("construction") and not reasons:
        blockers.append(f"Agency files this under '{opp.get('category')}': expect bonding, prevailing wage "
                        "and contractor licensing requirements. Confirm in the solicitation.")

    # 5. Set-asides and certifications.
    conds = " | ".join(req.get("eligibility_conditions") or []) + " | " + (opp.get("set_aside") or "")
    cl = conds.lower()
    cert_map = [("sdvosb", "sdvosb"), ("8(a)", "8a"), ("hubzone", "hubzone"), ("wosb", "wosb"),
                ("m/wbe-only", "nyc_mwbe"), ("small business", "small_business")]
    for needle, key in cert_map:
        if needle in cl:
            state = certs.get(key, "unverified")
            if state == "not_held":
                reasons.append(f"Restricted to {needle.upper()} firms; Caraway does not hold this status.")
            elif state != "verified":
                blockers.append(f"Restricted to {needle.upper()} firms; Caraway's status is {state}. Verify before pursuing.")
    if "pre-qualified list" in cl:
        blockers.append("Requires being on a pre-qualified list (PQL) first; that is a separate application.")
    if "state license" in cl:
        blockers.append("A state license is referenced; Caraway holds none on record.")
    if "experience / past performance" in cl:
        blockers.append("Experience or past performance is referenced; Caraway has no federal or municipal past performance on record.")

    # 6. Registrations.
    if opp.get("source") == "sam_gov" and regs.get("sam_gov_active") != "verified":
        blockers.append("Active SAM.gov entity registration (UEI) not verified.")
    if opp.get("source") == "nyc_city_record" and regs.get("nyc_passport_vendor_account") != "verified":
        blockers.append("NYC PASSPort vendor account not verified (needed to view documents and respond).")
    elif opp.get("source") not in ("sam_gov", "nyc_city_record"):
        state_regs = regs.get("state_vendor_registrations") or {}
        if state_regs.get(opp.get("source")) != "verified":
            blockers.append(f"Vendor registration with this government's procurement portal ({opp.get('source')}) "
                            "not verified; most require it before documents can be downloaded or a response submitted.")
    if opp.get("state") and (profile.get("home_base") or {}).get("state") is None:
        blockers.append(f"Work is in {opp.get('state')}; Caraway's home base and subcontractor coverage there are unknown.")

    # 7. Money, bonding, insurance.
    if req.get("bonding_requirements", "unknown") != "unknown":
        cap = (profile.get("bonding") or {}).get("bid_bond_capacity_usd")
        blockers.append(f"Bonding referenced ({req['bonding_requirements']}); Caraway bonding capacity: "
                        f"{'unknown' if cap is None else cap}.")
    blockers.append("Insurance coverage (general liability, workers' comp) not verified for Caraway.")
    est = req.get("estimated_value")
    mx = limits.get("max_contract_value_usd")
    if est and mx and est > mx:
        reasons.append(f"Published estimate ${est:,.0f} exceeds the current pursuit limit ${mx:,.0f}.")
    if (req.get("subcontracting_restrictions") or "").find("prohibited") >= 0:
        reasons.append("Subcontracting is prohibited; Caraway has no workforce to self-perform.")
    if "limitations on subcontracting" in (req.get("subcontracting_restrictions") or "").lower():
        blockers.append("FAR limitation on subcontracting applies: a prime that subcontracts most of the "
                        "labor may be non-compliant. Needs a compliant teaming structure.")

    if reasons:
        return result("not_eligible", "NO-GO", "None. See rejection reasons.")
    action = "Open the official notice, download the full solicitation and amendments, and confirm the blockers listed."
    if opp.get("source") == "nyc_city_record":
        action = "Look up the PIN in NYC PASSPort (public RFx browse), download documents, confirm blockers."
    return result("candidate", "INVESTIGATE", action)
