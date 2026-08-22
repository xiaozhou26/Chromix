#!/usr/bin/env bash
# Monthly Chromium rebase for Chromix.
#   1. bump CHROMIUM_VERSION to the new stable tag
#   2. sync the tree, 3-way apply the patch series, report any conflicts
#
# Usage:  build/rebase-monthly.sh <new-version> [workdir]
#   e.g.  build/rebase-monthly.sh 152.0.7000.0
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NEW="${1:?usage: rebase-monthly.sh <new-chromium-version>}"
WORK="${2:-$REPO/.chromix-build}"

echo "==> Rebasing Chromix: $(cat "$REPO/CHROMIUM_VERSION") -> $NEW"
echo "$NEW" > "$REPO/CHROMIUM_VERSION"

# Build (build.sh applies patches via apply-patches.sh, which reports conflicts).
if ! CHROMIUM_VERSION="$NEW" "$REPO/build/build.sh" "$WORK"; then
  echo "!! Rebase needs manual patch re-anchoring (see [FAIL] lines above)."
  echo "   Re-anchor the flagged patches, then re-run this script."
  exit 2
fi

echo "==> Rebased to $NEW cleanly."
