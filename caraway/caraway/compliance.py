"""Independent compliance gate.

This module does not know scores or rankings and cannot be argued with by
the executive layer. Every write-capable or binding action must pass
`authorize()`. Binding actions are hard-denied in code until the owner
explicitly configures authority; there is no flag an AI output can flip.
"""
from __future__ import annotations

from . import config, db

# Actions Caraway may take on its own (read-only or local drafts).
AUTONOMOUS = {"fetch_public_source", "store_record", "score", "draft_message", "create_task",
              "record_finding", "local_llm_summary"}
# Actions that leave the building or bind the company. Denied unless the
# owner has set the named control AND the action is in the allowlist below.
GATED = {
    "send_email": "email_sending_enabled",
}
# Never automated, regardless of configuration.
FORBIDDEN = {"submit_bid", "sign_contract", "make_attestation", "commit_funds", "make_purchase",
             "register_account", "accept_terms", "bypass_captcha"}


class Denied(Exception):
    pass


def paused(conn) -> bool:
    return config.STOP_FILE.exists() or db.control(conn, "paused") == "1"


def authorize(conn, action: str, target: str = "") -> None:
    if action in FORBIDDEN:
        db.audit(conn, "compliance", action, target, "DENIED", "Binding action reserved for the owner.")
        raise Denied(f"{action} is reserved for the owner and is never automated")
    if paused(conn) and action != "create_task":
        db.audit(conn, "compliance", action, target, "DENIED", "System paused or STOP file present.")
        raise Denied("system is paused")
    if action in AUTONOMOUS:
        return
    if action in GATED:
        if db.control(conn, GATED[action]) != "1":
            db.block(conn, action, "Owner has not enabled this action.",
                     "Connect a dedicated business email account and approve sending rules, then enable it.")
            db.audit(conn, "compliance", action, target, "DENIED", "Control not enabled.")
            raise Denied(f"{action} not enabled by owner")
        # Even when enabled, sending is not implemented in v0.1 (fail closed).
        raise Denied(f"{action} is not implemented in this version")
    db.audit(conn, "compliance", action, target, "DENIED", "Unknown action.")
    raise Denied(f"unknown action {action}")


def bid_readiness(opp: dict, profile: dict) -> dict:
    """Would Caraway be allowed to bid right now? Only 'verified' facts count."""
    missing = []
    regs, ins = profile.get("registrations", {}), profile.get("insurance", {})
    if opp.get("source") == "sam_gov" and regs.get("sam_gov_active") != "verified":
        missing.append("SAM.gov registration")
    if opp.get("source") == "nyc_city_record" and regs.get("nyc_passport_vendor_account") != "verified":
        missing.append("NYC PASSPort account")
    if ins.get("general_liability") != "verified":
        missing.append("general liability insurance")
    if (profile.get("finance") or {}).get("business_bank_account") != "established":
        missing.append("business bank account")
    if opp.get("eligibility_status") != "candidate":
        missing.append("eligibility screen not passed")
    missing.append("owner bid approval")  # always required
    return {"bid_authorized": False, "missing": missing}


# ------------------------------------------------------------------ email sending

def authorize_send(conn, policy: dict, kind: str, transport, sent_today: int, now=None, approved: bool = False) -> tuple:
    """Standing-policy check for one outbound email. Returns (ok, reason). Logs denials once per reason."""
    from datetime import datetime
    if paused(conn):
        return False, "system paused"
    if db.control(conn, "email_sending_enabled") != "1":
        return False, "owner has not enabled email sending (caraway mail enable)"
    if not policy.get("enabled"):
        return False, "comms policy disabled (config/comms_policy*.json enabled=false)"
    if transport is None or not transport.configured():
        return False, "no mail transport (Gmail not connected)"
    if kind not in policy.get("allowed_kinds", []):
        return False, f"message kind '{kind}' not allowed by policy"
    if not approved and kind not in policy.get("auto_send_kinds", []):
        return False, f"'{kind}' needs owner approval (approve the message, or add the kind to auto_send_kinds)"
    if sent_today >= int(policy.get("daily_send_limit", 0)):
        return False, "daily send limit reached"
    qh = policy.get("quiet_hours_local")
    hour = (now or datetime.now()).astimezone().hour
    if qh and (hour >= qh[0] or hour < qh[1]):
        return False, "quiet hours"
    return True, "ok"


# ------------------------------------------------------------------ standing authority (future binding actions)

AUTHORITY_PATH = config.PROJECT_ROOT / "config" / "authority_policy.json"


def evaluate_authority(action: str, ctx: dict) -> dict:
    """Would a standing owner policy authorize this action? Used to show which bids could
    eventually go out without a per-bid approval. Binding actions remain FORBIDDEN in code
    regardless of the answer; enabling them is a deliberate future code change plus owner sign-off."""
    import json as _json
    try:
        pol = _json.loads(AUTHORITY_PATH.read_text())
    except (OSError, ValueError):
        return {"authorized": False, "rule": None, "unmet": ["no authority policy file"]}
    best = {"authorized": False, "rule": None, "unmet": ["no rule for this action"]}
    for rule in pol.get("rules", []):
        if rule.get("action") != action:
            continue
        unmet = []
        if not rule.get("enabled"):
            unmet.append("rule disabled by owner")
        for key, limit in (rule.get("max") or {}).items():
            v = ctx.get(key)
            if v is None or v > limit:
                unmet.append(f"{key}={v} exceeds {limit}")
        for key, limit in (rule.get("min") or {}).items():
            v = ctx.get(key)
            if v is None or v < limit:
                unmet.append(f"{key}={v} below {limit}")
        for key in rule.get("require_true", []):
            if not ctx.get(key):
                unmet.append(f"{key} not satisfied")
        if action in FORBIDDEN:
            unmet.append("automated execution not implemented: submission stays with the owner")
        res = {"authorized": not unmet, "rule": rule.get("id"), "unmet": unmet}
        if not best["rule"] or len(unmet) < len(best["unmet"]):
            best = res
    return best
