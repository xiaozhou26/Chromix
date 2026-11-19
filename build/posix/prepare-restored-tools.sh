#!/usr/bin/env bash
# Prepare native host tools only; the normal builder regenerates GN and links Chromium.
set -euo pipefail
WORK="${1:?usage: prepare-restored-tools.sh WORKDIR linux|macos x64|arm64}"
PLATFORM="${2:?platform is required}"
ARCH="${3:?architecture is required}"
case "$PLATFORM:$ARCH" in linux:x64|linux:arm64|macos:x64|macos:arm64) ;; *) exit 2 ;; esac
if [ "$PLATFORM" = macos ]; then
  unset -- "${!DYLD_@}"
fi
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOST="$(uname -m)"
case "$HOST" in x86_64) HOST=x64 ;; aarch64) HOST=arm64 ;; esac
SYSTEM="$(uname -s)"
case "$SYSTEM:$PLATFORM:$HOST:$ARCH" in
  Linux:linux:x64:x64|Linux:linux:arm64:arm64|Darwin:macos:x64:x64|Darwin:macos:arm64:arm64|Linux:linux:x64:arm64) ;;
  *) echo "a native $PLATFORM $ARCH runner is required (except restored Linux x64 -> arm64)" >&2; exit 2 ;;
esac
if [ "$HOST" != "$ARCH" ] && [ ! -f "$WORK/src/.chromix-upstream-restored.json" ]; then
  echo "Linux x64 -> arm64 requires a full restored upstream cache; cold cross builds are unsupported" >&2
  exit 2
fi
WORK="$(cd "$WORK" && pwd)"
SRC="$WORK/src"
if [ "$PLATFORM" = macos ] && [ -f "$SRC/.chromix-upstream-restored.json" ]; then
  MACOS_RUNTIME_ENV="$(python3 "$REPO/tools/macos_runtime.py" --src "$SRC" --arch "$ARCH")"
  eval "$MACOS_RUNTIME_ENV"
fi
INSPECT="$(python3 "$REPO/tools/prepare_restored_build.py" --phase inspect \
  --platform "$PLATFORM" --arch "$ARCH" --workdir "$WORK")"
field() {
  python3 -c 'import json, sys; value=json.loads(sys.argv[1]);
for key in sys.argv[2].split("."): value=value[key]
print("1" if value else "0")' "$INSPECT" "$1"
}
COMPILERS_NATIVE="$(field compilers_native)"
BINDGEN_NATIVE="$(field tools.bindgen.native)"
NODE_NATIVE="$(field tools.node.native)"
RETRIEVE=0
if [ "$PLATFORM" = macos ] && { [ "$COMPILERS_NATIVE" != 1 ] || [ "$NODE_NATIVE" != 1 ]; }; then
  RETRIEVE=1
fi
if [ "$COMPILERS_NATIVE" != 1 ] || [ "$BINDGEN_NATIVE" != 1 ] || [ "$RETRIEVE" = 1 ]; then
  if [ "${GITHUB_ACTIONS:-}" != true ]; then
    echo "restored tool downloads/builds are restricted to GitHub Actions" >&2
    exit 1
  fi
fi
# Only fixed tooling/host-link paths and known downloader endpoints are changed.
python3 - "$REPO" "$WORK" "$PLATFORM" "$ARCH" "$HOST" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from tools.prepare_restored_build import prepare_tooling_links, verify_tooling
verify_tooling(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[1]))
prepare_tooling_links(Path(sys.argv[2]), sys.argv[3], sys.argv[4], host_arch=sys.argv[5])
PY
cd "$SRC"
if [ "$COMPILERS_NATIVE" != 1 ] || [ "$BINDGEN_NATIVE" != 1 ] || [ "$RETRIEVE" = 1 ] || [ "$HOST" != "$ARCH" ]; then
  python3 - "$REPO" "$SRC" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from tools.prepare_restored_build import restore_tool_endpoints
restore_tool_endpoints(Path(sys.argv[2]))
PY
fi
if [ "$PLATFORM" = linux ] && [ "$HOST" = arm64 ] && { [ "$COMPILERS_NATIVE" != 1 ] || [ "$BINDGEN_NATIVE" != 1 ]; }; then
  python3 - "$REPO" "$SRC" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from tools.prepare_restored_build import repair_linux_arm64_tool_script
repair_linux_arm64_tool_script(Path(sys.argv[2]))
PY
fi
if [ "$PLATFORM" = macos ]; then
  if [ "$RETRIEVE" = 1 ]; then
    RESOURCE_ARCH="$ARCH"
    [ "$ARCH" != x64 ] || RESOURCE_ARCH=x86_64
    python3 "$WORK/tooling/ungoogled-chromium-macos/retrieve_and_unpack_resource.py" -p "$RESOURCE_ARCH"
    BINDGEN_NATIVE=0
  fi
  if [ -f "$SRC/.chromix-upstream-restored.json" ]; then
    MACOS_RUNTIME_ENV="$(python3 "$REPO/tools/macos_runtime.py" --src "$SRC" --arch "$ARCH" \
      --prepare-loader --repair-bindgen-wrapper)"
    eval "$MACOS_RUNTIME_ENV"
    python3 "$REPO/tools/macos_runtime.py" --src "$SRC" --arch "$ARCH" --verify-loader > /dev/null
  fi
  if [ "$BINDGEN_NATIVE" != 1 ]; then
    python3 tools/rust/build_bindgen.py --skip-test
  fi
  if [ "$(cat "$REPO/CHROMIUM_MACOS_VERSION")" = 157.0.8103.78 ]; then
    # Resource replacement removes the donor's host-built Crubit executable.
    probe_crubit() {
      python3 - "$REPO" "$SRC" "$ARCH" <<'PYCRUBIT'
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, sys.argv[1])
from tools.prepare_restored_build import binary_architectures
src = Path(sys.argv[2])
path = src / "third_party/rust-toolchain/crubit/bin/cc_bindings_from_rs"
if any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(src)):
    print("unsafe macOS Crubit executable path", file=sys.stderr)
    raise SystemExit(2)
if not path.is_file() or not os.access(path, os.X_OK):
    raise SystemExit("macOS Crubit executable is missing")
if sys.argv[3] not in binary_architectures(path, "macos"):
    raise SystemExit("macOS Crubit executable does not match the native host")
try:
    subprocess.run([str(path), "--help"], check=True, timeout=30,
                   stdout=subprocess.DEVNULL,
                   env={k: v for k, v in os.environ.items() if not k.startswith("DYLD_")})
except (OSError, subprocess.SubprocessError) as error:
    raise SystemExit(f"macOS Crubit native probe failed: {error}")
PYCRUBIT
    }
    if probe_crubit; then
      :
    else
      CRUBIT_RC=$?
      [ "$CRUBIT_RC" -ne 2 ] || exit "$CRUBIT_RC"
      if [ "${GITHUB_ACTIONS:-}" != true ]; then
        echo "macOS Crubit build is restricted to GitHub Actions" >&2
        exit 1
      fi
      echo "==> Building native macOS Crubit with the verified upstream recipe"
      bash "$WORK/tooling/ungoogled-chromium-macos/build_crubit.sh"
      probe_crubit
    fi
  fi
else
  if [ "$ARCH" = x64 ]; then SYSROOT_ARCH=amd64; else SYSROOT_ARCH=arm64; fi
  if [ "$COMPILERS_NATIVE" != 1 ]; then
    if [ "$HOST" = x64 ]; then
      # Matching revisions do not prove that the restored binaries can execute.
      python3 - "$SRC" <<'PY'
from pathlib import Path
import sys
src = Path(sys.argv[1])
paths = [src / name for name in (
    "third_party/rust-toolchain/VERSION",
    "third_party/llvm-build/Release+Asserts/cr_build_revision",
    "third_party/llvm-build/cr_build_revision",
    "third_party/llvm-build/force_head_revision",
)]
for path in paths:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise SystemExit(f"refusing linked toolchain stamp: {path}")
    if path.exists() and not path.is_file():
        raise SystemExit(f"invalid toolchain stamp: {path}")
for path in paths:
    path.unlink(missing_ok=True)
PY
      python3 tools/rust/update_rust.py
      python3 tools/clang/scripts/update.py
      INSPECT="$(python3 "$REPO/tools/prepare_restored_build.py" --phase inspect \
        --platform "$PLATFORM" --arch "$ARCH" --workdir "$WORK")"
      COMPILERS_NATIVE="$(field compilers_native)"
      BINDGEN_NATIVE="$(field tools.bindgen.native)"
      if [ "$COMPILERS_NATIVE" != 1 ]; then
        echo "updated Linux x64 compiler packages cannot execute on this runner" >&2
        exit 1
      fi
    else
      python3 tools/clang/scripts/build.py \
        --without-fuchsia --without-android --disable-asserts \
        --host-cc=clang --host-cxx=clang++ --use-system-cmake --with-ml-inliner-model=
      export CARGO_HOME="$SRC/third_party/rust-src/cargo-home"
      python3 tools/rust/build_rust.py --skip-test
      BINDGEN_NATIVE=0
    fi
  fi
  if [ "$HOST" = x64 ] && [ "$BINDGEN_NATIVE" != 1 ]; then
    echo "Linux x64 Rust package lacks executable bindgen; refusing to build without native Rust LLVM intermediates" >&2
    exit 1
  fi
  SYSROOT_ARCHES=("$SYSROOT_ARCH")
  if [ "$HOST" != "$ARCH" ]; then
    SYSROOT_ARCHES=(amd64 arm64)
  fi
  if [ "${GITHUB_ACTIONS:-}" = true ]; then
    for sysroot_arch in "${SYSROOT_ARCHES[@]}"; do
      python3 build/linux/sysroot_scripts/install-sysroot.py --arch="$sysroot_arch"
    done
  fi
  if [ "$HOST" != "$ARCH" ]; then
    # Match the pinned installer's stamps without downloading on local runs.
    python3 - "$SRC" "${SYSROOT_ARCHES[@]}" <<'PY'
import json
from pathlib import Path
import sys
src = Path(sys.argv[1])
linux = src / "build/linux"
sysroots = json.loads((linux / "sysroot_scripts/sysroots.json").read_text())
for arch in sys.argv[2:]:
    entry = sysroots[f"bullseye_{arch}"]
    directory = f"debian_bullseye_{arch}-sysroot"
    if entry["SysrootDir"] != directory:
        raise SystemExit(f"unexpected pinned {arch} sysroot directory")
    root = linux / directory
    stamp = root / ".stamp"
    expected = entry["URL"] + "/" + entry["Sha256Sum"]
    if (not stamp.is_file() or stamp.read_text() != expected
            or any(root.glob(".*_is_first_class_gcs"))):
        raise SystemExit(f"missing or stale {arch} sysroot for Linux x64 -> arm64; prepare both sysroots in GitHub Actions")
PY
  fi
  if [ "$BINDGEN_NATIVE" != 1 ]; then
    python3 tools/rust/build_bindgen.py --skip-test
  fi
fi
if [ "$PLATFORM" = linux ] && [ "$(python3 -c 'import json, sys
state=json.loads(sys.argv[1]).get("generator_fingerprint", {}).get("typescript", {}).get("esbuild", {})
print("1" if state.get("install_needed") else "0")' "$INSPECT")" = 1 ]; then
  GENERATOR_DOWNLOAD_ARGS=()
  if [ "${GITHUB_ACTIONS:-}" = true ]; then
    GENERATOR_DOWNLOAD_ARGS=(--download)
  fi
  python3 "$REPO/tools/install_linux_generators.py" --repo "$REPO" --workdir "$WORK" --arch "$ARCH" \
    --downloads "${CHROMIX_LINUX_GENERATOR_DOWNLOADS:-$WORK/linux-generator-downloads}" \
    "${GENERATOR_DOWNLOAD_ARGS[@]}"
fi
if [ "$PLATFORM" = macos ] && [ "$(python3 -c 'import json, sys
state=json.loads(sys.argv[1]).get("generator_fingerprint", {}).get("macos154", {})
print("1" if state.get("install_needed") else "0")' "$INSPECT")" = 1 ]; then
  GENERATOR_DOWNLOAD_ARGS=()
  if [ "${GITHUB_ACTIONS:-}" = true ]; then
    GENERATOR_DOWNLOAD_ARGS=(--download)
  fi
  python3 "$REPO/tools/install_macos_generators.py" --repo "$REPO" --workdir "$WORK" --arch "$ARCH" \
    --downloads "${CHROMIX_MACOS_GENERATOR_DOWNLOADS:-$WORK/macos-generator-downloads}" \
    "${GENERATOR_DOWNLOAD_ARGS[@]}"
fi
python3 "$REPO/tools/prepare_restored_build.py" --phase finish \
  --platform "$PLATFORM" --arch "$ARCH" --workdir "$WORK"
touch "$SRC/.chromix-toolchain-ready"
