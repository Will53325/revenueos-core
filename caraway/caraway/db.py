"""SQLite persistence: schema, migrations, audit log helpers.

WAL mode so the dashboard can read while a scan writes. Migrations are
additive and idempotent; nothing here ever drops data.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS controls (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS sources (
  name TEXT PRIMARY KEY, kind TEXT, description TEXT, enabled INTEGER DEFAULT 1,
  requires_key INTEGER DEFAULT 0, status TEXT DEFAULT 'unknown',
  last_attempt_at TEXT, last_success_at TEXT, last_error TEXT,
  consecutive_failures INTEGER DEFAULT 0, records_last_run INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT,
  trigger TEXT, stats_json TEXT, error TEXT
);

-- Persistent work queue. Survives restarts; stale 'running' rows are requeued.
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT, status TEXT DEFAULT 'queued',
  attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3, next_run_at TEXT,
  last_error TEXT, created_at TEXT, updated_at TEXT, scan_run_id INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, next_run_at);

CREATE TABLE IF NOT EXISTS agencies (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, level TEXT, jurisdiction TEXT,
  UNIQUE(name, jurisdiction)
);

CREATE TABLE IF NOT EXISTS opportunities (
  id INTEGER PRIMARY KEY,
  dedupe_key TEXT UNIQUE NOT NULL,
  source TEXT NOT NULL, source_record_id TEXT,
  title TEXT, solicitation_number TEXT, agency_id INTEGER, agency_name TEXT,
  location TEXT, state TEXT, source_url TEXT, portal_url TEXT,
  published_at TEXT, due_at TEXT, due_at_raw TEXT, notice_type TEXT, category TEXT,
  naics TEXT, set_aside TEXT, selection_method TEXT,
  scope_text TEXT,
  estimated_value REAL, estimated_value_basis TEXT,
  site_visit TEXT DEFAULT 'unknown', site_visit_mandatory TEXT DEFAULT 'unknown', site_visit_at TEXT,
  prebid_at TEXT,
  eligibility_conditions TEXT, subcontracting_restrictions TEXT,
  insurance_requirements TEXT, bonding_requirements TEXT,
  documents_json TEXT, evidence_json TEXT, contact_json TEXT,
  confidence REAL, verification_status TEXT,
  target_category TEXT,
  lifecycle TEXT DEFAULT 'open',          -- open | expired | cancelled | closed | award_notice
  eligibility_status TEXT,                -- candidate | needs_verification | not_eligible | expired | not_an_opportunity
  go_no_go TEXT,                          -- INVESTIGATE | NO-GO | HOLD
  reasons_json TEXT, blockers_json TEXT,
  score REAL, score_model TEXT, predicted_prob REAL,
  next_action TEXT,
  llm_summary TEXT, llm_model TEXT,
  content_hash TEXT, version_count INTEGER DEFAULT 1,
  first_seen_at TEXT, last_seen_at TEXT, last_changed_at TEXT,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS opp_status ON opportunities(eligibility_status, score);
CREATE INDEX IF NOT EXISTS opp_solnum ON opportunities(solicitation_number);

CREATE TABLE IF NOT EXISTS opportunity_versions (
  id INTEGER PRIMARY KEY, opportunity_id INTEGER, content_hash TEXT, seen_at TEXT,
  change_note TEXT, raw_json TEXT
);

-- Federal award history (USAspending). Intelligence, not open opportunities.
CREATE TABLE IF NOT EXISTS award_intel (
  award_id TEXT PRIMARY KEY, generated_id TEXT, recipient TEXT, recipient_uei TEXT,
  amount REAL, agency TEXT, sub_agency TEXT, start_date TEXT, end_date TEXT,
  state TEXT, description TEXT, naics TEXT, target_category TEXT, url TEXT,
  first_seen_at TEXT, last_seen_at TEXT
);

-- Contracts ending soon = likely recompetes. A forecast, never a solicitation.
CREATE TABLE IF NOT EXISTS recompete_forecasts (
  award_id TEXT PRIMARY KEY, recipient TEXT, amount REAL, agency TEXT, sub_agency TEXT,
  end_date TEXT, state TEXT, description TEXT, naics TEXT, target_category TEXT,
  url TEXT, fit_score REAL, note TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS contractors (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, uei TEXT, city TEXT, state TEXT,
  categories TEXT, capabilities TEXT, license_info TEXT, license_verified TEXT DEFAULT 'unverified',
  contact_json TEXT, source TEXT, source_url TEXT, evidence TEXT,
  federal_award_count INTEGER DEFAULT 0, federal_award_total REAL DEFAULT 0,
  relationship_stage TEXT DEFAULT 'identified', notes TEXT,
  created_at TEXT, updated_at TEXT,
  UNIQUE(name, state)
);
CREATE TABLE IF NOT EXISTS contractor_interactions (
  id INTEGER PRIMARY KEY, contractor_id INTEGER, opportunity_id INTEGER, kind TEXT,
  direction TEXT, summary TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS quotes (
  id INTEGER PRIMARY KEY, contractor_id INTEGER, opportunity_id INTEGER, amount REAL,
  period TEXT, scope TEXT, received_at TEXT, valid_until TEXT, status TEXT, evidence TEXT
);
CREATE TABLE IF NOT EXISTS bid_estimates (
  id INTEGER PRIMARY KEY, opportunity_id INTEGER, version INTEGER, subcontract_cost REAL,
  direct_costs REAL, overhead_pct REAL, contingency_pct REAL, bid_price REAL,
  gross_profit REAL, gross_margin_pct REAL, upfront_cash_required REAL,
  assumptions TEXT, quote_ids TEXT, created_at TEXT, created_by TEXT
);
CREATE TABLE IF NOT EXISTS pursuits (
  opportunity_id INTEGER PRIMARY KEY, stage TEXT, updated_at TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS pursuit_events (
  id INTEGER PRIMARY KEY, opportunity_id INTEGER, from_stage TEXT, to_stage TEXT,
  actor TEXT, note TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY, opportunity_id INTEGER, outcome TEXT, awarded_value REAL,
  evidence TEXT, verified INTEGER DEFAULT 0, recorded_at TEXT
);
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY, opportunity_id INTEGER, model_version TEXT, score REAL,
  predicted_prob REAL, features_json TEXT, created_at TEXT,
  UNIQUE(opportunity_id, model_version)
);
CREATE TABLE IF NOT EXISTS ranking_models (
  version TEXT PRIMARY KEY, weights_json TEXT, status TEXT, created_at TEXT,
  evaluation_json TEXT, note TEXT
);

-- Strictly typed money facts. A bid value is never cash.
CREATE TABLE IF NOT EXISTS financial_facts (
  id INTEGER PRIMARY KEY,
  fact_type TEXT NOT NULL CHECK (fact_type IN (
    'bid_value','projected_profit','awarded_value','accounts_receivable','accounts_payable',
    'collected_cash','operating_expense','cash_balance','reserve','debt_obligation',
    'working_capital_commitment','insurance_capacity','bonding_capacity','contractor_payment_due')),
  basis TEXT NOT NULL CHECK (basis IN ('actual','estimate','projection')),
  amount REAL NOT NULL, as_of TEXT, opportunity_id INTEGER, counterparty TEXT,
  source TEXT, evidence TEXT, verified INTEGER DEFAULT 0, created_at TEXT
);

CREATE TABLE IF NOT EXISTS research_findings (
  id INTEGER PRIMARY KEY, topic TEXT, finding TEXT, citation_url TEXT,
  opportunity_id INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS comm_drafts (
  id INTEGER PRIMARY KEY, kind TEXT, to_name TEXT, to_address TEXT, subject TEXT, body TEXT,
  opportunity_id INTEGER, contractor_id INTEGER, status TEXT DEFAULT 'draft',
  created_at TEXT, UNIQUE(kind, opportunity_id, contractor_id)
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY, title TEXT UNIQUE, detail TEXT, assignee TEXT, priority INTEGER,
  status TEXT DEFAULT 'open', opportunity_id INTEGER, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_actions (
  id INTEGER PRIMARY KEY, agent TEXT, action TEXT, target TEXT, decision TEXT,
  rationale TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY, lesson TEXT, evidence TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS errors (
  id INTEGER PRIMARY KEY, component TEXT, message TEXT, detail TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS blocked_actions (
  id INTEGER PRIMARY KEY, action TEXT UNIQUE, reason TEXT, required_human_step TEXT,
  created_at TEXT, last_seen_at TEXT, resolved_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(path or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


SCHEMA_V2 = """
-- Forecasts and planned procurements. Never active bids.
CREATE TABLE IF NOT EXISTS forecast_opportunities (
  id INTEGER PRIMARY KEY, dedupe_key TEXT UNIQUE, source TEXT, title TEXT, agency TEXT,
  jurisdiction TEXT, state TEXT, anticipated_date TEXT, estimated_value_text TEXT,
  target_category TEXT, small_business_fit TEXT, source_url TEXT, raw_json TEXT,
  first_seen_at TEXT, last_seen_at TEXT
);
-- Contractor evidence: every fact about a contractor with where it came from.
CREATE TABLE IF NOT EXISTS contractor_evidence (
  id INTEGER PRIMARY KEY, contractor_id INTEGER, fact TEXT, value TEXT, source TEXT,
  source_url TEXT, verified_at TEXT, UNIQUE(contractor_id, fact, source)
);
CREATE TABLE IF NOT EXISTS opt_outs (
  address TEXT PRIMARY KEY, reason TEXT, created_at TEXT
);
-- Conversation memory.
CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY, opportunity_id INTEGER, contractor_id INTEGER, kind TEXT,
  status TEXT DEFAULT 'drafted', subject TEXT, gmail_thread_id TEXT, last_activity_at TEXT,
  next_followup_at TEXT, followups_sent INTEGER DEFAULT 0, created_at TEXT,
  UNIQUE(opportunity_id, contractor_id, kind)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, thread_id INTEGER, direction TEXT, status TEXT, to_address TEXT,
  from_address TEXT, subject TEXT, body_text TEXT, body_html TEXT, message_id TEXT UNIQUE,
  in_reply_to TEXT, classification TEXT, classification_detail TEXT, extracted_json TEXT,
  sent_at TEXT, received_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS bid_packages (
  id INTEGER PRIMARY KEY, opportunity_id INTEGER, version INTEGER, status TEXT,
  compliant INTEGER, checks_json TEXT, pricing_json TEXT, cashflow_json TEXT,
  policy_json TEXT, document_path TEXT, created_at TEXT, UNIQUE(opportunity_id, version)
);
-- Executive brain: every chosen action with its predicted value and later outcome.
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, opportunity_id INTEGER, action TEXT, stage_before TEXT,
  expected_value REAL, rationale TEXT, status TEXT DEFAULT 'chosen', executed_by TEXT,
  result TEXT, stage_after TEXT, created_at TEXT, evaluated_at TEXT
);
CREATE TABLE IF NOT EXISTS funnel_snapshots (
  id INTEGER PRIMARY KEY, taken_at TEXT, counts_json TEXT
);
"""

CONTRACTOR_COLUMNS = {
    "email": "TEXT", "phone": "TEXT", "website": "TEXT", "address": "TEXT", "zip": "TEXT",
    "naics": "TEXT", "certifications": "TEXT", "license_number": "TEXT", "license_status": "TEXT",
    "license_expires": "TEXT", "contact_name": "TEXT", "contact_source": "TEXT",
    "description": "TEXT", "fit_score": "REAL", "do_not_contact": "INTEGER DEFAULT 0",
    "name_key": "TEXT",
}
OPPORTUNITY_COLUMNS = {"funnel_stage": "TEXT", "funnel_updated_at": "TEXT"}
QUOTE_COLUMNS = {"message_id": "INTEGER", "basis": "TEXT", "verified": "INTEGER DEFAULT 0"}
MESSAGE_COLUMNS = {"approved_by_owner": "INTEGER DEFAULT 0"}


def _add_columns(conn, table: str, cols: dict) -> None:
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, typ in cols.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(SCHEMA_V2)
    _add_columns(conn, "contractors", CONTRACTOR_COLUMNS)
    _add_columns(conn, "opportunities", OPPORTUNITY_COLUMNS)
    _add_columns(conn, "quotes", QUOTE_COLUMNS)
    _add_columns(conn, "messages", MESSAGE_COLUMNS)
    conn.execute("CREATE INDEX IF NOT EXISTS contractors_key ON contractors(name_key, state)")
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    conn.execute("UPDATE meta SET value='2' WHERE key='schema_version' AND value<'2'")
    conn.execute("INSERT OR IGNORE INTO controls(key,value,updated_at) VALUES('paused','0',?)", (now(),))
    conn.execute("INSERT OR IGNORE INTO controls(key,value,updated_at) VALUES('email_sending_enabled','0',?)", (now(),))


def q(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def audit(conn, agent: str, action: str, target: str = "", decision: str = "", rationale: str = "") -> None:
    conn.execute("INSERT INTO agent_actions(agent,action,target,decision,rationale,created_at) VALUES(?,?,?,?,?,?)",
                 (agent, action, target, decision, rationale[:2000], now()))


def log_error(conn, component: str, message: str, detail: str = "") -> None:
    conn.execute("INSERT INTO errors(component,message,detail,created_at) VALUES(?,?,?,?)",
                 (component, message[:500], detail[:4000], now()))


def block(conn, action: str, reason: str, human_step: str) -> None:
    """Record something Caraway cannot do without a human. Idempotent."""
    conn.execute(
        """INSERT INTO blocked_actions(action,reason,required_human_step,created_at,last_seen_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(action) DO UPDATE SET reason=excluded.reason,
             required_human_step=excluded.required_human_step, last_seen_at=excluded.last_seen_at,
             resolved_at=NULL""",
        (action, reason, human_step, now(), now()))


def unblock(conn, action: str) -> None:
    conn.execute("UPDATE blocked_actions SET resolved_at=? WHERE action=? AND resolved_at IS NULL", (now(), action))


def task(conn, title: str, detail: str, assignee: str = "owner", priority: int = 2,
         opportunity_id: Optional[int] = None) -> None:
    conn.execute(
        """INSERT INTO tasks(title,detail,assignee,priority,opportunity_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(title) DO UPDATE SET detail=excluded.detail, priority=excluded.priority,
             updated_at=excluded.updated_at""",
        (title, detail, assignee, priority, opportunity_id, now(), now()))


def control(conn, key: str) -> str:
    r = one(conn, "SELECT value FROM controls WHERE key=?", (key,))
    return r["value"] if r else ""


def set_control(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO controls(key,value,updated_at) VALUES(?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                 (key, value, now()))


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)
