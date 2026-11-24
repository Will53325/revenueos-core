#!/bin/bash
# Removes the launchd agents. Keeps the database and logs.
for name in scanner dashboard; do
  launchctl bootout "gui/$(id -u)/com.caraway.$name" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/com.caraway.$name.plist"
done
echo "Caraway agents removed. Data kept in $(cd "$(dirname "$0")/.." && pwd)/var"
