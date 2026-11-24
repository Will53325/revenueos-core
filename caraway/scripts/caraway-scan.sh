#!/bin/bash
# Scheduled scan entry point (launchd or cron). Safe to run any time:
# overlapping runs are prevented by a lock, and a STOP file halts everything.
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p var/logs
if [ -f var/STOP ]; then echo "$(date) STOP file present; skipping scan"; exit 0; fi
exec /usr/bin/env python3 -m caraway scan --trigger scheduled
