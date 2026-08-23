#!/usr/bin/env bash
# Build a specific Chromium version of Chromix into its OWN tree/out, so
# multiple versions (e.g. 149 stable + 151 latest) coexist without clobbering
# each other.
#
# Usage:  build/build-version.sh <chromium-version>
#   e.g.  build/build-version.sh 149.0.7827.200
#         build/build-version.sh 151.0.7922.173
#
# Each version gets its own workdir  .chromix-build-<major>/  (each needs
# ~100GB + hours). The patch series is 3-way applied; if a patch fails on an
# older/newer tree, re-anchor it (see patches/README.md) and re-run.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="${1:?usage: build-version.sh <chromium-version>}"
MAJOR="${VER%%.*}"
WORK="$REPO/.chromix-build-$MAJOR"

echo "==> Chromix $MAJOR | Chromium $VER | workdir $WORK"

# Build the versioned tree (build.sh reads CHROMIUM_VERSION from the env).
CHROMIUM_VERSION="$VER" "$REPO/build/build.sh" "$WORK"

echo "==> built: $WORK/chromium/src/out/Chromix/chrome"
