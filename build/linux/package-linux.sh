#!/usr/bin/env bash
# Package a native Linux Chromix build into the SDK-compatible portable bundle.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:?usage: package-linux.sh /path/to/out/Chromix [dest]}"
DEST="${2:-$REPO/dist}"
STAGE="$DEST/chromix"
FONTS_SRC="${CHROMIX_FONTS_DIR:-$REPO/assets/fonts}"

rm -rf "$STAGE"
mkdir -p "$STAGE/locales" "$STAGE/fonts" "$DEST"

copy_required() {
  local relative="$1"
  if [ ! -e "$OUT/$relative" ]; then
    echo "required runtime file is missing: $OUT/$relative" >&2
    exit 1
  fi
  mkdir -p "$STAGE/$(dirname "$relative")"
  cp -a "$OUT/$relative" "$STAGE/$relative"
}

copy_optional() {
  local relative="$1"
  if [ -e "$OUT/$relative" ]; then
    mkdir -p "$STAGE/$(dirname "$relative")"
    cp -a "$OUT/$relative" "$STAGE/$relative"
  fi
}

for name in chrome chrome_crashpad_handler icudtl.dat resources.pak; do
  copy_required "$name"
done

if [ -e "$OUT/chrome_sandbox" ]; then
  cp -a "$OUT/chrome_sandbox" "$STAGE/chrome-sandbox"
elif [ -e "$OUT/chrome-sandbox" ]; then
  cp -a "$OUT/chrome-sandbox" "$STAGE/chrome-sandbox"
else
  echo "required runtime file is missing: $OUT/chrome_sandbox" >&2
  exit 1
fi

for name in \
  chrome_100_percent.pak chrome_200_percent.pak \
  libEGL.so libGLESv2.so libvulkan.so.1 libvk_swiftshader.so \
  vk_swiftshader_icd.json v8_context_snapshot.bin snapshot_blob.bin \
  lib/libc++.so; do
  copy_optional "$name"
done

find "$OUT/locales" -maxdepth 1 -type f -name '*.pak' -exec cp -a {} "$STAGE/locales/" \;
if [ ! -e "$STAGE/v8_context_snapshot.bin" ] && [ ! -e "$STAGE/snapshot_blob.bin" ]; then
  echo "no V8 snapshot blob found in $OUT" >&2
  exit 1
fi

if ! compgen -G "$FONTS_SRC/*.ttf" >/dev/null || \
   [ ! -f "$FONTS_SRC/fonts.conf.template" ] || \
   [ ! -f "$FONTS_SRC/NOTICE" ] || \
   [ ! -f "$FONTS_SRC/FORTRESS-LICENSE" ] || \
   [ ! -f "$FONTS_SRC/SOURCE.md" ]; then
  echo "font bundle is incomplete: $FONTS_SRC" >&2
  exit 1
fi
cp -a "$FONTS_SRC"/*.ttf "$FONTS_SRC/fonts.conf.template" \
  "$FONTS_SRC/NOTICE" "$FONTS_SRC/FORTRESS-LICENSE" "$FONTS_SRC/SOURCE.md" "$STAGE/fonts/"
if compgen -G "$FONTS_SRC/*.otf" >/dev/null; then
  cp -a "$FONTS_SRC"/*.otf "$STAGE/fonts/"
fi

cat > "$STAGE/chromix" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/fonts/fonts.conf.template"
if [ -f "$TEMPLATE" ]; then
  CACHE_DIR="${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}/chromix/fontconfig"
  mkdir -p "$CACHE_DIR"
  CONFIG="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/chromix-fontconfig-${UID:-0}.conf"
  sed -e "s|@FONTS_DIR@|$HERE/fonts|g" \
      -e "s|@CACHE_DIR@|$CACHE_DIR|g" "$TEMPLATE" > "$CONFIG"
  export FONTCONFIG_FILE="$CONFIG"
fi
exec "$HERE/chrome" "$@"
EOF

chmod 0755 "$STAGE/chrome" "$STAGE/chromix" "$STAGE/chrome-sandbox" \
  "$STAGE/chrome_crashpad_handler"
find "$STAGE/fonts" -type f -exec chmod 0644 {} +

ASSET="$DEST/chromix-linux-x64.tar.gz"
rm -f "$ASSET"
(
  cd "$DEST"
  tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 \
    -czf "$(basename "$ASSET")" chromix
)
HASH="$(sha256sum "$ASSET" | awk '{print $1}')"
printf '%s  %s\n' "$HASH" "$(basename "$ASSET")" > "$DEST/SHA256SUMS"
printf '==> %s  sha256=%s\n' "$ASSET" "$HASH"
