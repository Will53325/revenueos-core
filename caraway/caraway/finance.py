"""Pricing and finance. Pure arithmetic, auditable, no AI.

Facts are typed (financial_facts.fact_type) and based (actual / estimate /
projection). A bid value, projected profit, awarded value, receivable and
collected cash are different rows and are never summed together.
"""
from __future__ import annotations

from typing import Optional

from . import db


def bid_estimate(subcontract_cost: float, direct_costs: float = 0.0, overhead_pct: float = 0.10,
                 contingency_pct: float = 0.05, target_margin_pct: float = 0.15,
                 upfront_cash_required: float = 0.0) -> dict:
    """Price = cost * (1 + overhead + contingency) / (1 - margin). Returns an estimate, not a fact."""
    if subcontract_cost < 0 or direct_costs < 0:
        raise ValueError("costs cannot be negative")
    if not 0 <= target_margin_pct < 0.9:
        raise ValueError("target margin must be between 0 and 0.9")
    loaded = (subcontract_cost + direct_costs) * (1 + overhead_pct + contingency_pct)
    price = loaded / (1 - target_margin_pct)
    gross_profit = price - subcontract_cost - direct_costs
    return {
        "basis": "estimate",
        "bid_price": round(price, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_margin_pct": round(gross_profit / price * 100, 2) if price else 0.0,
        "loaded_cost": round(loaded, 2),
        "upfront_cash_required": round(upfront_cash_required, 2),
    }


def compare_quotes(quotes: list) -> Optional[dict]:
    valid = [q for q in quotes if q.get("amount") and q.get("status", "received") == "received"]
    return min(valid, key=lambda q: q["amount"]) if valid else None


def capacity_check(conn, upfront_cost: float, monthly_payroll_gap: float = 0.0,
                   payment_delay_days: int = 60) -> dict:
    """Can Caraway carry this project's cash needs with verified money only?

    Government agencies commonly pay 30-60+ days after invoicing, while
    subcontractors expect payment sooner. Needs verified actual cash data;
    without it the answer is 'unknown', never 'yes'.
    """
    cash = db.one(conn, """SELECT amount FROM financial_facts WHERE fact_type='cash_balance' AND basis='actual'
                           AND verified=1 ORDER BY as_of DESC LIMIT 1""")
    reserve = db.one(conn, """SELECT amount FROM financial_facts WHERE fact_type='reserve' AND verified=1
                              ORDER BY as_of DESC LIMIT 1""")
    committed = db.one(conn, """SELECT COALESCE(SUM(amount),0) AS s FROM financial_facts
                                WHERE fact_type IN ('working_capital_commitment','accounts_payable','contractor_payment_due')
                                AND basis='actual' AND verified=1""")
    if not cash:
        return {"decision": "unknown", "reason": "No verified cash balance on record. Connect banking or enter a verified balance."}
    float_needed = upfront_cost + monthly_payroll_gap * (payment_delay_days / 30)
    available = cash["amount"] - (committed["s"] if committed else 0) - (reserve["amount"] if reserve else 0)
    return {"decision": "yes" if available >= float_needed else "no", "available_after_reserve": round(available, 2),
            "cash_needed": round(float_needed, 2),
            "reason": "Verified cash minus commitments and reserve vs. upfront cost plus the payment-delay gap."}


def summary(conn) -> dict:
    rows = db.q(conn, """SELECT fact_type, basis, verified, COUNT(*) n, SUM(amount) total
                         FROM financial_facts GROUP BY fact_type, basis, verified ORDER BY fact_type""")
    return {"rows": rows, "has_verified_actuals": any(r["basis"] == "actual" and r["verified"] for r in rows)}
