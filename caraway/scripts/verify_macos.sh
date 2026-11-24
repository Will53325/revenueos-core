#!/bin/bash
# End-to-end check of a Mac deployment. Run after scripts/install_macos.sh.
# Verifies: Python, tests, both launchd agents, a scheduled scan actually running,
# dashboard health, restart recovery (kills the dashboard and checks launchd restarts it).
set -u
cd "$(dirname "$0")/.."
UIDN="$(id -u)"; pass=0; fail=0
ok(){ echo "PASS  $*"; pass=$((pass+1)); }; bad(){ echo "FAIL  $*"; fail=$((fail+1)); }
python3 -c 'import sys; assert sys.version_info >= (3,9)' && ok "python3 $(python3 -V 2>&1)" || bad "python3 3.9+ missing"
python3 -m unittest discover -s tests >/tmp/caraway-tests.log 2>&1 && ok "unit tests" || bad "unit tests (see /tmp/caraway-tests.log)"
for l in com.caraway.scanner com.caraway.dashboard; do
  launchctl print "gui/$UIDN/$l" >/dev/null 2>&1 && ok "$l loaded" || bad "$l not loaded (run scripts/install_macos.sh)"
done
BEFORE="$(python3 -c 'from caraway import db;c=db.connect();db.init(c);r=db.one(c,"select max(id) m from scan_runs");print(r["m"] or 0)')"
launchctl kickstart "gui/$UIDN/com.caraway.scanner" && ok "scanner kickstarted via launchd" || bad "kickstart scanner"
for i in $(seq 1 60); do
  AFTER="$(python3 -c 'from caraway import db;c=db.connect();r=db.one(c,"select max(id) m, status from scan_runs where id=(select max(id) from scan_runs)");print(r["m"] or 0, r["status"])')"
  set -- $AFTER; [ "$1" -gt "$BEFORE" ] && [ "$2" != "running" ] && break; sleep 5
done
[ "$1" -gt "$BEFORE" ] && ok "launchd scan completed (run $1: $2)" || bad "no scan completed within 5 minutes (see var/logs/launchd-scan.err)"
curl -fsS http://127.0.0.1:8787/health >/dev/null && ok "dashboard responding" || bad "dashboard not responding"
pkill -f "caraway serve" ; sleep 12
curl -fsS http://127.0.0.1:8787/health >/dev/null && ok "dashboard restarted by launchd after kill" || bad "dashboard did not restart (KeepAlive)"
python3 -m caraway doctor > /tmp/caraway-doctor.json && grep -q '"ok": true' /tmp/caraway-doctor.json && ok "doctor: healthy" || bad "doctor reports problems: $(cat /tmp/caraway-doctor.json)"
echo "---- $pass passed, $fail failed"; [ "$fail" -eq 0 ]
