#!/bin/bash
# Installs Caraway's two user-level launchd agents (no sudo, no system changes).
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$PROJECT/var/logs"
command -v python3 >/dev/null || { echo "python3 not found. Run: xcode-select --install"; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"'
for name in scanner dashboard; do
  label="com.caraway.$name"
  sed "s#__PROJECT__#$PROJECT#g" "$PROJECT/deploy/launchd/$label.plist.template" > "$AGENTS/$label.plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
  echo "loaded $label"
done
echo "Dashboard: http://127.0.0.1:8787   (first scan starts now; logs in $PROJECT/var/logs)"
