#!/usr/bin/env python3
"""Merge downloaded per-architecture Rust bundles into third_party/rust-toolchain.

Ported verbatim from ungoogled-chromium-windows' build.py merge loop, which
is what upstream's field-proven Windows CI uses. GitHub Actions runs our
prepare scripts through Windows PowerShell 5.1; a PowerShell transcription of
this copy loop lost bin/cargo.exe silently on the first Chromix CI run, so
keep byte-for-byte parity with the upstream Python instead of trusting any
shell copy semantics - and verify the result before returning.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Directories to copy from each source bundle to the target folder.
DIRS_TO_COPY = ["bin", "lib"]
BINARIES_THAT_MUST_EXIST = ["cargo.exe", "rustc.exe"]


def fail(message):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def merge_toolchain(sources, destination):
    # 64-bit hosts take their executables from the x64 bundle only, exactly
    # like upstream; libraries still merge from every available bundle.
    host_is_64bit = sys.maxsize > 2**32

    for part in DIRS_TO_COPY:
        target_dir = destination / part
        if not target_dir.is_dir():
            target_dir.mkdir(parents=True)
        for source in sources:
            if (part == "bin") and (
                host_is_64bit != (source.name == "rust-toolchain-x64")
            ):
                continue
            for cp_src in source.glob(f"*/{part}/*"):
                cp_dst = target_dir / cp_src.name
                if cp_src.is_dir():
                    shutil.copytree(cp_src, cp_dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(cp_src, cp_dst)


def write_installed_version(destination, sources):
    rustc = sources[0] / "rustc" / "bin" / "rustc.exe"
    if not rustc.is_file():
        fail(f"x64 bundle rustc is missing: {rustc}")
    with open(destination / "INSTALLED_VERSION", "w") as handle:
        subprocess.run([str(rustc), "--version"], stdout=handle, check=True)


def verify(destination, third_party_root, arch="x64"):
    missing = [name for name in BINARIES_THAT_MUST_EXIST
               if not (destination / "bin" / name).is_file()]
    if missing:
        listing = sorted(p.name for p in destination.rglob("*"))[:40]
        bundles = sorted(
            p.name for p in third_party_root.glob("rust-toolchain-*"))
        fail("merge did not produce "
             f"{', '.join('bin/' + m for m in missing)} under {destination}; "
             f"bundles found: {bundles or 'none'}; merged entries: "
             f"{listing or 'none'}")
    if arch == "arm64":
        for triple in ("x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc"):
            libraries = destination / "lib" / "rustlib" / triple / "lib"
            for pattern in ("libstd-*.rlib", "libcore-*.rlib", "liballoc-*.rlib",
                            "libcompiler_builtins-*.rlib"):
                matches = list(libraries.glob(pattern))
                if not matches or any(path.stat().st_size == 0 for path in matches):
                    fail(f"merged Rust {triple} libraries missing or empty: {pattern}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--third-party-root",
                        help="source tree third_party directory containing "
                             "rust-toolchain-x64/x86/arm bundles and the "
                             "rust-toolchain merge target (default: cwd)")
    parser.add_argument("--arch", choices=("x64", "arm64"), default="x64")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--chromium-version", default="")
    args = parser.parse_args()

    root = Path(args.third_party_root or ".").resolve()
    destination = root / "rust-toolchain"
    if args.verify_only:
        verify(destination, root, args.arch)
        return
    version157 = args.chromium_version == "157.0.8103.78"
    if args.chromium_version and not version157:
        fail("unsupported version-specific Rust merge")
    if destination.exists():
        if version157:
            if destination.is_symlink() or set(p.name for p in destination.iterdir()) != {"crubit"}:
                fail("Windows157 fresh Rust destination must contain only downloaded Crubit")
        else:
            shutil.rmtree(destination)

    sources = []
    for suffix in ("x64", "x86", "arm"):
        candidate = root / f"rust-toolchain-{suffix}"
        if candidate.is_dir():
            sources.append(candidate)
    if not any(source.name == "rust-toolchain-x64"
               for source in sources):
        fail(f"no downloaded x64 rust bundle under {root}")

    merge_toolchain(sources, destination)
    if version157:
        development = root / "rust-toolchain-x64-dev"
        libraries = list(development.glob("*/lib/*"))
        if not libraries or any(not path.is_dir() or path.is_symlink() for path in libraries):
            fail("exact Windows157 rustc-dev host libraries missing or linked")
        for path in libraries:
            shutil.copytree(path, destination / "lib" / path.name, dirs_exist_ok=True)
    # Verify before writing the version stamp: a missing cargo must fail even
    # on platforms where executing the bundled rustc binary is impossible.
    verify(destination, root, args.arch)
    write_installed_version(destination, sources)
    print(f"==> merged Rust toolchain: {len(sources)} bundles -> {destination}")


if __name__ == "__main__":
    main()
