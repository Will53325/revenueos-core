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
python3 -m caraway scan              # manual Run Scan (also a button on the dashboard)
python3 -m caraway status            # last successful scan, sources, blockers
python3 -m caraway leads -n 20       # ranked candidate leads
python3 -m caraway report            # markdown summary
python3 -m caraway pause             # stop all automated work (resume with: resume)
python3 -m caraway outcome 458 not_worth_pursuing --evidence "needs MD license"   # teach the ranker
python3 -m caraway evaluate          # measure ranking vs verified outcomes
python3 -m unittest discover -s tests -v
```

**Emergency stop:** `touch var/STOP` halts every scheduled run until the file is deleted.

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
  comms.py          drafts only
  llm.py            Ollama provider (optional), schema-validated output
  dashboard.py      local web UI
config/             owner capability profile (all unverified by default)
deploy/launchd/     plist templates
scripts/            install / uninstall / scheduled-scan wrappers
tests/              35 rule, safety, parsing and pipeline tests
var/                database, logs (gitignored)
```

## Integrating the earlier contracting app

No earlier Caraway contracting app or database was found in the repositories available to
this build. If it exists on the Mac, the cleanest merge is at the database layer:
`opportunities`, `contractors`, `pursuits`, `quotes` and `bid_estimates` here are a superset of
the usual pipeline/CRM tables, so existing rows can be imported with a one-off script.
