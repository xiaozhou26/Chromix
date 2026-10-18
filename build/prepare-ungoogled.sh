#!/usr/bin/env bash
# Prepare Chromium -> ungoogled core -> platform overlay -> prune -> Chromix.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${1:?usage: prepare-ungoogled.sh WORKDIR linux|macos [x64|arm64]}"
PLATFORM="${2:?platform is required}"
ARCH="${3:-x64}"
case "$ARCH" in x64|arm64) ;; *) echo "unsupported architecture: $ARCH" >&2; exit 2 ;; esac
case "$PLATFORM" in
  linux) PLATFORM_NAME=ungoogled-chromium-portablelinux; PLATFORM_KEY=UngoogledLinux ;;
  macos) PLATFORM_NAME=ungoogled-chromium-macos; PLATFORM_KEY=UngoogledMacOS ;;
  *) echo "unsupported platform: $PLATFORM" >&2; exit 2 ;;
esac
mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
CORE_REPO="$WORK/tooling/ungoogled-chromium"
PLATFORM_REPO="$WORK/tooling/$PLATFORM_NAME"
PLATFORM_PATCHES="$PLATFORM_REPO/patches"
CACHE="$WORK/download_cache"
SRC="$WORK/src"
READY="$SRC/.chromix-source-ready"
revision() {
  python3 "$REPO/tools/platform_pins.py" --repo "$REPO" --platform "$PLATFORM" --field "$1"
}
CHROMIUM_VERSION="$(revision ChromiumVersion)"
CORE_COMMIT="$(revision UngoogledCommit)"
PLATFORM_COMMIT="$(revision "${PLATFORM_KEY}Commit")"
PLATFORM_VERSION="$(revision "${PLATFORM_KEY}Version")"
PATCH_HASH="$(python3 "$REPO/tools/patch_selection.py" --repo "$REPO" --platform "$PLATFORM" --key posix)"
KEY="$PLATFORM|$ARCH|$CHROMIUM_VERSION|$CORE_COMMIT|$PLATFORM_COMMIT|$PATCH_HASH"
RESTORED=0
if [ -f "$SRC/.chromix-upstream-restored.json" ]; then
  python3 "$REPO/tools/restore_upstream_cache.py" --phase verify \
    --platform "$PLATFORM" --arch "$ARCH" --workdir "$WORK"
  RESTORED=1
fi
for marker in .chromix-domain-substitution-in-progress .chromix-restored-patches-in-progress; do
  if [ -e "$SRC/$marker" ]; then
    echo "source preparation was interrupted: $marker; use a clean work directory" >&2
    exit 1
  fi
done
if [ -f "$READY" ] && [ "$(cat "$READY")" = "$KEY" ]; then
  if [ "$RESTORED" -eq 1 ]; then
    PATCH_BIN="${PATCH_BIN:-$(command -v gpatch || command -v patch)}"
    python3 "$REPO/tools/apply_restored_patches.py" --src "$SRC" --repo "$REPO" \
      --core "$CORE_REPO" --platform-tooling "$PLATFORM_REPO" \
      --platform "$PLATFORM" --patch-bin "$PATCH_BIN" --check
  fi
  if [ "$RESTORED" -eq 1 ] || [ "$CHROMIUM_VERSION" = "154.0.8037.97" ] || [ "$CHROMIUM_VERSION" = "155.0.8059.39" ] || [ "$CHROMIUM_VERSION" = "155.0.8059.79" ]; then
    SOURCE_CHECK="$(mktemp "${TMPDIR:-/tmp}/chromix-warm-patches-XXXXXX.json")"
    rm "$SOURCE_CHECK"
    python3 "$REPO/tools/verify_patch_stack.py" --src "$SRC" --repo "$REPO" \
      --core "$CORE_REPO" --platform-tooling "$PLATFORM_REPO" --platform "$PLATFORM" --output "$SOURCE_CHECK"
    rm "$SOURCE_CHECK"
  fi
  echo "==> pinned ungoogled source already prepared: $KEY"
  exit 0
fi
if [ -e "$SRC" ] && [ "$RESTORED" -ne 1 ]; then
  echo "source is incomplete or has different pins; use a clean work directory: $WORK" >&2
  exit 1
fi
if [ -e "$READY" ]; then
  echo "prepared patch set changed; use a clean work directory: $WORK" >&2
  exit 1
fi
mkdir -p "$WORK/tooling" "$CACHE"
checkout_pinned() {
  local url="$1" path="$2" commit="$3"
  if [ ! -d "$path/.git" ]; then
    git init "$path"
    git -C "$path" remote add origin "$url"
  fi
  git -C "$path" fetch --depth 1 origin "$commit"
  git -C "$path" checkout --detach --force "$commit"
  test "$(git -C "$path" rev-parse HEAD)" = "$commit"
}
checkout_pinned https://github.com/ungoogled-software/ungoogled-chromium.git "$CORE_REPO" "$CORE_COMMIT"
checkout_pinned "https://github.com/ungoogled-software/$PLATFORM_NAME.git" "$PLATFORM_REPO" "$PLATFORM_COMMIT"
test "$(cat "$CORE_REPO/chromium_version.txt")" = "$CHROMIUM_VERSION"
CORE_VERSION="$CHROMIUM_VERSION-$(cat "$CORE_REPO/revision.txt")"
test "$CORE_VERSION" = "$(revision UngoogledVersion)"
if [ "$PLATFORM" = macos ]; then
  test "$CORE_VERSION.$(cat "$PLATFORM_REPO/revision.txt")" = "$PLATFORM_VERSION"
else
  case "$PLATFORM_VERSION" in
    "$CORE_VERSION"|"$CORE_VERSION".*) ;;
    *) echo "portablelinux version does not match the pinned core: $PLATFORM_VERSION" >&2; exit 1 ;;
  esac
fi
if [ "$RESTORED" -eq 1 ]; then
  PATCH_BIN="${PATCH_BIN:-$(command -v gpatch || command -v patch)}"
  python3 "$REPO/tools/prepare_restored_build.py" --phase inspect --platform "$PLATFORM" --arch "$ARCH" \
    --workdir "$WORK"
  python3 "$REPO/tools/apply_restored_patches.py" --src "$SRC" --repo "$REPO" \
    --core "$CORE_REPO" --platform-tooling "$PLATFORM_REPO" \
    --platform "$PLATFORM" --patch-bin "$PATCH_BIN"
  printf '%s\n' "$CORE_COMMIT" > "$SRC/.chromix-ungoogled-core"
  printf '%s\n' "$PLATFORM_COMMIT" > "$SRC/.chromix-ungoogled-platform"
  printf '%s\n' "$CHROMIUM_VERSION" > "$SRC/.chromix-chromium-version"
  printf '%s\n' "$CORE_COMMIT" > "$SRC/.chromix-domain-substituted"
  test "$(python3 "$REPO/tools/patch_selection.py" --repo "$REPO" --platform "$PLATFORM" --key posix)" = "$PATCH_HASH"
  printf '%s\n' "$KEY" > "$READY"
  echo "==> restored upstream source with Chromix patches: $KEY"
  exit 0
fi
if [ "$PLATFORM" = linux ]; then
  # Recovery payloads are bound to exact pins and independently checked bytes.
  python3 - "$PLATFORM_PATCHES/ungoogled-chromium/portablelinux/fix-compiling-on-arm64.patch" \
    "$CHROMIUM_VERSION" "$CORE_COMMIT" "$PLATFORM_COMMIT" "$PLATFORM_VERSION" \
    "$REPO/build/linux/recovery/153.0.8010.36-1-arm64.patch" <<'PY'
import hashlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
pin = tuple(sys.argv[2:5])
legacy_pin = ('152.0.7977.82', 'e71b91c6e336d0f25cfc6b9ef09298a9d2506e24',
              '02c59ed68d1963a647bb478064823d114e466ffb')
recovery_pin = ('153.0.8010.36', 'dd8fb9b5c837982faf41ba58cd30a5664e77c329',
                'a5ffa5e4a9fb722b97a5cf7966e29450a150c3dd')
if pin != legacy_pin and (pin != recovery_pin or sys.argv[5] != '153.0.8010.36-1'):
    raise SystemExit(
        f'portablelinux ARM64 patch repair is unverified for {sys.argv[2]} '
        f'({sys.argv[4]}, {sys.argv[5]}); refusing cold preparation: {path}')
try:
    content = path.read_bytes()
except FileNotFoundError:
    raise SystemExit(f'missing portablelinux ARM64 patch: {path}')
if pin == legacy_pin:
    bad = (b'--- a/tools/rust/build_rust.py\n'
           b'+++ b/tools/rust/build_rust.py\n'
           b'@@ -55,7 +55,7 @@')
    good = bad.replace(b'-55,7 +55,7', b'-55,8 +55,8')
    corrected = content.replace(bad, good, 1)
    expected = 'bf1e5d6978c5b3b5121336b673ea5138941e9d1e28d00cf47c232ec08521f0e1'
else:
    original = '4a848abdacb9b677b3d309a8a8b6597250aeaf4c1b4a009f16faf490e55103ad'
    expected = 'b27db03071f007b40af4861b73a40af7c73c5d8cb2be3f6aada471b1fa2cbb1a'
    if hashlib.sha256(content).hexdigest() not in (original, expected):
        raise SystemExit(f'unexpected portablelinux ARM64 patch; review pinned workaround: {path}')
    recovery = Path(sys.argv[6])
    try:
        corrected = recovery.read_bytes()
    except FileNotFoundError:
        raise SystemExit(f'missing portablelinux ARM64 recovery payload: {recovery}')
if hashlib.sha256(corrected).hexdigest() != expected:
    raise SystemExit(f'unexpected portablelinux ARM64 patch; review pinned workaround: {path}')
if corrected != content:
    path.write_bytes(corrected)
PY
fi
PATCH_BIN="${PATCH_BIN:-$(command -v gpatch || command -v patch)}"
export PATCH_BIN
mkdir -p "$SRC"
python3 "$CORE_REPO/utils/downloads.py" retrieve -i "$CORE_REPO/downloads.ini" -c "$CACHE"
python3 "$CORE_REPO/utils/downloads.py" unpack -i "$CORE_REPO/downloads.ini" -c "$CACHE" "$SRC"
python3 "$CORE_REPO/utils/patches.py" apply "$SRC" "$CORE_REPO/patches"
python3 "$CORE_REPO/utils/patches.py" apply "$SRC" "$PLATFORM_PATCHES"
python3 "$CORE_REPO/utils/prune_binaries.py" "$SRC" "$CORE_REPO/pruning.list"
# The lite archive omits a Torque source used by Chromium's build graph.
cp -R "$REPO/build/windows/lite-tarball-files/." "$SRC/"
CHROMIX_PATCH_PLATFORM="$PLATFORM" "$REPO/build/apply-patches.sh" "$SRC"

if [ "$PLATFORM" = macos ]; then
  rm -rf "$PLATFORM_REPO/ungoogled-chromium"
  ln -s "$CORE_REPO" "$PLATFORM_REPO/ungoogled-chromium"
  mkdir -p "$PLATFORM_REPO/build"
  rm -rf "$PLATFORM_REPO/build/src" "$PLATFORM_REPO/build/download_cache"
  ln -s "$SRC" "$PLATFORM_REPO/build/src"
  ln -s "$CACHE" "$PLATFORM_REPO/build/download_cache"
  RESOURCE_ARCH="$ARCH"
  [ "$ARCH" = x64 ] && RESOURCE_ARCH=x86_64
  python3 "$PLATFORM_REPO/retrieve_and_unpack_resource.py" -p "$RESOURCE_ARCH"
fi
printf '%s\n' "$CORE_COMMIT" > "$SRC/.chromix-ungoogled-core"
printf '%s\n' "$PLATFORM_COMMIT" > "$SRC/.chromix-ungoogled-platform"
printf '%s\n' "$CHROMIUM_VERSION" > "$SRC/.chromix-chromium-version"
test "$(python3 "$REPO/tools/patch_selection.py" --repo "$REPO" --platform "$PLATFORM" --key posix)" = "$PATCH_HASH"
printf '%s\n' "$KEY" > "$READY"
echo "==> prepared ungoogled source: $KEY"
