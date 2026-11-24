"""Bid preparation engine.

Builds a versioned bid package per opportunity from documented evidence:
  * market benchmark from real federal awards in the same category (USAspending)
  * subcontractor quotes (lowest valid, annualized)
  * pricing via finance.bid_estimate with policy margins
  * month-by-month working-capital model (government pays late, subs get paid sooner)
  * compliance checks, each with status pass | fail | unknown and its evidence
  * standing-authority evaluation (would a policy allow submission?)

A package is 'compliant' only if every check passes. Unknown is never pass.
Nothing here submits anything.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Optional

from . import compliance, config, contractors as crm, db, eligibility, finance
from .mail.classify import annualize

POLICY_PATH = config.PROJECT_ROOT / "config" / "bid_policy.json"
DEFAULTS = {
    "overhead_pct": 0.10, "contingency_pct": 0.05, "target_margin_pct": 0.15, "min_margin_pct": 0.10,
    "contract_months": 12, "subcontractor_payment_days": 30,
    "government_payment_days": {"federal": 30, "state": 45, "county": 45, "municipal": 45, "default": 45},
    "benchmark_max_award": 1000000,
}


def policy() -> dict:
    try:
        return {**DEFAULTS, **json.loads(POLICY_PATH.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def benchmark(conn, category: Optional[str], state: Optional[str]) -> dict:
    """Annualized price of comparable federal awards. An estimate, labeled with its sample."""
    if not category:
        return {"basis": "none", "n": 0}
    rows = db.q(conn, """SELECT amount, start_date, end_date, state FROM award_intel WHERE target_category=?
                         AND amount > 10000 AND amount <= ?""", (category, policy()["benchmark_max_award"]))
    def annual(r):
        try:
            from datetime import date
            d = (date.fromisoformat(r["end_date"][:10]) - date.fromisoformat(r["start_date"][:10])).days
        except (TypeError, ValueError):
            return None
        return r["amount"] / max(d / 365.0, 0.5) if d > 60 else None
    local = [a for a in (annual(r) for r in rows if r["state"] == state) if a]
    allv = [a for a in (annual(r) for r in rows) if a]
    sample, scope = (local, f"{state} federal awards") if len(local) >= 10 else (allv, "national federal awards")
    if len(sample) < 5:
        return {"basis": "insufficient data", "n": len(sample)}
    return {"basis": f"median annualized value of {len(sample)} {scope} in '{category}' (USAspending)",
            "n": len(sample), "median_annual": round(statistics.median(sample), 2),
            "p25_annual": round(statistics.quantiles(sample, n=4)[0], 2), "p75_annual": round(statistics.quantiles(sample, n=4)[2], 2),
            "caveat": "Different agencies, sizes and scopes. A sanity range, not a price for this job."}


def cashflow(annual_price: float, annual_sub_cost: float, months: int, gov_days: int, sub_days: int,
             upfront: float = 0.0) -> dict:
    """Day-level cash model. Work done in month m is invoiced at the end of that month (day 30*m).
    The agency pays gov_days later; the subcontractor is paid sub_days after month end. On the
    same day, payments out are counted before payments in (conservative). Returns the peak
    cash Caraway must carry, plus a monthly summary for display."""
    monthly_in, monthly_out = annual_price / 12, annual_sub_cost / 12
    events = []
    for m in range(1, months + 1):
        events.append((30 * m + sub_days, 0, -monthly_out))   # 0 sorts before 1: outflow first on ties
        events.append((30 * m + gov_days, 1, monthly_in))
    events.sort()
    bal, low = -upfront, -upfront
    by_month: dict = {}
    for day, _, amt in events:
        bal += amt
        low = min(low, bal)
        mm = (day - 1) // 30 + 1
        row = by_month.setdefault(mm, {"month": mm, "cash_in": 0.0, "cash_out": 0.0})
        row["cash_in" if amt > 0 else "cash_out"] += abs(amt)
        row["cumulative"] = round(bal, 2)
    rows = [dict(r, cash_in=round(r["cash_in"], 2), cash_out=round(r["cash_out"], 2)) for _, r in sorted(by_month.items())]
    return {"peak_cash_needed": round(-low, 2) if low < 0 else 0.0, "months": rows, "gov_payment_days": gov_days,
            "sub_payment_days": sub_days, "basis": "projection"}


def _check(name, status, evidence, fix=""):
    return {"check": name, "status": status, "evidence": evidence, "fix": fix}


def prepare(conn, opp_id: int, profile: Optional[dict] = None) -> dict:
    profile = profile or config.load_profile()
    pol = policy()
    opp = db.one(conn, "SELECT * FROM opportunities WHERE id=?", (opp_id,))
    if not opp:
        raise ValueError(f"no opportunity {opp_id}")
    compliance.authorize(conn, "score", f"bid:{opp_id}")
    level = "federal" if opp["source"] == "sam_gov" else (db.one(conn, "SELECT level FROM agencies WHERE id=?", (opp["agency_id"],)) or {}).get("level")
    gov_days = pol["government_payment_days"].get(level or "default", pol["government_payment_days"]["default"])
    bench = benchmark(conn, opp["target_category"], opp["state"])

    quotes = db.q(conn, """SELECT q.*, c.name, c.certifications, c.license_status, c.license_number FROM quotes q
                           JOIN contractors c ON c.id=q.contractor_id WHERE q.opportunity_id=? AND q.status='received'""", (opp_id,))
    annualized = []
    for q in quotes:
        a = annualize(q["amount"], q["period"] or "unspecified")
        if a:
            annualized.append((a, q))
    best = min(annualized, key=lambda x: x[0]) if annualized else None

    pricing: dict = {"basis": "estimate", "benchmark": bench}
    if bench.get("median_annual"):
        # Most a sub can cost while Caraway prices at the market median and keeps its target margin.
        pricing["target_subcontract_cost"] = round(bench["median_annual"] * (1 - pol["target_margin_pct"])
                                                   / (1 + pol["overhead_pct"] + pol["contingency_pct"]), 2)
    if best:
        est = finance.bid_estimate(best[0], overhead_pct=pol["overhead_pct"], contingency_pct=pol["contingency_pct"],
                                   target_margin_pct=pol["target_margin_pct"])
        pricing.update(est, quote_id=best[1]["id"], subcontractor=best[1]["name"], annual_sub_cost=best[0],
                       quote_verified=bool(best[1]["verified"]))
        if bench.get("p75_annual") and est["bid_price"] > bench["p75_annual"]:
            pricing["market_warning"] = "price is above the 75th percentile of comparable awards; likely uncompetitive"
        cf = cashflow(est["bid_price"], best[0], pol["contract_months"], gov_days, pol["subcontractor_payment_days"])
    else:
        cf = {"peak_cash_needed": None, "basis": "needs a subcontractor quote"}

    checks = []
    a_ok = opp["eligibility_status"] == "candidate"
    checks.append(_check("eligibility screen", "pass" if a_ok else "fail", f"status {opp['eligibility_status']}"))
    dl = eligibility.days_left(opp["due_at"])
    min_days = profile.get("pursuit_limits", {}).get("min_days_to_deadline", 7)
    checks.append(_check("time to deadline", "pass" if dl is not None and dl >= 2 else ("unknown" if dl is None else "fail"),
                         f"{dl:.1f} days" if dl is not None else "no firm deadline", "confirm the due date in the solicitation"))
    regs = profile.get("registrations", {})
    reg_ok = (regs.get("sam_gov_active") == "verified") if opp["source"] == "sam_gov" else \
        (regs.get("nyc_passport_vendor_account") == "verified") if opp["source"] == "nyc_city_record" else \
        ((regs.get("state_vendor_registrations") or {}).get(opp["source"]) == "verified")
    checks.append(_check("vendor registration", "pass" if reg_ok else "fail", "owner profile",
                         "register on the agency's portal and mark it verified in owner_profile.local.json"))
    ins_ok = profile.get("insurance", {}).get("general_liability") == "verified"
    checks.append(_check("insurance", "pass" if ins_ok else "fail", "owner profile", "bind GL + workers' comp; record limits"))
    conds = json.loads(opp["eligibility_conditions"] or "[]")
    setaside = [c for c in conds if "set-aside" in c or "M/WBE" in c] + ([opp["set_aside"]] if opp["set_aside"] else [])
    open_comp = any(w in (opp["scope_text"] or "").lower() for w in ("full and open", "unrestricted"))
    checks.append(_check("set-aside / certification", "fail" if setaside else ("pass" if open_comp else "unknown"),
                         "; ".join(setaside) or ("notice states unrestricted competition" if open_comp else
                                                 "no set-aside found in notice text, which is not proof there is none"),
                         "certification must be held and verified before bidding a set-aside" if setaside else
                         "confirm competition type in the solicitation"))
    sub = (opp["subcontracting_restrictions"] or "unknown").lower()
    if "prohibited" in sub:
        checks.append(_check("subcontracting allowed", "fail", sub, "Caraway cannot self-perform"))
    elif "limitations on subcontracting" in sub or (opp["source"] == "sam_gov" and opp["set_aside"]):
        checks.append(_check("FAR 52.219-14 limitation on subcontracting", "fail",
                             "services set-aside: at most 50% of the amount paid may go to non-similarly-situated subs; "
                             "Caraway has no workforce", "team only with a similarly situated subcontractor, verified"))
    else:
        checks.append(_check("subcontracting allowed", "unknown", f"notice text: {sub}", "read the solicitation's subcontracting clauses"))
    if "goal" in sub:
        mwbe_sub = best and best[1]["certifications"] and ("MBE" in best[1]["certifications"] or "WBE" in best[1]["certifications"])
        checks.append(_check("participation goal", "pass" if mwbe_sub else "unknown",
                             f"subcontractor certifications: {best[1]['certifications'] if best else 'no sub selected'}"))
    checks.append(_check("subcontractor quote", "pass" if best and best[1]["verified"] else ("unknown" if best else "fail"),
                         f"{best[1]['name']}: ${best[0]:,.0f}/yr ({best[1]['evidence']})" if best else "no quote received",
                         "confirm the quote in writing against the full scope" if best else "request quotes"))
    if opp["target_category"] == "pest_control":
        lic = best and best[1]["license_status"] == "active"
        checks.append(_check("performer license", "pass" if lic else "unknown",
                             (best[1]["license_number"] if lic else "no verified pesticide license for the selected sub"),
                             "use a subcontractor with an active state pesticide business license"))
    if pricing.get("gross_margin_pct") is not None:
        checks.append(_check("margin floor", "pass" if pricing["gross_margin_pct"] >= pol["min_margin_pct"] * 100 else "fail",
                             f"{pricing['gross_margin_pct']}% vs floor {pol['min_margin_pct'] * 100:.0f}%"))
    cap = finance.capacity_check(conn, 0, monthly_payroll_gap=(best[0] / 12 if best else 0), payment_delay_days=gov_days)
    checks.append(_check("working capital", {"yes": "pass", "no": "fail"}.get(cap["decision"], "unknown"),
                         f"peak cash needed {cf.get('peak_cash_needed')}; {cap['reason']}", "connect verified bank balance"))
    bond = opp["bonding_requirements"] or "unknown"
    if bond != "unknown":
        bcap = profile.get("bonding", {}).get("bid_bond_capacity_usd")
        checks.append(_check("bonding", "pass" if bcap and pricing.get("bid_price") and bcap >= pricing["bid_price"] else "fail",
                             f"{bond}; capacity {bcap}", "obtain surety capacity"))
    checks.append(_check("full solicitation reviewed", "unknown", "only the notice summary has been analyzed",
                         "download the solicitation and amendments; confirm scope, forms and attestations"))

    compliant = all(c["status"] == "pass" for c in checks)
    ctx = {"bid_price": pricing.get("bid_price"), "upfront_cash_required": cf.get("peak_cash_needed"),
           "days_to_payment": gov_days, "gross_margin_pct": pricing.get("gross_margin_pct"), "days_to_deadline": dl,
           "all_checks_pass": compliant, "quote_in_hand": bool(best), "registrations_verified": reg_ok, "insurance_verified": ins_ok}
    authority = compliance.evaluate_authority("submit_bid", ctx)
    status = "compliant_pending_owner" if compliant else ("awaiting_quotes" if not best else "blocked")

    prev = db.one(conn, "SELECT MAX(version) v FROM bid_packages WHERE opportunity_id=?", (opp_id,))
    version = (prev["v"] or 0) + 1
    path = config.HOME / "bids" / f"opp-{opp_id}-v{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(opp, pricing, cf, checks, authority, status))
    conn.execute("""INSERT INTO bid_packages(opportunity_id,version,status,compliant,checks_json,pricing_json,cashflow_json,policy_json,
                    document_path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                 (opp_id, version, status, int(compliant), db.dumps(checks), db.dumps(pricing), db.dumps(cf),
                  db.dumps(authority), str(path), db.now()))
    conn.execute("DELETE FROM financial_facts WHERE opportunity_id=? AND fact_type IN ('bid_value','projected_profit') AND basis!='actual'", (opp_id,))
    if pricing.get("bid_price"):
        conn.execute("""INSERT INTO financial_facts(fact_type,basis,amount,as_of,opportunity_id,source,evidence,verified,created_at)
                        VALUES('bid_value','estimate',?,?,?,'bid_engine',?,0,?), ('projected_profit','projection',?,?,?,'bid_engine',?,0,?)""",
                     (pricing["bid_price"], db.now(), opp_id, f"package v{version}", db.now(),
                      pricing["gross_profit"], db.now(), opp_id, f"package v{version}", db.now()))
    db.audit(conn, "bid_engine", "prepare_bid_package", str(opp_id), status, f"v{version} checks={sum(c['status']=='pass' for c in checks)}/{len(checks)}")
    return {"opportunity_id": opp_id, "version": version, "status": status, "compliant": compliant, "path": str(path),
            "checks": checks, "pricing": pricing, "cashflow": {k: v for k, v in cf.items() if k != "months"}, "authority": authority}


def render(opp, pricing, cf, checks, authority, status) -> str:
    sym = {"pass": "PASS", "fail": "FAIL", "unknown": "UNKNOWN"}
    lines = [f"# Bid package: {opp['title']}", "",
             f"- Agency: {opp['agency_name']} · {opp['location']}",
             f"- Solicitation: {opp['solicitation_number'] or 'n/a'} · Due: {opp['due_at'] or opp['due_at_raw']}",
             f"- Official notice: {opp['source_url']}", f"- Package status: **{status}**", "",
             "## Compliance checks", "", "| Check | Status | Evidence | To resolve |", "|---|---|---|---|"]
    for c in checks:
        lines.append(f"| {c['check']} | {sym[c['status']]} | {str(c['evidence']).replace('|', '/')} | {c['fix']} |")
    b = pricing.get("benchmark", {})
    lines += ["", "## Pricing (estimate)", "",
              f"- Market benchmark: {b.get('basis')}" + (f": median ${b['median_annual']:,.0f}/yr, middle half ${b['p25_annual']:,.0f}–${b['p75_annual']:,.0f}" if b.get("median_annual") else ""),
              f"- Target subcontract cost (to price at market median with target margin): " +
              (f"${pricing['target_subcontract_cost']:,.0f}/yr" if pricing.get("target_subcontract_cost") else "n/a")]
    if pricing.get("bid_price"):
        lines += [f"- Lowest quote: {pricing['subcontractor']} ${pricing['annual_sub_cost']:,.0f}/yr (verified: {pricing['quote_verified']})",
                  f"- Bid price: ${pricing['bid_price']:,.2f}/yr · Gross profit ${pricing['gross_profit']:,.2f} ({pricing['gross_margin_pct']}%)",
                  f"- {pricing.get('market_warning', '')}"]
        lines += ["", "## Working capital (projection)", "",
                  f"Government pays ~{cf['gov_payment_days']} days after invoice; subcontractor paid {cf['sub_payment_days']} days after month end. "
                  f"Peak cash needed: **${cf['peak_cash_needed']:,.0f}**."]
    else:
        lines.append("- No subcontractor quote yet: bid price not computed.")
    lines += ["", "## Standing authority", "",
              f"Rule `{authority.get('rule')}`: {'would authorize' if authority.get('authorized') else 'does not authorize'}. "
              + "; ".join(authority.get("unmet", [])), "",
              "_Estimates and projections are not revenue. Submission always requires the owner in this version._"]
    return "\n".join(lines)
