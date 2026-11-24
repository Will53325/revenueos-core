#!/bin/bash
# Move Caraway (with its full git history) into its own PRIVATE repository.
#
#   scripts/migrate_to_private_repo.sh <new-private-repo-url> [source-branch]
#
# 1. Create an EMPTY private repo first (GitHub: New repository > Private, no README),
#    e.g. https://github.com/<you>/caraway.git
# 2. Run this script from anywhere inside the revenueos-core checkout.
#
# What it does: `git subtree split` extracts only the caraway/ folder's history into a
# temporary branch and pushes it as `main` of the new repo. revenueos-core itself is not
# modified, and nothing else in it (portfolio or other projects) is copied.
# What it does NOT do: delete anything from the public repo. After verifying the new repo,
# remove the public copy with the commands printed at the end.
set -euo pipefail
URL="${1:?usage: migrate_to_private_repo.sh <new-private-repo-url> [source-branch]}"
SRC="${2:-claude/upbeat-albattani-7bokbw}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
git fetch origin "$SRC" >/dev/null 2>&1 || true
REF="$(git rev-parse --verify "origin/$SRC" 2>/dev/null || git rev-parse --verify "$SRC")"
TMP="caraway-export-$$"
git branch -D "$TMP" >/dev/null 2>&1 || true
git subtree split --prefix=caraway "$REF" -b "$TMP" >/dev/null
echo "Extracted $(git rev-list --count "$TMP") commits of caraway/ history."
git push "$URL" "$TMP:refs/heads/main"
git branch -D "$TMP" >/dev/null
cat <<MSG

Done. Caraway now lives at $URL (branch main). Next:
  git clone $URL ~/Developer/caraway && cd ~/Developer/caraway && ./scripts/install_macos.sh
Copy your local data if you already ran it elsewhere:  cp <old>/caraway/var/caraway.db ~/Developer/caraway/var/

After confirming the private copy works, remove the public copy (irreversible for that branch):
  git -C "$ROOT" push origin --delete $SRC
Note: anything already public may have been cached or forked; no secrets were ever committed.
MSG
