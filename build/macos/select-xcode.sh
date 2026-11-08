#!/usr/bin/env bash
# Execute for GitHub Actions, or source and call select_macos_xcode locally.

select_macos_xcode() {
  # launch_mac.cc uses posix_spawn_file_actions_addchdir, declared in SDK 26.
  local minimum_sdk=26 minimum_minor=0
  local repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  if [ "$(cat "$repo/CHROMIUM_MACOS_VERSION")" = 156.0.8078.79 ]; then
    minimum_sdk=26.5
    minimum_minor=5
  fi
  local applications="${CHROMIX_XCODE_APPLICATIONS_DIR:-/Applications}"
  local current candidate developer_dir sdk_version sdk_path xcode_version
  local version_pattern='^[0-9]+(\.[0-9]+)*$'
  local tool

  for tool in xcrun xcodebuild xcode-select; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      echo "macOS toolchain selection requires $tool (install Xcode with macOS SDK >= $minimum_sdk)" >&2
      return 1
    fi
  done
  current="$(xcode-select --print-path 2>/dev/null)" || current=""
  printf '==> macOS SDK >= %s required; DEVELOPER_DIR=%s; xcode-select=%s\n' \
    "$minimum_sdk" "${DEVELOPER_DIR:-<unset>}" "${current:-<unset>}" >&2

  # Prefer a supported explicit selection, then the active Xcode, then installed apps.
  for candidate in "${DEVELOPER_DIR:-}" "$current" \
    "$applications"/Xcode*.app "$applications"/Xcodes/Xcode*.app \
    "${HOME:-}/Applications"/Xcode*.app; do
    [ -n "$candidate" ] || continue
    developer_dir="${candidate%/}"
    case "$developer_dir" in *.app) developer_dir="$developer_dir/Contents/Developer" ;; esac
    if [ ! -d "$developer_dir/Platforms/MacOSX.platform/Developer/SDKs" ]; then
      [ ! -d "$candidate" ] || printf 'Skipping %s: full Xcode is required\n' "$candidate" >&2
      continue
    fi
    if ! sdk_version="$(env -u SDKROOT DEVELOPER_DIR="$developer_dir" xcrun --sdk macosx --show-sdk-version 2>&1)"; then
      printf 'Skipping %s: xcrun failed: %s\n' "$developer_dir" "$sdk_version" >&2
      continue
    fi
    printf 'Found %s: macOS SDK %s\n' "$developer_dir" "$sdk_version" >&2
    if ! [[ $sdk_version =~ $version_pattern ]] ||
       ! python3 -c 'import sys; v=tuple(map(int,sys.argv[1].split("."))); sys.exit((v+(0,0))[:2] < (26,int(sys.argv[2])))' "$sdk_version" "$minimum_minor"; then
      printf 'Skipping %s: macOS SDK >= %s required\n' "$developer_dir" "$minimum_sdk" >&2
      continue
    fi
    if ! sdk_path="$(env -u SDKROOT DEVELOPER_DIR="$developer_dir" xcrun --sdk macosx --show-sdk-path 2>&1)"; then
      printf 'Skipping %s: SDK path lookup failed: %s\n' "$developer_dir" "$sdk_path" >&2
      continue
    fi
    if [ ! -d "$sdk_path" ]; then
      printf 'Skipping %s: SDK path does not exist: %s\n' "$developer_dir" "$sdk_path" >&2
      continue
    fi
    if ! xcode_version="$(DEVELOPER_DIR="$developer_dir" xcodebuild -version 2>&1)"; then
      printf 'Skipping %s: xcodebuild failed: %s\n' "$developer_dir" "$xcode_version" >&2
      continue
    fi
    if [ "$minimum_minor" -eq 5 ]; then
      local release="${xcode_version%%$'\n'*}"
      release="${release#Xcode }"
      if ! [[ $release =~ $version_pattern ]] ||
         ! python3 -c 'import sys; v=tuple(map(int,sys.argv[1].split("."))); sys.exit((v+(0,0))[:2] < (26,5))' "$release"; then
        printf 'Skipping %s: Xcode >= 26.5 required\n' "$developer_dir" >&2
        continue
      fi
    fi
    if [ -n "${GITHUB_ENV:-}" ]; then
      printf 'DEVELOPER_DIR=%s\n' "$developer_dir" >> "$GITHUB_ENV" || return 1
      if [ "$minimum_minor" -eq 5 ]; then
        printf 'SDKROOT=%s\n' "$sdk_path" >> "$GITHUB_ENV" || return 1
      fi
    fi
    export DEVELOPER_DIR="$developer_dir"
    if [ "$minimum_minor" -eq 5 ]; then
      export SDKROOT="$sdk_path"
    fi
    printf '==> Selected DEVELOPER_DIR=%s\n%s\nmacOS SDK %s: %s\n' \
      "$DEVELOPER_DIR" "$xcode_version" "$sdk_version" "$sdk_path" >&2
    return 0
  done

  printf 'No usable installed Xcode provides macOS SDK >= %s. Install Xcode %s or newer and set DEVELOPER_DIR to its Contents/Developer directory.\n' "$minimum_sdk" "$minimum_sdk" >&2
  printf 'Searched active/configured Xcode and %s, %s/Xcodes, %s/Applications. GitHub runners must include Xcode 26+ (macos-15 for arm64, macos-15-intel for x64).\n' \
    "$applications" "$applications" "${HOME:-}" >&2
  return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  select_macos_xcode
fi
