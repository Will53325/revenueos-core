# Caraway Status — 2026-11-24 (v0.2)

Supersedes the first-night report. Setup and commands are in README.md.

## Built in v0.2
- **Contractor research engine** (`contractors.py`). Pulls from official directories only, and
  every fact carries its source:

  | Source | Contacts available |
  |---|---|
  | NYC SBS certified businesses | phone, certifications, past jobs |
  | Delaware Office of Supplier Diversity | email, phone |
  | NY DEC registered pesticide businesses | active license + expiry |
  | LA business tax registrations | address only |
  | USAspending award recipients | none |
  | Montgomery County contract holders | none |

- **More discovery:**
  - Baton Rouge bid openings (active bids).
  - Cook County, San Diego County, NYC M/WBE upcoming procurements, and Montgomery County
    contracts expiring within 9 months. These are stored as **forecasts**, never as bids.
- **Email engine** (`mail/`):
  - Caraway-branded RFQ, follow-up and negotiation templates.
  - A standing communications policy and a send gate (owner switch, policy, transport,
    approval or auto-send kind, caps, quiet hours, opt-outs).
  - A dry-run transport (writes `.eml` files) and a Gmail API transport with OAuth; the token
    is stored in the macOS Keychain.
  - Reply classification, quote extraction, conversation threads, and permanent opt-outs.
- **Bid engine** (`bids.py`):
  - A benchmark from comparable federal awards and a target subcontract cost.
  - Pricing from real quotes.
  - A day-level working-capital model.
  - Pass/fail/unknown compliance checks with evidence, including FAR 52.219-14, set-asides,
    licenses, bonding and registrations.
  - An evaluation against the standing-authority policy.
- **Executive brain** (`brain.py`):
  - Funnel stages derived from evidence, and next actions ranked by expected value.
  - Safe actions executed automatically; specific owner tasks for the rest.
  - Every decision logged and scored after 7 days. Measured success rates replace the priors
    after 10 scored decisions per action type.
- **Operations:**
  - `caraway doctor` and a health check after every scan, covering stale scans, failed jobs,
    unloaded agents, disk space and database integrity. Problems raise a macOS notification
    and a P1 task.
  - `scripts/verify_macos.sh` for end-to-end deployment verification.
  - `scripts/migrate_to_private_repo.sh` for the private-repo move (dry-run tested).

## Live results (cloud test environment)
- Full cycle: ok, 85 s wall time, about 2 s CPU, 34 MB peak memory; the database is 11 MB.
- Notices: 546 stored from 5 open-bid sources, of which **7 are candidates**. The rest:
  336 rejected with reasons and 203 expired. SAM.gov is still blocked until an API key is added.
- 42 forecasts and 4,333 contractors, backed by 4,549 evidence records. Of the contractors,
  64 have an official email, 580 a phone number, and 399 an active license on record.
- Funnel:

  | Stage | Count |
  |---|---|
  | Discovered | 546 |
  | Qualified | 7 |
  | Contractors identified | 7 |
  | Quote requested | 0 |
  | Quote received | 0 |
  | Compliant bid | 0 |
  | Awarded | 0 |
  | Collected | $0 |

- 2 RFQs were drafted for Delaware Pest Control (Loyalty Termite & Pest Control, Grinnage
  Exterminating). Both are **blocked**: the sender name, email and mailing address are not set.
- 53 automated tests pass, including an end-to-end run of the full loop.

## What stops revenue right now (owner actions)
1. Sending authorization: Gmail connection plus sender details plus `mail enable`. Or call the
   phone lists in the Tasks tab and record quotes with `caraway quote add`.
2. Vendor registrations and insurance. These fail on all 7 bid packages.
3. A SAM.gov API key, to reach the largest pool of janitorial and grounds work.
4. A home base, so geography can be scored.

## Not done
- Not deployed or verified on the Mac (no Mac was attached).
- Gmail has not been tested against a real account; the code path is built.
- Full solicitation documents are not ingested.
- Moving and hauling firms are not distinguished (call lists for moving jobs include waste
  haulers).
- No email addresses exist for CA, MD or NY firms in the official directories used so far.
