#!/usr/bin/env bash
# Linux builds; x64 -> arm64 requires a full restored upstream cache.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO/build/posix/upstream-cache.sh"
BUILD_PROFILE="${CHROMIX_BUILD_PROFILE:-release}"
case "$BUILD_PROFILE" in
  fast|release) ;;
  *) echo "CHROMIX_BUILD_PROFILE must be fast or release" >&2; exit 2 ;;
esac
WORK="${1:-$REPO/.chromix-build-linux}"
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in x86_64) HOST_ARCH=x64 ;; aarch64) HOST_ARCH=arm64 ;; esac
ARCH="${2:-$HOST_ARCH}"
case "$ARCH" in
  x64) SYSROOT_ARCH=amd64 ;;
  arm64) SYSROOT_ARCH=arm64 ;;
  *) echo "unsupported Linux architecture: $ARCH" >&2; exit 2 ;;
esac
# Host tools (Node/Go used by generators) must run on the actual host; the
# target architecture only selects sysroots and output binaries. Upstream
# portablelinux keeps linux-amd64 Go on x64 hosts regardless of ARCH.
case "$HOST_ARCH" in
  x64) GO_ARCH=amd64 ;;
  arm64) GO_ARCH=arm64 ;;
esac
case "$(uname -s):$HOST_ARCH:$ARCH" in
  Linux:x64:x64|Linux:arm64:arm64|Linux:x64:arm64) ;;
  *) echo "unsupported Linux host/target pair: $HOST_ARCH -> $ARCH" >&2; exit 2 ;;
esac
if [ "$HOST_ARCH" != "$ARCH" ] && [ ! -f "$WORK/src/.chromix-upstream-restored.json" ]; then
  echo "Linux x64 -> arm64 requires a full restored upstream cache; cold cross builds are unsupported" >&2
  exit 2
fi
mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
SRC="$WORK/src"
OUT="$SRC/out/Chromix"
export TMPDIR="${TMPDIR:-$WORK/tmp}"
mkdir -p "$TMPDIR"
TMPDIR="$(cd "$TMPDIR" && pwd)"
"$REPO/build/prepare-ungoogled.sh" "$WORK" linux "$ARCH"
if [ -f "$SRC/.chromix-upstream-restored.json" ]; then
  OUT="$SRC/out/Default"
fi
if [ "${CHROMIX_SKIP_DEPS:-0}" != 1 ]; then
  "$SRC/build/install-build-deps.sh" --no-prompt
fi
cd "$SRC"
chromix_select_restored_ninja linux
for tool in node go gperf clang-format "$CHROMIX_NINJA"; do
  command -v "$tool" >/dev/null || { echo "required build tool is missing: $tool" >&2; exit 1; }
done
if ! node --input-type=module -e 'process.exit(typeof import.meta.main === "boolean" ? 0 : 1)'; then
  echo "Node.js 22.18+ or 24.2+ is required for DevTools generation" >&2
  exit 1
fi
# Refresh host-tool links when PATH changes between builds. Host tools
# (Node/Go consumed by generators) follow the host architecture, mirroring
# upstream portablelinux's setup_toolchain; target_cpu selects binaries only.
for node_arch in x64 "$HOST_ARCH"; do
  mkdir -p "third_party/node/linux/node-linux-$node_arch/bin"
  ln -sfn "$(command -v node)" "third_party/node/linux/node-linux-$node_arch/bin/node"
done
mkdir -p third_party/gperf/cipd/bin buildtools/linux64-format
ln -sfn "$(command -v gperf)" third_party/gperf/cipd/bin/gperf
ln -sfn "$(command -v clang-format)" buildtools/linux64-format/clang-format
# Dawn resolves Go from its cipd-style host dir; linux-amd64 on x64 hosts and
# linux-arm64 on arm64 hosts regardless of the target architecture.
mkdir -p "third_party/dawn/tools/golang/linux-$GO_ARCH/bin"
ln -sfn "$(command -v go)" "third_party/dawn/tools/golang/linux-$GO_ARCH/bin/go"
"third_party/dawn/tools/golang/linux-$GO_ARCH/bin/go" version
if [ -f "$SRC/.chromix-upstream-restored.json" ]; then
  bash "$REPO/build/posix/prepare-restored-tools.sh" "$WORK" linux "$ARCH"
fi
if [ ! -f "$SRC/.chromix-toolchain-ready" ]; then
  if [ "$HOST_ARCH" != "$ARCH" ]; then
    echo "restored cross tool preparation did not complete; cold cross builds are unsupported" >&2; exit 1
  fi
  if [ -f "$SRC/.chromix-domain-substituted" ]; then
    echo "toolchain is incomplete in a domain-substituted source tree; use a clean work directory" >&2; exit 1
  fi
  chromix_import_upstream_cache toolchain linux
  if chromix_has_upstream_toolchain; then
    echo "==> reusing verified upstream LLVM/Rust toolchains"
  elif [ "$HOST_ARCH" = x64 ]; then
    python3 tools/rust/update_rust.py
    python3 tools/clang/scripts/update.py
  else
    python3 tools/clang/scripts/build.py \
      --without-fuchsia --without-android --disable-asserts \
      --host-cc=clang --host-cxx=clang++ --use-system-cmake --with-ml-inliner-model=
    export CARGO_HOME="$SRC/third_party/rust-src/cargo-home"
    python3 tools/rust/build_rust.py --skip-test
  fi
  python3 build/linux/sysroot_scripts/install-sysroot.py --arch="$SYSROOT_ARCH"
  if [ "$HOST_ARCH" = arm64 ] && [ ! -x third_party/rust-toolchain/bin/bindgen ]; then
    python3 tools/rust/build_bindgen.py --skip-test
  fi
  test -x third_party/rust-toolchain/bin/bindgen
  touch "$SRC/.chromix-toolchain-ready"
fi
export CC="$SRC/third_party/llvm-build/Release+Asserts/bin/clang"
export CXX="$SRC/third_party/llvm-build/Release+Asserts/bin/clang++"
export AR="$SRC/third_party/llvm-build/Release+Asserts/bin/llvm-ar"
export NM="$SRC/third_party/llvm-build/Release+Asserts/bin/llvm-nm"
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
  --platform-tooling "$WORK/tooling/ungoogled-chromium-portablelinux" --platform linux --output "$SOURCE_REPORT"
cp "$SOURCE_REPORT" "$WORK/fingerprint-diagnostics/source-final.json"
mkdir -p "$OUT"
printf 'target_cpu = "%s"\nv8_target_cpu = "%s"\n' "$ARCH" "$ARCH" > "$WORK/target.gn"
GN_INPUTS=("$WORK/tooling/ungoogled-chromium/flags.gn"
  "$WORK/tooling/ungoogled-chromium-portablelinux/flags.linux.gn"
  "$REPO/build/args.gn" "$WORK/target.gn")
if [ -f "$SRC/.chromix-upstream-restored.json" ]; then
  GN_INPUTS=("$OUT/args.gn" "${GN_INPUTS[@]}")
fi
printf '==> Build profile: %s\n' "$BUILD_PROFILE"
python3 "$REPO/tools/merge_gn_args.py" --build-profile "$BUILD_PROFILE" "$OUT/args.gn" "${GN_INPUTS[@]}"
# GN's standalone bootstrap still treats this libstdc++ warning as an error.
CXXFLAGS="${CXXFLAGS:+$CXXFLAGS }-Wno-deprecated-declarations" \
  python3 "$REPO/tools/bootstrap_gn.py" --src "$SRC" --out "$OUT"
"$OUT/gn" gen "$OUT" --fail-on-unused-args
chromix_report_upstream_plan chrome chrome_crashpad_handler chrome_sandbox
chromix_build_restored_target linux "${CHROMIX_JOBS:-$(getconf _NPROCESSORS_ONLN)}" chrome chrome_crashpad_handler chrome_sandbox
if [ "$HOST_ARCH" = "$ARCH" ]; then
  "$OUT/chrome" --version
else
  echo "==> cross build: runtime validation deferred to the required native ARM64 job; runtime is not verified"
fi
