"""Classify inbound replies and extract quote facts. Rules first; an optional
local model may label 'other' replies, but never changes extracted money."""
from __future__ import annotations

import re
from typing import Optional

LABELS = ("bounce", "out_of_office", "unsubscribe", "declined", "quote_provided", "interested", "question", "other")
AMOUNT_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*(k\b)?\s*(per|/|a)?\s*(month|mo|year|yr|annum|visit|week|service|hour|hr|day)?", re.I)


def classify(subject: str, body: str, from_addr: str = "") -> dict:
    s, b = (subject or "").lower(), (body or "").lower()
    head = b[:1500]
    f = (from_addr or "").lower()
    if "mailer-daemon" in f or "postmaster" in f or "delivery status notification" in s or "undeliverable" in s:
        return {"label": "bounce"}
    if re.search(r"out of (the )?office|auto(matic)?[- ]?reply|i am away|on vacation", s + " " + head[:400]):
        return {"label": "out_of_office"}
    if re.search(r"^\s*(remove|unsubscribe|stop)\b|please remove me|do not contact|don't contact|take me off", head, re.M):
        return {"label": "unsubscribe"}
    if re.search(r"not interested|no thank|we('| a)?ll pass|we will pass|\bpass on\b|unable to (bid|quote|take)|can('|no)t take|"
                 r"don'?t do (that|this)|not something we|outside (our|of our) (area|scope)|too (far|busy)", head):
        return {"label": "declined"}
    amounts = extract_amounts(body)
    if amounts and re.search(r"quote|price|pricing|proposal|estimate|bid|cost|charge|rate|per (month|visit|year)", head):
        return {"label": "quote_provided", "amounts": amounts}
    if re.search(r"interested|send (me |us |over )?(the )?(scope|details|documents|specs|bid package)|happy to (quote|bid|help)|we can do", head):
        return {"label": "interested"}
    if "?" in head:
        return {"label": "question"}
    return {"label": "other"}


def extract_amounts(body: str) -> list:
    out = []
    for m in AMOUNT_RE.finditer(body or ""):
        v = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
        if v <= 0:
            continue
        period = (m.group(4) or "").lower()
        period = {"mo": "month", "yr": "year", "annum": "year", "hr": "hour"}.get(period, period) or "unspecified"
        out.append({"amount": v, "period": period, "text": m.group(0).strip()})
    return out


def annualize(amount: float, period: str) -> Optional[float]:
    return {"month": amount * 12, "year": amount, "week": amount * 52}.get(period)
