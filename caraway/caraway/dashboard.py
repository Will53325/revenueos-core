"""Local web dashboard. Standard library only, server-rendered, mobile-friendly.

Binds to 127.0.0.1 by default. All data shown comes from the database; every
string from a public source is HTML-escaped, and only http(s) links render.
State-changing actions are POSTs carrying a per-process token.
"""
from __future__ import annotations

import html
import json
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, eligibility, finance, report, scanner

TOKEN = secrets.token_urlsafe(16)
TABS = [("overview", "Status"), ("funnel", "Funnel"), ("leads", "Leads"), ("bids", "Bids"), ("outreach", "Outreach"), ("all", "All notices"), ("forecasts", "Recompetes"),
        ("contractors", "Contractors"), ("pipeline", "Pipeline"), ("tasks", "Your tasks"),
        ("drafts", "Drafts"), ("finance", "Finance"), ("logs", "Logs")]
STAGES = ["identified", "researching", "contacting_subs", "pricing", "ready_for_owner", "submitted_by_owner", "won", "lost", "dropped"]
_scan_state = {"running": False, "last": None}

CSS = """
:root{--bg:#f6f6f3;--card:#fff;--fg:#1d1f22;--muted:#5d6168;--line:#dcdcd6;--accent:#1f5f8b;--ok:#2f7d4f;--warn:#a86a00;--bad:#b23a3a;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#15171a;--card:#1d2024;--fg:#e8e8e4;--muted:#a2a6ad;--line:#33373d;--accent:#7db7e0;--ok:#6cc290;--warn:#e2ac4a;--bad:#e57878;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
header{position:sticky;top:0;background:var(--card);border-bottom:1px solid var(--line);z-index:5}
.top{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:10px max(16px,env(safe-area-inset-left))}
.brand{font-weight:700;letter-spacing:.02em}nav{display:flex;gap:4px;overflow-x:auto;padding:0 12px 8px}
nav a{white-space:nowrap;padding:6px 10px;border-radius:6px;color:var(--muted);text-decoration:none;font-size:14px}
nav a.on{background:var(--accent);color:var(--card)}main{max-width:1100px;margin:0 auto;padding:16px max(16px,env(safe-area-inset-left)) 48px}
h1{font-size:20px;margin:8px 0 12px}h2{font-size:16px;margin:20px 0 8px}.muted{color:var(--muted)}small{font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}.kpi b{display:block;font-size:22px}
.pill{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);margin-right:4px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}a{color:var(--accent)}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.tw{overflow-x:auto}button,select{font:inherit;padding:7px 12px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);min-height:38px}
button.primary{background:var(--accent);color:var(--card);border-color:var(--accent)}ul{margin:6px 0;padding-left:20px}
details summary{cursor:pointer;color:var(--accent)}pre{white-space:pre-wrap;font-size:13px}.label{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
"""


def e(v) -> str:
    return html.escape("" if v is None else str(v))


def link(url, text=None) -> str:
    if not url or not str(url).startswith(("http://", "https://")):
        return e(text or "")
    return f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{e(text or url)}</a>'


def dl_text(d) -> str:
    if d is None:
        return '<span class="warn">no firm deadline</span>'
    cls = "bad" if d < 7 else ("warn" if d < 14 else "ok")
    return f'<span class="{cls}">{d:.0f} days left</span>'


def form(action: str, label: str, extra: str = "", primary=False) -> str:
    return (f'<form method="post" action="/{action}" style="display:inline">'
            f'<input type="hidden" name="token" value="{TOKEN}">{extra}'
            f'<button class="{"primary" if primary else ""}">{e(label)}</button></form>')


def page(tab: str, body: str) -> str:
    nav = "".join(f'<a class="{"on" if t == tab else ""}" href="/?tab={t}">{e(n)}</a>' for t, n in TABS)
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"
            f"<title>Caraway</title><style>{CSS}</style></head><body><header><div class='top'><span class='brand'>CARAWAY</span>"
            f"<small class='muted'>Acquisition intelligence · local</small></div><nav>{nav}</nav></header><main>{body}</main></body></html>")


def lead_card(conn, r: dict) -> str:
    blockers = r.get("blockers") or json.loads(r.get("blockers_json") or "[]")
    docs = json.loads(r.get("documents_json") or "[]")
    ev = json.loads(r.get("evidence_json") or "[]")
    return f"""<div class="card"><div class="label">{e(r['agency_name'])} · {e(r['location'])} · {e(r['source'])}</div>
      <b>{e(r['title'])}</b><br>
      <span class="pill">score {e(r['score'])}</span><span class="pill">{e(r['target_category'])}</span>
      <span class="pill">{e(r['go_no_go'])}</span> {dl_text(eligibility.days_left(r['due_at']))}
      <div><small>Due: {e(r['due_at'] or r['due_at_raw'] or 'not published')} · Solicitation: {e(r['solicitation_number'] or 'n/a')}
      · Est. value: {e('$%s (%s)' % (format(r['estimated_value'], ',.0f'), r['estimated_value_basis']) if r['estimated_value'] else 'not published')}</small></div>
      <div><small>Official record: {link(r['source_url'], 'open source')} {(' · City Record page: ' + link(r['portal_url'], 'open')) if r.get('portal_url') else ''}</small></div>
      <div><small><b>Next action:</b> {e(r['next_action'])}</small></div>
      <details><summary>Blockers to verify ({len(blockers)})</summary><ul>{''.join(f'<li>{e(b)}</li>' for b in blockers)}</ul></details>
      <details><summary>Requirements found in notice text</summary><ul>
        <li>Site visit: {e(r['site_visit'])} (mandatory: {e(r['site_visit_mandatory'])}) {e(r['site_visit_at'] or '')}</li>
        <li>Bonding: {e(r['bonding_requirements'])}</li><li>Insurance: {e(r['insurance_requirements'])}</li>
        <li>Subcontracting: {e(r['subcontracting_restrictions'])}</li><li>Conditions: {e(r['eligibility_conditions'])}</li>
        <li>Documents: {', '.join(link(d.get('url'), d.get('name')) for d in docs) or 'none listed'}</li></ul>
        {''.join(f"<p><small class='muted'>[{e(x['field'])}] {e(x['excerpt'])}</small></p>" for x in ev[:6])}
        <p><small class='muted'>Verification: {e(r['verification_status'])}. Unknown means not found in the notice text, not absent.</small></p></details>
      {('<p><small><b>Local AI summary</b> (' + e(r['llm_model']) + '): ' + e(r['llm_summary']) + '</small></p>') if r.get('llm_summary') else ''}
      <div style="margin-top:6px">{form('stage', 'Set stage', f'<input type=hidden name=id value={r["id"]}><select name=stage>' + ''.join(f'<option>{s}</option>' for s in STAGES) + '</select>')}</div></div>"""


def render(tab: str, params: dict) -> str:
    conn = db.connect(); db.init(conn)
    try:
        if tab == "overview":
            st = report.status(conn)
            last = st["last_successful_scan"] or {}
            srows = "".join(f"<tr><td>{e(s['name'])}</td><td class='{'ok' if s['status']=='ok' else 'bad' if s['status'] in ('error','blocked') else 'muted'}'>{e(s['status'])}</td>"
                            f"<td>{e(s['last_success_at'] or 'never')}</td><td>{e(s['records_last_run'])}</td><td><small>{e(s['last_error'] or '')}</small></td></tr>" for s in st["sources"])
            blk = "".join(f"<li><b>{e(b['action'])}</b>: {e(b['reason'])}<br><small>{e(b['required_human_step'])}</small></li>" for b in st["open_blockers"])
            run = "<span class='warn'>scan running…</span>" if _scan_state["running"] else form("scan", "Run scan now", primary=True)
            ctl = form("resume", "Resume automation") if st["paused"] else form("pause", "Pause all automation")
            by = st["by_eligibility"]
            return page(tab, f"""<h1>Operational status</h1>
              <div class="card">{'<b class="bad">PAUSED</b> · ' if st['paused'] else '<b class="ok">Active</b> · '}
               Last successful scan: <b>{e(last.get('finished_at', 'never'))}</b> ({e(last.get('trigger', ''))}) <div style="margin-top:8px">{run} {ctl}</div>
               <small class="muted">{e(_scan_state['last'] or '')}</small></div>
              <div class="grid"><div class="card kpi"><span class="label">Candidate leads</span><b>{by.get('candidate', 0)}</b></div>
               <div class="card kpi"><span class="label">Rejected (not eligible)</span><b>{by.get('not_eligible', 0)}</b></div>
               <div class="card kpi"><span class="label">Expired / quarantined</span><b>{by.get('expired', 0)}</b></div>
               <div class="card kpi"><span class="label">Federal recompetes (forecast)</span><b>{st['recompete_forecasts']}</b></div>
               <div class="card kpi"><span class="label">Contractors on file</span><b>{st['contractors']}</b></div></div>
              <p class="muted"><small>Counts are records from official public sources. Nothing here is revenue, an award, or a win.</small></p>
              <h2>Sources</h2><div class="tw"><table><tr><th>Source</th><th>Status</th><th>Last success</th><th>Records</th><th>Error</th></tr>{srows}</table></div>
              <h2>Blocked integrations</h2><ul>{blk or '<li>None</li>'}</ul>""")
        if tab == "funnel":
            from . import brain
            fr = brain.funnel_report(conn)
            rows = "".join(f"<tr><td>{e(st)}</td><td><b>{fr['reached'][st]}</b></td><td>{fr['at_stage'][st]}</td></tr>" for st in brain.STAGES)
            conv = "".join(f"<li>{e(k)}: {'n/a' if v is None else f'{v * 100:.0f}%'}</li>" for k, v in fr["conversion"].items())
            acts = brain.plan(conn, 15)
            arows = "".join(f"<tr><td>{a['expected_value']:,.0f}</td><td>{e(a['action'])}<br><small class='muted'>{'Caraway' if a['action'] in brain.AUTONOMOUS else 'You'}</small></td>"
                            f"<td>#{a['opportunity_id']} {e(a['title'][:70])}<br><small class='muted'>{e(a['why'])} · p={a['p']} ({a['p_source']}) · value: {e(a['value_basis'])}</small></td></tr>" for a in acts)
            learn = fr["decision_learning"]
            dec = db.q(conn, "SELECT status, COUNT(*) n FROM decisions GROUP BY status")
            return page(tab, f"""<h1>Pipeline to revenue</h1>
              <p class='muted'><small>Counts are opportunities, from recorded evidence. "Reached" means got at least that far. Collected cash counts only verified actuals.</small></p>
              <div class='grid'><div class='card kpi'><span class='label'>Verified collected cash</span><b>${fr['collected_cash_verified']:,.0f}</b></div>
              <div class='card kpi'><span class='label'>Quotes received</span><b>{fr['reached']['quote_received']}</b></div>
              <div class='card kpi'><span class='label'>Compliant bids</span><b>{fr['reached']['bid_ready']}</b></div>
              <div class='card kpi'><span class='label'>Awards</span><b>{fr['reached']['awarded']}</b></div></div>
              <div class='tw'><table><tr><th>Stage</th><th>Reached</th><th>Currently here</th></tr>{rows}</table></div>
              <h2>Conversion</h2><ul>{conv}</ul>
              <h2>Next best actions</h2><p class='muted'><small>Value = probability the action advances the lead × estimated profit if won × urgency, spread over the stages left. Estimates, not revenue.</small></p>
              <div class='tw'><table><tr><th>Value</th><th>Action / who</th><th>Opportunity</th></tr>{arows or '<tr><td colspan=3>No open candidates.</td></tr>'}</table></div>
              <h2>Decision record</h2><p>{', '.join(f"{e(d['status'])}: {d['n']}" for d in dec) or 'none'} · measured action rates: {e(learn) if learn else 'not enough evaluated decisions yet (need 10 per action)'}</p>""")
        if tab == "bids":
            rows = db.q(conn, """SELECT b.*, o.title, o.due_at FROM bid_packages b JOIN opportunities o ON o.id=b.opportunity_id
                                 WHERE b.version=(SELECT MAX(version) FROM bid_packages x WHERE x.opportunity_id=b.opportunity_id) ORDER BY o.due_at""")
            cards = ""
            for b in rows:
                checks, pr = json.loads(b["checks_json"]), json.loads(b["pricing_json"])
                bench = pr.get("benchmark", {})
                lis = "".join(f"<li><span class='{ {'pass': 'ok', 'fail': 'bad'}.get(c['status'], 'warn')}'>{e(c['status'].upper())}</span> {e(c['check'])}: <small>{e(c['evidence'])}{(' → ' + e(c['fix'])) if c['status'] != 'pass' and c['fix'] else ''}</small></li>" for c in checks)
                price = (f"Bid ${pr['bid_price']:,.0f}/yr · margin {pr['gross_margin_pct']}% · sub {e(pr.get('subcontractor'))}" if pr.get("bid_price")
                         else f"No quote yet. Target subcontract cost: {('$' + format(pr['target_subcontract_cost'], ',.0f') + '/yr') if pr.get('target_subcontract_cost') else 'n/a'}")
                cards += (f"<div class='card'><b>#{b['opportunity_id']} {e(b['title'])}</b><br><span class='pill'>{e(b['status'])}</span><span class='pill'>v{b['version']}</span> "
                          f"{dl_text(eligibility.days_left(b['due_at']))}<div><small>{price}</small></div>"
                          f"<div><small class='muted'>Benchmark: {e(bench.get('basis'))}{(' · median $' + format(bench['median_annual'], ',.0f') + '/yr') if bench.get('median_annual') else ''}</small></div>"
                          f"<details><summary>Checks ({sum(c['status'] == 'pass' for c in checks)}/{len(checks)} pass)</summary><ul>{lis}</ul></details></div>")
            return page(tab, f"<h1>Bid packages</h1><p class='muted'><small>Compliant only when every check passes. Unknown is not a pass. Submission is always yours.</small></p>{cards or '<p>No packages yet.</p>'}")
        if tab == "outreach":
            ths = db.q(conn, """SELECT t.*, c.name, c.email, c.phone, o.title FROM threads t JOIN contractors c ON c.id=t.contractor_id
                                JOIN opportunities o ON o.id=t.opportunity_id ORDER BY t.last_activity_at DESC LIMIT 100""")
            cards = ""
            for t in ths:
                msgs = db.q(conn, "SELECT * FROM messages WHERE thread_id=? ORDER BY id", (t["id"],))
                ml = "".join(f"<details><summary>{e(m['direction'])} · {e(m['classification'])} · {e(m['status'])}{(' — ' + e(m['classification_detail'])) if m['classification_detail'] else ''}</summary><pre>{e(m['body_text'])}</pre></details>" for m in msgs)
                cards += f"<div class='card'><b>{e(t['name'])}</b> <small>{e(t['email'])} · {e(t['phone'])}</small><br><small>#{t['opportunity_id']} {e(t['title'][:80])}</small><br><span class='pill'>{e(t['status'])}</span>{ml}</div>"
            from .mail import engine as mail_engine, transport as mail_tr
            pol = mail_engine.load_policy()
            gate = (f"Owner enabled: {'yes' if db.control(conn, 'email_sending_enabled') == '1' else 'no'} · policy enabled: {'yes' if pol.get('enabled') else 'no'} · "
                    f"Gmail connected: {'yes' if mail_tr.GmailTransport().configured() else 'no'}")
            return page(tab, f"<h1>Outreach</h1><div class='card'><small>{gate}. Nothing is sent unless all three are yes and the message kind is approved.</small></div>{cards or '<p>No conversations yet.</p>'}")
        if tab == "leads":
            rows = report.leads(conn, 50)
            return page(tab, "<h1>Ranked actionable leads</h1><p class='muted'><small>INVESTIGATE means the notice fits Caraway's model and nothing disqualifying was found in its text. It is not permission to bid: every lead still has blockers to verify.</small></p>"
                        + ("".join(lead_card(conn, r) for r in rows) or "<p>No candidates yet.</p>"))
        if tab == "all":
            st = params.get("status", ["not_eligible"])[0]
            rows = db.q(conn, """SELECT id,source,title,agency_name,due_at,due_at_raw,eligibility_status,reasons_json,source_url,version_count
                                 FROM opportunities WHERE eligibility_status=? ORDER BY due_at DESC LIMIT 500""", (st,))
            opts = "".join(f'<a class="pill" href="/?tab=all&status={s}">{s}</a>' for s in ("candidate", "not_eligible", "expired", "not_an_opportunity"))
            trs = "".join(f"<tr><td>{e(r['source'])}</td><td>{link(r['source_url'], r['title'])}<br><small class='muted'>{e(r['agency_name'])}</small></td>"
                          f"<td><small>{e(r['due_at'] or r['due_at_raw'])}</small></td><td><small>{'<br>'.join(e(x) for x in json.loads(r['reasons_json'] or '[]'))}</small></td>"
                          f"<td>{e(r['version_count'])}</td></tr>" for r in rows)
            return page(tab, f"<h1>All notices: {e(st)} ({len(rows)})</h1><p>{opts}</p><div class='tw'><table><tr><th>Source</th><th>Notice</th><th>Due</th><th>Reasons</th><th>Versions</th></tr>{trs}</table></div>")
        if tab == "forecasts":
            rows = db.q(conn, "SELECT * FROM recompete_forecasts ORDER BY fit_score DESC, end_date LIMIT 300")
            trs = "".join(f"<tr><td>{e(r['end_date'])}</td><td>{link(r['url'], r['description'][:120] or r['award_id'])}<br><small class='muted'>{e(r['sub_agency'])} · {e(r['state'])}</small></td>"
                          f"<td>{e(r['target_category'])}</td><td>${(r['amount'] or 0):,.0f}</td><td>{e(r['recipient'])}</td></tr>" for r in rows)
            fc = db.q(conn, "SELECT * FROM forecast_opportunities ORDER BY anticipated_date LIMIT 200")
            frows = "".join(f"<tr><td>{e(r['anticipated_date'])}</td><td>{link(r['source_url'], r['title'][:140])}<br><small class='muted'>{e(r['agency'])} · {e(r['state'])}</small></td>"
                            f"<td>{e(r['target_category'])}</td><td><small>{e(r['small_business_fit'] or r['estimated_value_text'] or '')}</small></td></tr>" for r in fc)
            body_local = f"<h1>Local forecasts and expiring contracts ({len(fc)})</h1><p class='muted'><small>Planned buys and contracts nearing the end of their term. Not open solicitations.</small></p><div class='tw'><table><tr><th>When</th><th>What</th><th>Category</th><th>Notes</th></tr>{frows}</table></div>"
            return page(tab, body_local + f"<h1>Federal contracts ending soon ({len(rows)})</h1><p class='muted'><small>From USAspending.gov. These are <b>forecasts</b>: the current contract ends and the work is often re-solicited on SAM.gov. Amount is the incumbent's total obligated value, not Caraway revenue.</small></p><div class='tw'><table><tr><th>Ends</th><th>Award</th><th>Category</th><th>Obligated</th><th>Incumbent</th></tr>{trs}</table></div>")
        if tab == "contractors":
            rows = db.q(conn, "SELECT * FROM contractors ORDER BY fit_score DESC, federal_award_count DESC LIMIT 400")
            total = db.one(conn, "SELECT COUNT(*) n, SUM(email IS NOT NULL) e, SUM(phone IS NOT NULL) p FROM contractors")
            trs = "".join(f"<tr><td>{e(r['name'])}<br><small class='muted'>{e(r['certifications'] or '')} {e(r['license_number'] or '')}</small></td><td>{e(r['state'])}</td><td>{e(r['categories'])}</td>"
                          f"<td><small>{e(r['email'] or '')}<br>{e(r['phone'] or '')}</small></td><td>{(r['fit_score'] or 0):.0f}</td><td>{link(r['source_url'], r['source'])}</td><td>{e(r['relationship_stage'])}</td></tr>" for r in rows)
            return page(tab, f"<h1>Contractor CRM ({total['n']}; {total['e']} with email, {total['p']} with phone)</h1><p class='muted'><small>From official directories: NYC SBS certified businesses, Delaware Office of Supplier Diversity, NY DEC pesticide registrations, LA business tax registrations, federal award recipients, county contract holders. Contact details are exactly as the business registered them. Top 400 by fit shown.</small></p><div class='tw'><table><tr><th>Firm</th><th>State</th><th>Category</th><th>Contact</th><th>Fit</th><th>Source</th><th>Stage</th></tr>{trs}</table></div>")
        if tab == "pipeline":
            rows = db.q(conn, """SELECT p.stage, p.updated_at, o.id, o.title, o.due_at, o.source_url FROM pursuits p JOIN opportunities o ON o.id=p.opportunity_id
                                 ORDER BY p.stage, o.due_at""")
            trs = "".join(f"<tr><td>{e(r['stage'])}</td><td>{link(r['source_url'], r['title'])}</td><td>{e(r['due_at'])}</td></tr>" for r in rows)
            return page(tab, f"<h1>Pursuit pipeline</h1><div class='tw'><table><tr><th>Stage</th><th>Opportunity</th><th>Due</th></tr>{trs}</table></div>")
        if tab == "tasks":
            rows = db.q(conn, "SELECT * FROM tasks WHERE status='open' ORDER BY assignee='owner' DESC, priority, id")
            items = "".join(f"<div class='card'><span class='pill'>P{e(r['priority'])}</span><span class='pill'>{e(r['assignee'])}</span><b>{e(r['title'])}</b><pre>{e(r['detail'])}</pre></div>" for r in rows)
            return page(tab, f"<h1>Tasks requiring human action ({len(rows)})</h1>{items}")
        if tab == "drafts":
            rows = db.q(conn, "SELECT * FROM comm_drafts ORDER BY id DESC LIMIT 100")
            items = "".join(f"<div class='card'><span class='pill'>{e(r['status'])}</span><b>{e(r['subject'])}</b><br><small>To: {e(r['to_name'])} ({e(r['to_address'] or 'address not researched')})</small><details><summary>Body</summary><pre>{e(r['body'])}</pre></details></div>" for r in rows)
            return page(tab, f"<h1>Communication drafts</h1><p class='muted'><small>Drafts only. Sending is disabled until a dedicated business email is connected and you approve sending rules.</small></p>{items}")
        if tab == "finance":
            fs = finance.summary(conn)
            trs = "".join(f"<tr><td>{e(r['fact_type'])}</td><td>{e(r['basis'])}</td><td>{'yes' if r['verified'] else 'no'}</td><td>{e(r['n'])}</td><td>${(r['total'] or 0):,.2f}</td></tr>" for r in fs["rows"])
            return page(tab, f"<h1>Finance</h1><div class='card'>{'Verified actuals on file.' if fs['has_verified_actuals'] else '<b>No verified financial data yet.</b> Nothing is shown as revenue, profit or cash until it comes from a verified record (bank, accounting, or owner-entered with evidence).'}</div>"
                             f"<div class='tw'><table><tr><th>Fact type</th><th>Basis</th><th>Verified</th><th>Rows</th><th>Total</th></tr>{trs or '<tr><td colspan=5>No rows.</td></tr>'}</table></div>"
                             "<p class='muted'><small>Actual, estimate and projection are stored separately and never added together. Bid value, awarded value, receivables and collected cash are separate fact types.</small></p>")
        if tab == "logs":
            runs = db.q(conn, "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 20")
            errs = db.q(conn, "SELECT * FROM errors ORDER BY id DESC LIMIT 30")
            acts = db.q(conn, "SELECT * FROM agent_actions ORDER BY id DESC LIMIT 40")
            r1 = "".join(f"<tr><td>{e(r['id'])}</td><td>{e(r['started_at'])}</td><td>{e(r['status'])}</td><td>{e(r['trigger'])}</td><td><details><summary>stats</summary><pre>{e(r['stats_json'])}</pre></details></td></tr>" for r in runs)
            r2 = "".join(f"<tr><td>{e(r['created_at'])}</td><td>{e(r['component'])}</td><td>{e(r['message'])}</td></tr>" for r in errs)
            r3 = "".join(f"<tr><td>{e(r['created_at'])}</td><td>{e(r['agent'])}</td><td>{e(r['action'])}</td><td>{e(r['decision'])}</td><td><small>{e((r['rationale'] or '')[:200])}</small></td></tr>" for r in acts)
            return page(tab, f"<h1>Logs</h1><h2>Scan runs</h2><div class='tw'><table>{r1}</table></div><h2>Errors</h2><div class='tw'><table>{r2 or '<tr><td>None</td></tr>'}</table></div><h2>Audit log</h2><div class='tw'><table>{r3}</table></div>")
        return page("overview", "<p>Unknown page.</p>")
    finally:
        conn.close()


def _background_scan() -> None:
    _scan_state["running"] = True
    try:
        res = scanner.run_scan("dashboard")
        _scan_state["last"] = f"Last dashboard scan: {res.get('status')}"
    except scanner.Locked:
        _scan_state["last"] = "A scan was already running."
    except Exception as ex:  # noqa: BLE001
        _scan_state["last"] = f"Scan failed: {ex}"
    finally:
        _scan_state["running"] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: str, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/health":
            return self._send(200, "ok", "text/plain")
        if u.path == "/api/status":
            conn = db.connect(); db.init(conn)
            try:
                return self._send(200, json.dumps(report.status(conn), default=str), "application/json")
            finally:
                conn.close()
        if u.path != "/":
            return self._send(404, "not found", "text/plain")
        params = urllib.parse.parse_qs(u.query)
        self._send(200, render(params.get("tab", ["overview"])[0], params))

    def do_POST(self):
        n = min(int(self.headers.get("Content-Length") or 0), 10000)
        data = urllib.parse.parse_qs(self.rfile.read(n).decode())
        if data.get("token", [""])[0] != TOKEN:
            return self._send(403, "bad token", "text/plain")
        path = urllib.parse.urlparse(self.path).path
        conn = db.connect(); db.init(conn)
        try:
            if path == "/scan" and not _scan_state["running"]:
                threading.Thread(target=_background_scan, daemon=True).start()
            elif path == "/pause":
                db.set_control(conn, "paused", "1"); db.audit(conn, "owner", "pause", "dashboard")
            elif path == "/resume":
                db.set_control(conn, "paused", "0"); db.audit(conn, "owner", "resume", "dashboard")
            elif path == "/stage":
                oid, stage = int(data["id"][0]), data["stage"][0]
                if stage in STAGES:
                    old = db.one(conn, "SELECT stage FROM pursuits WHERE opportunity_id=?", (oid,))
                    conn.execute("INSERT INTO pursuits(opportunity_id,stage,updated_at) VALUES(?,?,?) ON CONFLICT(opportunity_id) DO UPDATE SET stage=excluded.stage, updated_at=excluded.updated_at", (oid, stage, db.now()))
                    conn.execute("INSERT INTO pursuit_events(opportunity_id,from_stage,to_stage,actor,created_at) VALUES(?,?,?,?,?)",
                                 (oid, old and old["stage"], stage, "owner", db.now()))
        finally:
            conn.close()
        self.send_response(303)
        self.send_header("Location", "/?tab=pipeline" if path == "/stage" else "/")
        self.end_headers()


def serve(host: str = config.DASHBOARD_HOST, port: int = config.DASHBOARD_PORT) -> None:
    config.ensure_dirs()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Caraway dashboard on http://{host}:{port}", flush=True)
    httpd.serve_forever()
