"""Opportunity ranking and measurable learning.

Score = weighted sum of transparent features (0-100). Every scored
opportunity gets a `predictions` row tied to the model version. When
verified outcomes exist, `evaluate()` reports Brier score and accuracy per
model, and `propose_and_test()` fits a candidate on older outcomes and only
adopts it if it beats the active model on held-out newer outcomes. Storing
data is not learning; adoption requires a measured improvement.
"""
from __future__ import annotations

import json
import math
from typing import Optional

from . import db

DEFAULT_VERSION = "heuristic-v1"
DEFAULT_WEIGHTS = {
    "bias": -1.0,
    "category_core": 1.6,        # janitorial / landscaping
    "category_other": 0.8,       # other target categories
    "deadline_sweet_spot": 0.9,  # 10-45 days out
    "deadline_tight": -0.6,      # 7-10 days
    "sealed_bid": 0.5,           # low-price award favors a lean newcomer
    "proposal_best_value": -0.2,
    "unrestricted": 0.3,
    "blocker_count": -0.25,      # per blocker
    "bonding_mentioned": -0.6,
    "site_visit_required": -0.3,
    "value_known_small": 0.5,    # published value within limits
    "no_firm_deadline": -0.4,
}
MIN_OUTCOMES_TO_LEARN = 20


def features(opp: dict, assessment: dict, req: dict) -> dict:
    cat = assessment.get("target_category")
    dl = assessment.get("days_left")
    sel = (opp.get("selection_method") or "").lower()
    f = {
        "bias": 1.0,
        "category_core": 1.0 if cat in ("janitorial", "landscaping") else 0.0,
        "category_other": 1.0 if cat and cat not in ("janitorial", "landscaping") else 0.0,
        "deadline_sweet_spot": 1.0 if dl is not None and 10 <= dl <= 45 else 0.0,
        "deadline_tight": 1.0 if dl is not None and 7 <= dl < 10 else 0.0,
        "sealed_bid": 1.0 if "sealed bid" in sel or "low bid" in sel else 0.0,
        "proposal_best_value": 1.0 if "proposal" in sel or "best value" in sel else 0.0,
        "unrestricted": 1.0 if not opp.get("set_aside") else 0.0,
        "blocker_count": float(len(assessment.get("blockers") or [])),
        "bonding_mentioned": 0.0 if req.get("bonding_requirements", "unknown") == "unknown" else 1.0,
        "site_visit_required": 1.0 if req.get("site_visit_mandatory") == "yes" else 0.0,
        "value_known_small": 1.0 if req.get("estimated_value") and req["estimated_value"] <= 500000 else 0.0,
        "no_firm_deadline": 1.0 if dl is None else 0.0,
    }
    return f


def active_model(conn) -> tuple:
    row = db.one(conn, "SELECT version, weights_json FROM ranking_models WHERE status='active' ORDER BY created_at DESC LIMIT 1")
    if row:
        return row["version"], json.loads(row["weights_json"])
    conn.execute("INSERT OR IGNORE INTO ranking_models(version,weights_json,status,created_at,note) VALUES(?,?,?,?,?)",
                 (DEFAULT_VERSION, json.dumps(DEFAULT_WEIGHTS), "active", db.now(),
                  "Hand-set prior. Replaced only by a candidate that wins a held-out test on verified outcomes."))
    return DEFAULT_VERSION, dict(DEFAULT_WEIGHTS)


def predict(weights: dict, feats: dict) -> float:
    z = sum(weights.get(k, 0.0) * v for k, v in feats.items())
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def score(conn, opp_id: int, opp: dict, assessment: dict, req: dict) -> tuple:
    version, weights = active_model(conn)
    feats = features(opp, assessment, req)
    p = predict(weights, feats)
    s = round(p * 100, 1)
    if assessment["eligibility_status"] != "candidate":
        s = 0.0
    conn.execute("""INSERT INTO predictions(opportunity_id,model_version,score,predicted_prob,features_json,created_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(opportunity_id,model_version) DO UPDATE SET
                    score=excluded.score, predicted_prob=excluded.predicted_prob, features_json=excluded.features_json""",
                 (opp_id, version, s, p, json.dumps(feats), db.now()))
    return s, p, version


# ---------- learning from verified outcomes ----------

POSITIVE = {"won", "shortlisted", "worth_pursuing"}


def _labeled(conn) -> list:
    return db.q(conn, """SELECT p.features_json, p.model_version, p.predicted_prob, o.outcome, o.recorded_at
                         FROM outcomes o JOIN predictions p ON p.opportunity_id=o.opportunity_id
                         WHERE o.verified=1 ORDER BY o.recorded_at""")


def evaluate(conn, weights: Optional[dict] = None, rows: Optional[list] = None) -> dict:
    rows = rows if rows is not None else _labeled(conn)
    if weights is None:
        _, weights = active_model(conn)
    if not rows:
        return {"n": 0, "brier": None, "accuracy": None}
    brier, correct = 0.0, 0
    for r in rows:
        y = 1.0 if r["outcome"] in POSITIVE else 0.0
        p = predict(weights, json.loads(r["features_json"]))
        brier += (p - y) ** 2
        correct += int((p >= 0.5) == (y == 1.0))
    return {"n": len(rows), "brier": round(brier / len(rows), 4), "accuracy": round(correct / len(rows), 3)}


def _fit(rows: list, start: dict, epochs: int = 400, lr: float = 0.05, l2: float = 0.01) -> dict:
    w = dict(start)
    for _ in range(epochs):
        grad = {k: 0.0 for k in w}
        for r in rows:
            f = json.loads(r["features_json"])
            y = 1.0 if r["outcome"] in POSITIVE else 0.0
            err = predict(w, f) - y
            for k, v in f.items():
                grad[k] = grad.get(k, 0.0) + err * v
        for k in w:
            w[k] -= lr * (grad.get(k, 0.0) / len(rows) + l2 * w[k])
    return w


def propose_and_test(conn) -> dict:
    rows = _labeled(conn)
    if len(rows) < MIN_OUTCOMES_TO_LEARN:
        return {"adopted": False, "reason": f"{len(rows)} verified outcomes; need {MIN_OUTCOMES_TO_LEARN} before learning."}
    cut = int(len(rows) * 0.7)
    train, test = rows[:cut], rows[cut:]
    version, active_w = active_model(conn)
    cand_w = _fit(train, active_w)
    base, cand = evaluate(conn, active_w, test), evaluate(conn, cand_w, test)
    new_version = f"learned-{db.now()[:19]}"
    adopted = cand["brier"] is not None and base["brier"] is not None and cand["brier"] < base["brier"] - 0.005
    conn.execute("INSERT INTO ranking_models(version,weights_json,status,created_at,evaluation_json,note) VALUES(?,?,?,?,?,?)",
                 (new_version, json.dumps(cand_w), "active" if adopted else "rejected", db.now(),
                  json.dumps({"baseline": base, "candidate": cand, "train_n": len(train)}),
                  "Adopted: lower held-out Brier score." if adopted else "Rejected: did not beat the active model."))
    if adopted:
        conn.execute("UPDATE ranking_models SET status='retired' WHERE version=?", (version,))
        db.audit(conn, "learning", "adopt_model", new_version, "ADOPTED", json.dumps({"baseline": base, "candidate": cand}))
    return {"adopted": adopted, "baseline": base, "candidate": cand, "version": new_version}
