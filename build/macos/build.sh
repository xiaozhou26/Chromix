#!/usr/bin/env bash
# Native macOS build using pinned ungoogled-chromium source layers.
set -euo pipefail
unset -- "${!DYLD_@}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$REPO/build/posix/upstream-cache.sh"
BUILD_PROFILE="${CHROMIX_BUILD_PROFILE:-release}"
case "$BUILD_PROFILE" in
  fast|release) ;;
  *) echo "CHROMIX_BUILD_PROFILE must be fast or release" >&2; exit 2 ;;
esac
WORK="${1:-$REPO/.chromix-build-mac}"
HOST_ARCH="$(uname -m)"
[ "$HOST_ARCH" = x86_64 ] && HOST_ARCH=x64
ARCH="${2:-$HOST_ARCH}"
case "$ARCH" in arm64|x64) ;; *) echo "unsupported macOS architecture: $ARCH" >&2; exit 2 ;; esac
if [ "$(uname -s)" != Darwin ] || [ "$HOST_ARCH" != "$ARCH" ]; then
  echo "a native macOS $ARCH host is required" >&2; exit 2
fi
source "$REPO/build/macos/select-xcode.sh"
select_macos_xcode
mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
SRC="$WORK/src"
OUT="$SRC/out/Chromix"
if [ -f "$SRC/.chromix-upstream-restored.json" ]; then
  MACOS_RUNTIME_ENV="$(python3 "$REPO/tools/macos_runtime.py" --src "$SRC" --arch "$ARCH")"
  eval "$MACOS_RUNTIME_ENV"
fi
"$REPO/build/prepare-ungoogled.sh" "$WORK" macos "$ARCH"
if [ -f "$SRC/.chromix-upstream-restored.json" ]; then
  OUT="$SRC/out/Default"
fi
cd "$SRC"
chromix_select_restored_ninja macos
if [ -f "$SRC/.chromix-upstream-restored.json" ]; then
  bash "$REPO/build/posix/prepare-restored-tools.sh" "$WORK" macos "$ARCH"
fi
if [ ! -f "$SRC/.chromix-toolchain-ready" ]; then
  if [ -f "$SRC/.chromix-domain-substituted" ]; then
    echo "toolchain is incomplete in a domain-substituted source tree; use a clean work directory" >&2; exit 1
  fi
  chromix_import_upstream_cache toolchain macos
  if [ ! -x third_party/rust-toolchain/bin/bindgen ]; then
    python3 tools/rust/build_bindgen.py --skip-test
  fi
  touch "$SRC/.chromix-toolchain-ready"
fi
if [ -f "$SRC/.chromix-domain-substitution-in-progress" ]; then
  echo "domain substitution was interrupted; use a clean work directory" >&2; exit 1
fi
if [ "${CHROMIX_APPLY_DOMAIN_SUBSTITUTION:-1}" = 1 ] && [ ! -f "$SRC/.chromix-domain-substituted" ]; then
  touch "$SRC/.chromix-domain-substitution-in-progress"
  python3 "$WORK/tooling/ungoogled-chromium/utils/domain_substitution.py" apply \
    -r "$WORK/tooling/ungoogled-chromium/domain_regex.list" \
    -f "$WORK/tooling/ungoogled-chromium/domain_substitution.list" "$SRC"
  mv "$SRC/.chromix-domain-substitution-in-progress" "$SRC/.chromix-domain-substituted"
fi
# Verify real source content, not only intended-input stamps, before compiling.
mkdir -p "$WORK/fingerprint-diagnostics"
SOURCE_REPORT="$WORK/fingerprint-diagnostics/source-$(date +%s)-$$.json"
python3 "$REPO/tools/verify_patch_stack.py" --src "$SRC" --repo "$REPO" \
  --core "$WORK/tooling/ungoogled-chromium" \
  --platform-tooling "$WORK/tooling/ungoogled-chromium-macos" --platform macos --output "$SOURCE_REPORT"
cp "$SOURCE_REPORT" "$WORK/fingerprint-diagnostics/source-final.json"
mkdir -p "$OUT"
printf 'target_cpu = "%s"\nv8_target_cpu = "%s"\n' "$ARCH" "$ARCH" > "$WORK/target.gn"
GN_INPUTS=("$WORK/tooling/ungoogled-chromium/flags.gn"
  "$WORK/tooling/ungoogled-chromium-macos/flags.macos.gn"
  "$REPO/build/args.macos.gn" "$WORK/target.gn")
if [ -f "$SRC/.chromix-upstream-restored.json" ]; then
  GN_INPUTS=("$OUT/args.gn" "${GN_INPUTS[@]}")
fi
printf '==> Build profile: %s\n' "$BUILD_PROFILE"
python3 "$REPO/tools/merge_gn_args.py" --build-profile "$BUILD_PROFILE" "$OUT/args.gn" "${GN_INPUTS[@]}"
python3 "$REPO/tools/bootstrap_gn.py" --src "$SRC" --out "$OUT"
"$OUT/gn" gen "$OUT" --fail-on-unused-args
chromix_report_upstream_plan chrome
chromix_build_restored_target macos "${CHROMIX_JOBS:-$(sysctl -n hw.ncpu)}" chrome
printf '==> Done: %s\n' "$OUT/Chromium.app"
