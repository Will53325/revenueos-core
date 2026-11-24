# Caraway — Acquisition Intelligence

Local-first system for Caraway Housing LLC that finds public contracting opportunities,
screens them against what Caraway can actually deliver today, ranks them, and keeps
scanning on a schedule. Pure Python standard library + SQLite: nothing to `pip install`,
no paid APIs, comfortable on an 8 GB M1 MacBook Air.

It **reads public data and drafts**. It never submits bids, signs, attests, spends money,
registers accounts or sends email. Those are blocked in code (`caraway/compliance.py`).

## Quick start (macOS)

```bash
git clone https://github.com/Will53325/revenueos-core.git
cd revenueos-core && git checkout claude/upbeat-albattani-7bokbw && cd caraway
python3 -m caraway scan          # one live scan now (~1–2 min)
python3 -m caraway serve         # dashboard at http://127.0.0.1:8787
```

Requires Python 3.9+ (`python3 --version`; if missing run `xcode-select --install`).

### Run it automatically (launchd, no sudo)

```bash
./scripts/install_macos.sh
```

This loads two user agents in `~/Library/LaunchAgents`:

| Agent | What it does |
|---|---|
| `com.caraway.scanner` | Runs a scan at load and every 4 hours. If the Mac was asleep, launchd runs it on wake. Low CPU/IO priority. |
| `com.caraway.dashboard` | Keeps the dashboard up at http://127.0.0.1:8787 (restarts if it crashes). |

Remove with `./scripts/uninstall_macos.sh` (keeps data).

## Commands

```bash
python3 -m caraway scan                   # full cycle: discover, screen, research, brain, mail (launchd runs this every 4h)
python3 -m caraway brain funnel           # opportunities at each stage, conversion, verified collected cash
python3 -m caraway brain plan             # ranked next actions with expected value
python3 -m caraway leads -n 20            # ranked candidate leads
python3 -m caraway contractors list --category janitorial --state DE
python3 -m caraway bid prepare 458        # bid package: pricing, cash flow, compliance checks
python3 -m caraway quote add --opportunity 458 --contractor 3943 --amount 2400 --period month --evidence "call 11/25, Angie"
python3 -m caraway outcome 458 lost --evidence "bid tab posted 1/10"
python3 -m caraway mail status | drafts | approve 12 13 | enable | disable
python3 -m caraway doctor                 # health: stale scans, failed jobs, launchd, disk, DB integrity
python3 -m caraway pause | resume
python3 -m unittest discover -s tests -v
```

**Emergency stop:** `touch var/STOP` halts every scheduled run until the file is deleted.

## The acquisition loop

Every scan ends with an executive-brain cycle that moves opportunities toward revenue:

```
discovered -> qualified -> contractors_identified -> quote_requested -> quote_received
           -> bid_ready -> submitted -> awarded -> collected
```

Stages are derived from evidence (a sent email, a stored quote, a compliant package, a verified
award, verified cash), never from the system's own claims. The brain values each possible next
action (probability it advances the lead x estimated profit if won x urgency), does the ones it
may do alone (contractor research, RFQ drafts, follow-ups, bid packages) and turns the rest into
specific owner tasks (call lists with phone numbers, sending authorization, blockers, submission).
Every decision is logged; after 7 days it is scored as advanced / no progress, and once an action
type has 10 scored decisions its measured rate replaces the prior.

### Contractor research

Official directories only, with every fact stored in `contractor_evidence`:
NYC SBS certified businesses (phone, website, certifications, past jobs), Delaware Office of
Supplier Diversity (email, phone), NY DEC registered pesticide businesses (license + expiry),
LA City business tax registrations (NAICS, address), USAspending award recipients, Montgomery
County contract holders. Email addresses exist only where the business registered one with a
government directory; Caraway does not guess or scrape them. Opt-outs are permanent.

### Email (Gmail) — off until you turn it on

1. Google Cloud Console → create a project → enable the Gmail API → OAuth client type **Desktop app** → download `client_secret.json`.
2. `python3 -m caraway mail connect --client-secret ~/Downloads/client_secret.json` (token goes to the macOS Keychain).
3. `cp config/comms_policy.json config/comms_policy.local.json`, fill in the sender name, email,
   phone and **business mailing address**, set `"enabled": true`.
4. Review drafts (`mail drafts` or the Outreach tab). Either approve individual messages
   (`mail approve ID`) or list kinds in `auto_send_kinds` (e.g. `["rfq","followup"]`) as a standing policy.
5. `python3 -m caraway mail enable`.

Every send also passes daily caps, quiet hours, opt-out and per-contractor limits. Replies are
pulled from Gmail each cycle (or `mail import FOLDER` for exported .eml files), classified
(quote, interested, question, declined, unsubscribe, bounce, out-of-office), and quotes are stored
as unverified until you confirm them. Negotiation emails are drafted only if enabled in policy,
never accept anything, and are always marked non-binding.

### Bid packages

`config/bid_policy.json` holds Caraway's pricing assumptions. Each package records: a market
benchmark from comparable federal awards, the target subcontract cost, the lowest quote, bid
price and margin, a day-level working-capital model (agency pays late, subcontractor paid sooner),
and pass/fail/unknown checks with evidence (registration, insurance, set-aside, FAR 52.219-14
limitation on subcontracting, participation goals, licenses, margin floor, working capital,
bonding, solicitation review). Unknown is never a pass. Packages are written to `var/bids/`.

`config/authority_policy.json` describes standing owner authorizations (e.g. auto-submit a
small, low-risk bid). Caraway evaluates every package against it and shows which rule would
apply. Bid submission itself stays blocked in code in this version.

## Verify a Mac deployment

```bash
./scripts/install_macos.sh && ./scripts/verify_macos.sh
```

`verify_macos.sh` checks Python, runs the tests, confirms both launchd agents are loaded, triggers a
real scan through launchd and waits for it to finish, kills the dashboard to confirm launchd restarts
it, and runs `doctor`. Failed jobs, stale scans or unloaded agents raise a macOS notification and a
P1 task, and are written to `var/health.json`.

## Move to a private repository

```bash
caraway/scripts/migrate_to_private_repo.sh https://github.com/<you>/caraway.git
```

Create the empty private repo first. The script extracts only `caraway/` with its history
(`git subtree split`) and pushes it as `main`; nothing else in revenueos-core is copied or changed.
In the new repo, the project lives at the root (run commands there instead of in `caraway/`).

## Recovery

| Problem | Fix |
|---|---|
| Dashboard not loading | `launchctl kickstart -k gui/$(id -u)/com.caraway.dashboard`, or run `python3 -m caraway serve` |
| Scans not happening | `cat var/logs/launchd-scan.err var/logs/caraway.log`; re-run `./scripts/install_macos.sh` |
| A source keeps failing | Dashboard > Status > Sources shows the error. Other sources keep working. |
| Crash mid-scan | Nothing to do: the next run requeues stale jobs and the lock prevents overlaps. |
| Corrupt/lost DB | `var/caraway.db` is the only state. Back it up with `cp var/caraway.db ~/caraway-backup.db`. A fresh scan rebuilds opportunities from the sources. |

## Sources

| Source | Kind | Key? | Status |
|---|---|---|---|
| Baton Rouge / East Baton Rouge Parish bid openings (`data.brla.gov u9zk-8nix`) | Open solicitations | No | Working |
| Cook County IL buying plan, San Diego County forecast, NYC M/WBE upcoming, Montgomery County expiring contracts | Forecasts (never treated as bids) | No | Working |
| NYC City Record Online (NYC Open Data `dg92-zbpx`) | Open solicitations | No | Working |
| Los Angeles RAMP open bids (`data.lacity.org hf3r-utnq`) | Open solicitations | No | Working |
| Montgomery County, MD solicitations (`eeq6-nnwe`) | Open solicitations | No | Working |
| State of Delaware open bids (`data.delaware.gov 2hnj-zwix`) | Open solicitations | No | Working |
| USAspending.gov API | Federal award history, recompete forecasts, contractors | No | Working |
| SAM.gov Get Opportunities API v2 | Federal open solicitations | **Yes (free)** | Built, blocked until a key is added |

Only allowlisted hosts can be contacted (`config.ALLOWED_HOSTS`). Adding a government is a
config entry in `caraway/sources/socrata_bids.py` plus its host in the allowlist.

### Enable SAM.gov (biggest single improvement)

1. Sign in at sam.gov (free login.gov account). Account Details → request a **public API key**.
2. Edit `~/Library/LaunchAgents/com.caraway.scanner.plist`, uncomment the `CARAWAY_SAM_API_KEY` lines, paste the key.
3. `./scripts/install_macos.sh` again (reloads). The key stays on your Mac; it is never written to the database or logs.

### Optional local AI (Ollama)

Rules make every decision. The model only writes plain-English summaries of top leads.

```bash
brew install ollama          # or the installer from ollama.com
ollama pull qwen2.5:3b       # ~1.9 GB; fits 8 GB RAM next to a browser
ollama serve                 # Caraway auto-detects it on the next scan
```

Model swaps: `CARAWAY_OLLAMA_MODEL=llama3.2:3b`. Disable: `CARAWAY_LLM=none`. The provider
interface (`caraway/llm.py`) is where a stronger local or commercial model would plug in.

## How screening works

Each notice gets one of: `candidate`, `not_eligible`, `expired`, `not_an_opportunity`.
`candidate` means the work fits Caraway's model (janitorial, grounds, light facility work,
moving/hauling, pest control, delivered through subcontractors) and nothing in the notice
text disqualifies it. It is **never** "go": every candidate lists *blockers* (registration,
insurance, licenses, set-aside status, geography) that a human must verify. Unknown means
"not found in the notice text", not "not required".

Hard rejections: deadline passed or under 7 days, missed mandatory site visit, construction
or licensed trades, software/IT, set-asides Caraway does not hold, cancellations, award notices,
material/supply purchases, estimates above the pursuit limit.

Edit `config/owner_profile.json` (copy to `owner_profile.local.json`, which is gitignored) as
facts get verified. Example: set `"sam_gov_active": "verified"` once the registration is active.

## Learning (measured, not assumed)

Every scored lead stores a prediction with its model version. Record real outcomes with
`caraway outcome`. After 20 verified outcomes, `caraway evaluate` fits a candidate model on
older outcomes and adopts it **only** if it beats the active model's Brier score on newer
held-out outcomes. Every adoption or rejection is logged.

## Layout

```
caraway/            package
  sources/          NYC, Socrata bid datasets, SAM.gov, USAspending
  scanner.py        lock, persistent job queue, retries, ingest/dedupe/versioning
  eligibility.py    deterministic, fail-closed screening
  compliance.py     independent gate; binding actions hard-denied
  ranking.py        transparent scoring + outcome-tested learning
  executive.py      next actions, human tasks, subcontractor matching, RFQ drafts
  finance.py        bid math, typed money facts, cash-capacity check
  contractors.py    contractor research engine (official directories, evidence, CRM)
  mail/             templates, policy gate, Gmail/dry-run transports, OAuth, classifier, engine
  bids.py           bid packages: benchmark, pricing, cash flow, compliance checks
  brain.py          executive brain: funnel stages, next-best action, decisions, learning
  health.py         doctor, failed-job reporting, macOS notifications
  llm.py            Ollama provider (optional), schema-validated output
  dashboard.py      local web UI
config/             owner capability profile (all unverified by default)
deploy/launchd/     plist templates
scripts/            install / uninstall / scheduled-scan wrappers
tests/              53 rule, safety, parsing, pipeline and end-to-end tests
var/                database, logs (gitignored)
```

## Integrating the earlier contracting app

No earlier Caraway contracting app or database was found in the repositories available to
this build. If it exists on the Mac, the cleanest merge is at the database layer:
`opportunities`, `contractors`, `pursuits`, `quotes` and `bid_estimates` here are a superset of
the usual pipeline/CRM tables, so existing rows can be imported with a one-off script.
