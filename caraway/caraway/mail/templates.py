"""Caraway-branded message templates. Plain text is the source of truth;
HTML is a light branded wrapper. No tracking pixels, no link tracking."""
from __future__ import annotations

import html

BRAND = {"ink": "#1d2a33", "accent": "#2f6f5e", "muted": "#5b6670", "line": "#dfe4e2"}


def signature(sender: dict) -> str:
    lines = [sender.get("name") or "[Owner name — set in config/comms_policy.json]",
             f"{sender.get('title') or 'Owner'}, {sender.get('company') or 'Caraway Housing LLC'}"]
    for k in ("phone", "email", "website"):
        if sender.get(k):
            lines.append(sender[k])
    lines.append(sender.get("physical_address") or "[Business mailing address — required before sending]")
    return "\n".join(lines)


def footer() -> str:
    return ("This is a business inquiry about a public contracting opportunity. It is not an offer, award or "
            "commitment. If you prefer not to hear from Caraway again, reply \"remove\" and we will not contact you.")


def nice_date(v) -> str:
    from datetime import datetime
    if not v:
        return "see notice"
    try:
        dt = datetime.fromisoformat(str(v).replace(" America/New_York", "").replace("Z", "+00:00"))
        t = "" if (dt.hour, dt.minute) == (0, 0) else dt.strftime(" at %-I:%M %p")
        return dt.strftime("%B %-d, %Y") + t + (" (UTC)" if dt.tzinfo and dt.utcoffset().total_seconds() == 0 and t else "")
    except ValueError:
        return str(v)


def _opp_block(opp: dict) -> str:
    return (f"  Opportunity: {opp.get('title')}\n"
            f"  Agency: {opp.get('agency_name') or 'see notice'}\n"
            f"  Location: {opp.get('location') or 'see notice'}\n"
            f"  Responses due to the agency: {nice_date(opp.get('due_at') or opp.get('due_at_raw'))}\n"
            f"  Solicitation #: {opp.get('solicitation_number') or 'n/a'}\n"
            f"  Official notice: {opp.get('source_url')}")


def rfq(opp: dict, c: dict, sender: dict, reply_by: str) -> tuple:
    subject = f"Quote request: {opp.get('title', '')[:70]} — Caraway Housing LLC"
    hello = f"Hello {c.get('contact_name') or c['name'] + ' team'},"
    body = f"""{hello}

I'm with Caraway Housing LLC. We found your company in the {c.get('contact_source_label') or 'public business directory'} and are putting together a response to this public solicitation:

{_opp_block(opp)}

We're looking for a qualified {c.get('category_label') or 'service'} partner to perform the work as our subcontractor. If it fits your crew, could you reply by {reply_by} with:

  1. Your price for the scope (we'll send the solicitation's scope and site details on request)
  2. Your certificate of insurance and any licenses that apply
  3. One or two similar jobs you've done, with a reference if possible

Pricing you share is used only to prepare this bid and stays confidential.

Thank you,
{signature(sender)}

{footer()}"""
    return subject, body


def followup(opp: dict, c: dict, sender: dict, n: int) -> tuple:
    subject = f"Re: Quote request: {opp.get('title', '')[:70]} — Caraway Housing LLC"
    lead = ("Just following up on the quote request below." if n == 1 else
            "One last follow-up on this. If the timing or scope doesn't work, a quick \"pass\" is completely fine.")
    body = f"""Hello {c.get('contact_name') or c['name'] + ' team'},

{lead} The agency's deadline is {nice_date(opp.get('due_at') or opp.get('due_at_raw'))}, so we need pricing a few days before that.

{_opp_block(opp)}

Thank you,
{signature(sender)}

{footer()}"""
    return subject, body


def negotiation(opp: dict, c: dict, sender: dict, quoted: float) -> tuple:
    subject = f"Re: Quote request: {opp.get('title', '')[:70]} — Caraway Housing LLC"
    body = f"""Hello {c.get('contact_name') or c['name'] + ' team'},

Thank you for your quote of ${quoted:,.2f}. This solicitation will be very price-competitive. Is there any room to sharpen that number, for example through scheduling flexibility, a longer term, or scope assumptions we should clarify?

This is not a request to commit to anything yet. We're still preparing the bid, and no award has been made.

Thank you,
{signature(sender)}

{footer()}"""
    return subject, body


def thanks(opp: dict, c: dict, sender: dict) -> tuple:
    subject = f"Re: Quote request: {opp.get('title', '')[:70]} — Caraway Housing LLC"
    body = f"""Hello {c.get('contact_name') or c['name'] + ' team'},

Thank you, we received your information. We'll be in touch if we move forward with a bid, and we'll let you know how it turns out either way.

{signature(sender)}"""
    return subject, body


def to_html(text: str) -> str:
    esc = html.escape(text).replace("\n", "<br>")
    return (f'<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;color:{BRAND["ink"]};max-width:640px">'
            f'<div style="border-bottom:3px solid {BRAND["accent"]};padding:0 0 8px;margin:0 0 16px;font-weight:700;letter-spacing:.04em">'
            f'CARAWAY HOUSING LLC</div>{esc}</div>')
