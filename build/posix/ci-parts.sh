#!/usr/bin/env bash
# Snapshot a POSIX build work tree for stage-to-stage handoff, modeled on
# ungoogled-chromium-portablelinux's export/import-cache scripts: tar|zstd so
# mtimes, modes, and symlinks survive the round trip ninja needs. Slicing into
# numbered volumes keeps restore order explicit; volumes distribute
# round-robin over fixed upload slots.
#
# >4 volumes means the artifact total exceeds what every Actions plan accepts;
# continue with an explicit warning instead of aborting like the Windows side,
# because re-packing hundreds of gigabytes buys nothing once the upload cap is
# the real constraint.
set -euo pipefail

ROOT="${1:?usage: ci-parts.sh ROOT PARTS_DIR}"
PARTS_DIR="${2:?usage: ci-parts.sh ROOT PARTS_DIR}"
MAX_SLOTS="${CHROMIX_SNAPSHOT_MAX_SLOTS:-4}"
MAX_VOLUMES="${CHROMIX_SNAPSHOT_MAX_VOLUMES:-8}"
# Overridable for tests; production keeps each volume under the per-artifact
# cap GitHub enforces on every plan.
VOLUME_BYTES="${CHROMIX_SNAPSHOT_VOLUME_BYTES:-$((9 * 1024 * 1024 * 1024))}"

command -v zstd >/dev/null 2>&1 || { echo "zstd is not installed" >&2; exit 1; }
[ -d "$ROOT" ] || { echo "snapshot root does not exist: $ROOT" >&2; exit 1; }

ROOT="$(CDPATH= cd -- "$ROOT" && pwd -P)"
mkdir -p -- "$PARTS_DIR"
PARTS_DIR="$(CDPATH= cd -- "$PARTS_DIR" && pwd -P)"
# Resolve symlinks and .. before checking the destructive cleanup boundary.
case "$ROOT/" in
  "${PARTS_DIR%/}/"*)
    echo "snapshot parts directory must not equal or contain root: $PARTS_DIR" >&2
    exit 1
    ;;
esac
rm -rf "${PARTS_DIR:?}"/*

stage_dir="$PARTS_DIR/stage"
mkdir -p "$stage_dir"
# A failed pipeline must never expose partial volumes in upload slots.
trap 'rm -rf "$stage_dir"' EXIT
# GNU split (gsplit on macOS) starts decimal suffixes at zero with -d.
# Select before consuming stdin: a failed stream cannot be retried.
if command -v gsplit >/dev/null 2>&1; then SPLIT=gsplit; else SPLIT=split; fi

# BSD tar exclusions are unanchored: ./download_cache also drops the required
# tooling/download_cache symlink. find -path matches the full relative path.
find_excludes=(-name '.snapshot-stage-*' -o -path './download_cache' -o -path './dist' -o -path './smoke'
               -o -path './runtime-smoke-stage-*')
case "$PARTS_DIR" in
  "${ROOT%/}/"*)
    parts_relative="${PARTS_DIR#"${ROOT%/}/"}"
    # find -path interprets globs, so escape the literal destination path.
    parts_pattern="${parts_relative//\\/\\\\}"
    parts_pattern="${parts_pattern//\*/\\*}"
    parts_pattern="${parts_pattern//\?/\\?}"
    parts_pattern="${parts_pattern//\[/\\[}"
    find_excludes+=(-o -path "./$parts_pattern")
    ;;
esac
# Disable tar recursion so only the pruned, NUL-delimited entries are archived.
# POSIX pax preserves nanosecond mtimes with both GNU and BSD tar.
# Repeat directory metadata last, children before parents, for BSD re-restores.
# Keep the initial traversal order for GNU tar's deferred symlink restoration.
(
  cd -- "$ROOT"
  find . \( "${find_excludes[@]}" \) -prune -o -print0
  find . \( "${find_excludes[@]}" \) -prune -o -type d -print0 |
    TMPDIR="$stage_dir" LC_ALL=C sort -zr
) | tar --format=pax -cpf - --no-recursion --null -C "$ROOT" -T - |
  zstd -T0 -3 -c |
  "$SPLIT" -a 3 -d -b "$VOLUME_BYTES" - "$stage_dir/vol"

total=0
volumes=0
for vol in "$stage_dir"/vol*; do
  [ -s "$vol" ] || { echo "empty snapshot volume" >&2; exit 1; }
  size="$(stat -c %s "$vol" 2>/dev/null || stat -f %z "$vol")"
  total=$((total + size))
  volumes=$((volumes + 1))
done
echo "==> packed ${total} bytes as $volumes volumes (streaming)"

if [ "$volumes" -gt "$MAX_VOLUMES" ]; then
  echo "ERROR: $volumes volumes exceed the ${MAX_VOLUMES}-volume handoff budget;" \
    "tree too large to hand off via artifacts" >&2
  rm -rf "$stage_dir"
  exit 1
fi

i=0
for vol in "$stage_dir"/vol*; do
  [ -e "$vol" ] || break
  i=$(( i + 1 ))
  slot=$(( (i - 1) % MAX_SLOTS + 1 ))
  part_dir="$PARTS_DIR/p$slot"
  mkdir -p "$part_dir"
  mv "$vol" "$part_dir/tree.tar.zst.$(printf '%03d' "$i")"
done

rm -rf "$stage_dir"
# Null-delimited paths survive spaces in runner.temp/part dir names.
find "$PARTS_DIR" -type f -name 'tree.tar.zst.*' -print0 |
  sort -z | xargs -0 ls -lh

if [ "$volumes" -gt "$MAX_SLOTS" ]; then
  echo "::warning::$volumes snapshot volumes exceed 4 upload slots;" \
    "artifact size caps depend on the GitHub plan - verify this upload succeeds"
fi
