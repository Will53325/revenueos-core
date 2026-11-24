# Caraway Morning Report — 2026-11-24

## Bottom line

The contract scanner works and has done real end-to-end scans against live official data.
It is **not yet running on your Mac.** It was built and tested in a Claude Code cloud
container, because no Mac was connected to this session. To start it, run the three commands
under "Restart / start" below. After that it scans every 4 hours with no Claude session involved.

## What was built

All in `caraway/` on branch `claude/upbeat-albattani-7bokbw`. Pure Python and SQLite with no
dependencies and no paid services.

- **Live discovery from 4 official open-bid feeds (no keys needed):** NYC City Record, City of
  Los Angeles RAMP, Montgomery County MD, and State of Delaware.
- **Federal intelligence from USAspending.gov:** award history, contracts ending soon
  (recompete forecasts), and a contractor database of firms with real federal awards.
- **SAM.gov adapter:** uses the official API. It is built and tested against the documented
  response shape, but switched off until you add a free API key.
- **Eligibility screen (fail-closed):** sorts each notice into candidate, not eligible, expired,
  or not an opportunity. Every rejection has a reason. Every candidate lists the blockers you
  must verify. Nothing is ever marked "GO" automatically.
- **Requirement extraction with evidence excerpts:** site visits, bonding, insurance,
  subcontracting limits, set-asides, and published values. A value is shown only if the notice
  states it.
- **Dedup and amendment tracking:** by solicitation number and content hash, with version
  history. Extensions, corrections, and cancellations are recognized. Fake placeholder
  deadlines (e.g. 9999-09-09) are handled.
- **Ranking:** transparent weights. Every score is logged as a prediction so it can be checked
  against real outcomes. The model is only replaced if a candidate beats it on held-out results.
- **Independent compliance gate:**
  - Bids, signatures, attestations, spending, purchases, and account registration are
    hard-denied in code.
  - Email sending is denied even if someone enables it.
  - Network access goes through a host allowlist.
  - Pause control and a STOP file halt automation.
- **Executive layer:**
  - Creates the owner-task list.
  - Matches leads to contractors by category and state.
  - Writes RFQ email drafts (never sent).
- **Finance data model:** typed money facts (bid value, awarded value, receivable, collected
  cash, and so on), each marked actual, estimate, or projection. It includes bid arithmetic and
  a cash-capacity check that answers "unknown" until verified bank data exists.
- **Dashboard at http://127.0.0.1:8787**, mobile-friendly, with these tabs:
  - Status, Leads, All notices with rejection reasons, Recompetes
  - Contractors, Pipeline, Your tasks, Drafts
  - Finance (empty until verified data exists), Logs, errors, and audit trail
- **Scheduler:** launchd agents for scans every 4 hours plus an always-on dashboard, with
  install and uninstall scripts.
- **Optional local AI:** an Ollama provider (recommended model `qwen2.5:3b`, about 1.9 GB). It
  only writes summaries. Its output must match a strict JSON schema, so instructions planted in
  a document can't steer it.

## What was tested (with evidence)

| Test | Result |
|---|---|
| Live scans against official sources | 8+ runs, latest status `ok`, ~30–60 s each |
| Notices stored | 521 (NYC 268, LA 189, Delaware 42, Montgomery 22) |
| Screening outcome | 7 candidates, 317 rejected with reasons, 197 expired and quarantined |
| Federal intelligence | 3,339 award records, 849 recompete forecasts (598 under $1M), 1,980 contractors |
| Unit/integration tests | 35 pass (`python3 -m unittest discover -s tests`) |
| Overlapping scans | Second run skipped by lock ✔ |
| STOP file / pause | Scan skipped ✔ |
| One source failing | Retried 3× with backoff, marked `error`; other sources unaffected; run marked `partial` ✔ |
| Dashboard | All 10 tabs return 200; no horizontal scroll at 375 px or 1280 px; forged POST rejected (403) ✔ |
| launchd plists | Valid plist syntax ✔. **Not loaded on a real Mac yet** (no Mac available) |
| Ollama | Code path tested against a fake local server ✔. **Not tested with a real model** |

Bugs found and fixed through live testing:
- A stray "landscape" in a healthcare notice made it a false lead.
- NYC's "Mandatory: noDate" formatting made an optional pre-bid look mandatory.
- "Services (other than human services)" was wrongly excluded as human services.
- Software "maintenance" contracts were treated as facility work.
- Retries were not awaited, so a failed run could report "ok".

## Is the scanner running?

- **On your Mac:** No, not until you run `./scripts/install_macos.sh`.
- **In the cloud container:** it ran repeatedly, but that container is temporary and is not a
  production host.

## Real opportunities found

**7 open notices passed the screen.** That is below the target of 10, and I didn't loosen the
rules to hit the number. The main reason is that the federal feed (SAM.gov), where most
janitorial and grounds work is posted, needs an API key that wasn't available. The 598 small
federal recompetes show how much volume sits there.

None of these is bid-ready. Every one has unverified blockers: vendor registration on that
government's portal, insurance, and subcontractor coverage in a state where Caraway has none
yet. I have **not** read the full solicitation documents for any of them.

### Five strongest

1. **LA County: RFSQ for Landscape and Grounds Maintenance Services (BRC0000478)**. Continuous
   enrollment, open until 2027-05-14.
   https://www.rampla.org/s/opportunity-details?id=006Ql00000NI1mnIAD
   *Why it fits:* this is a qualification list, not a one-shot bid. Firms qualify first and then
   compete for individual work orders. That is a realistic way in for a new firm, with no
   deadline crunch and a core Caraway category.
   *Blockers:* RAMP/LA County vendor registration; likely experience and insurance thresholds
   in the RFSQ; needs a California grounds subcontractor.

2. **LA County: RFSQ for Facility Ancillary Services Master Agreement (FASMA, GCS-10639-S)**.
   Continuous, open until 2028-09-17.
   https://www.rampla.org/s/opportunity-details?id=006Ql00000lfnZZIAY
   *Why it fits:* same master-agreement model, covering facility support services that typically
   include janitorial-type work. It stays open for almost 4 years.
   *Blockers:* same as #1. Exact service categories must be confirmed in the RFSQ.

3. **Montgomery County, MD DEP: Vegetated Roof (Green) Maintenance (RFP 1202826)**. Due
   2026-12-21, 27 days out.
   Record: https://data.montgomerycountymd.gov/resource/eeq6-nnwe.json?number=1202826 ·
   Portal: https://www.montgomerycountymd.gov/PRO/ (I couldn't verify this link from the cloud
   environment.)
   *Why it fits:* recurring maintenance (plant care, weeding, inspection) that a landscaping
   subcontractor can perform. It is not set aside, and there is enough time to prepare.
   *Blockers:* county vendor registration; the RFP is best-value, so past performance will
   matter; needs a Maryland green-roof-capable subcontractor; roof access may raise
   safety/insurance requirements.

4. **Delaware: Pest Control Services**. Due 2026-12-08, 14 days out.
   https://mmp.delaware.gov/Bids/Details/9359
   *Why it fits:* routine recurring service, normally performed by a licensed local operator.
   *Blockers:* Delaware pesticide applicator licensing almost certainly applies to whoever
   performs the work. The notice summary has no detail, so the documents must be checked. Tight
   timeline.

5. **NYC DCAS: Citywide Moving Services (PIN 85727B0004)**. Due 2026-12-03, 2:00 PM ET, 9 days out.
   https://data.cityofnewyork.us/resource/dg92-zbpx.json?request_id=20261021030 (official
   NYC Open Data record; documents are in NYC PASSPort).
   *Why it fits:* competitive sealed bid, so the lowest responsive price wins. That suits a lean
   prime with a moving subcontractor. The pre-bid conference was optional (it was held 11/5).
   *Blockers:* PASSPort account; 9 days is very tight; citywide scale may be too large to start
   with.

Also screened in: **LA County On-Call Catch Basin Cleanout (BRC0000655, due Dec 7)** and **LA
County Street Sweeping RFSQ**. I ranked them lower because they need vacuum or sweeper trucks
and storm-water compliance. Caraway would depend entirely on an equipped subcontractor.

## Known eligibility blockers (company-wide)

1. SAM.gov registration not verified. Federal awards require it.
2. No business bank account yet. Government payment requires one.
3. Insurance (general liability and workers' comp) not in place or verified.
4. SDVOSB/VOSB status not certified. Don't claim it until SBA VetCert approves.
5. No past performance on record. Best-value RFPs will weight this heavily.
6. Home base and travel radius unknown, so geography can't be scored.
7. No subcontractor relationships yet. The 1,980 contractors on file are leads, not partners,
   and their contacts are unresearched.

## Unfinished

- SAM.gov live data (needs the key).
- Downloading and parsing full solicitation documents (portals need vendor logins).
- Contractor contact research (emails and phones) and license verification.
- Email sending (deliberately disabled).
- Banking and accounting connections (data model only).
- Real-model Ollama testing.
- An owner-facing UI for recording outcomes and quotes (CLI only for outcomes; quotes need a form).
- More state and county sources beyond the 4 open-data feeds.

## Logins and credentials needed

| What | Why | Where |
|---|---|---|
| SAM.gov public API key (free) | Unlocks federal opportunities | sam.gov → Account Details |
| SAM.gov entity registration | Required to bid federally | sam.gov (free; avoid paid "registration services") |
| LA County / RAMP vendor account | Documents and responses for leads 1, 2 | rampla.org |
| Montgomery County vendor registration | Lead 3 | Montgomery County Office of Procurement |
| Delaware vendor registration | Lead 4 | mmp.delaware.gov |
| NYC PASSPort account | Lead 5 | NYC MOCS PASSPort |
| Dedicated business email | Before any outreach can be sent | Your choice; then enable in settings |

## Restart / start

```bash
cd revenueos-core/caraway
./scripts/install_macos.sh
```

Manual scan any time:

```bash
python3 -m caraway scan
```

Restart just the dashboard:

```bash
launchctl kickstart -k gui/$(id -u)/com.caraway.dashboard
```

**Dashboard:** http://127.0.0.1:8787

## Next three highest-value tasks

1. **Add the SAM.gov key and turn it on.** This is the largest pool of janitorial and grounds
   work, and it lets the scanner reach the 10+ target honestly.
2. **Pick a home base and first one or two target states, then verify subcontractors there.**
   Geography is currently the most common blocker. The recompete list shows which local federal
   contracts end in the next 9 months.
3. **Parse full solicitation documents for candidates.** Start with public SAM.gov attachments,
   then the RAMP and Delaware pages. Extract real insurance, bonding, licensing, and
   subcontracting terms so blockers become facts.

## Notes

- `revenueos-core` is a **public** GitHub repository, so this code is publicly visible. No
  secrets or personal data are in it, and the database (`var/`) is gitignored. Consider moving
  Caraway to a private repo.
- I didn't use SAM.gov's undocumented website search endpoint as a workaround for the missing
  key. It isn't an official API, and the safety system flagged it.
- The cloud environment "Caraway" was created for this session, with network access limited to
  the official data hosts above.
