#!/usr/bin/env python3
"""Validate exact Windows157 host generators and prepare the fresh Crubit tool."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import struct
import subprocess
import sys

try:
    from . import install_windows_metagen_bindings as metagen
    from .platform_pins import load_pins
except ImportError:
    import install_windows_metagen_bindings as metagen
    from platform_pins import load_pins

VERSION = "157.0.8103.78"
CORE = "8c886dffc6ade39d8f7df5d64ace5a8824b455e1"
WINDOWS = "20e9a348fc616bd73a366fbd5d8881613d6c5cb0"
CRUBIT = "20dc640430bd60ff2f7f8dbf69c311c5f807adde"
CRUBIT_ROOT = "third_party/rust-toolchain/crubit"
HOST_TRIPLE = "x86_64-pc-windows-msvc"
CRUBIT_BIN_MANIFEST = "cargo/cc_bindings_from_rs/cc_bindings_from_rs/Cargo.toml"
CRUBIT_BIN_SHA256 = "b236a5902e6eada4433b14dfadd472e50f8a972166a5ea1286cf34a54ac1c543"
CRUBIT_INPUTS = {
    "Cargo.toml": "6858ed7e72c061faaa0251205dd38068deedec0573398513a54e3d722d8b1615",
    "Cargo.lock": "c296d483ba3a46ab228d51af4445e84c02180128d88d8ac472e6fc055b9efbfc",
}
TOOLING = {
    "downloads.ini": "78a25f0b00e2a8988807025fa083f3a1caa58077a5e15d58ce0aa62a9ff0e19f",
    "build.py": "c45db5298979c9b25bc5f9667fbbed42ca3af547ade0a7109ebb7c86737c3b6e",
    "flags.windows.gn": "80672194492e03c9e022f036d7918edeaeda7e7fe060c13aa6c4ed190a0ee2b2",
}
PGO = {
    "x64": "chrome-win64-8103-1794309577-cf2b72f4243669cb9a85737ae6dc9c12adab5d81-4dab8e40e268505db4624f458ef8821ff42a7a64.profdata",
    "arm64": "chrome-win-arm64-main-1791827984-0345e8d81a06ba4b3f9957be64a2d237eaea5b09-106edaadeecbbcc690ffeac4a85ea4c14e171497.profdata",
}
MINIBRANCH = b"MINIBRANCH_BASE_VERSION=\n"


def file(path: Path) -> Path:
    metagen.local(path)
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size == 0:
        raise ValueError("missing, empty or linked Windows157 dependency: " + str(path))
    return path


def sha(path: Path) -> str:
    with file(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_pins(repo: Path) -> None:
    pins = load_pins(repo, "windows")
    if (pins["ChromiumVersion"], pins["UngoogledCommit"], pins["UngoogledWindowsCommit"]) != (VERSION, CORE, WINDOWS):
        raise ValueError("Windows157 tools require exact source pins")


def pe_executable(path: Path) -> None:
    with file(path).open("rb") as stream:
        dos = stream.read(64)
        if len(dos) != 64 or dos[:2] != b"MZ":
            raise ValueError("Windows157 host tool requires AMD64 PE: " + str(path))
        offset = struct.unpack_from("<I", dos, 60)[0]
        if offset < 64 or offset + 26 > path.stat().st_size:
            raise ValueError("invalid host PE offset")
        stream.seek(offset)
        header = stream.read(26)
        machine, sections = struct.unpack_from("<HH", header, 4)
        optional, flags, magic = struct.unpack_from("<HHH", header, 20)
        if (header[:4] != b"PE\0\0" or machine != 0x8664 or magic != 0x20B
                or not flags & 2 or flags & 0x2000 or not 1 <= sections <= 96
                or optional < 112 or offset + 24 + optional + sections * 40 > path.stat().st_size):
            raise ValueError("Windows157 host tool requires AMD64 PE executable")


def inputs(src: Path) -> Path:
    root = src / CRUBIT_ROOT
    for name, expected in CRUBIT_INPUTS.items():
        if sha(root / "lib/third_party/crubit" / name) != expected:
            raise ValueError("exact Crubit input SHA256 mismatch: " + name)
    return root


def dependencies(src: Path) -> None:
    if file(src / "chrome/VERSION").read_text().splitlines() != ["MAJOR=157", "MINOR=0", "BUILD=8103", "PATCH=78"]:
        raise ValueError("Windows157 source version mismatch")
    metagen.validate_sources(src, VERSION)
    root = inputs(src)
    for relative in ("third_party/node/win/node.exe", "third_party/ninja/ninja.exe",
                     "third_party/llvm-build/Release+Asserts/bin/clang-cl.exe",
                     "third_party/llvm-build/Release+Asserts/bin/llvm-profdata.exe",
                     "third_party/dawn/tools/golang/windows-amd64/bin/go.exe",
                     "third_party/devtools-frontend/src/third_party/esbuild/esbuild.exe",
                     "third_party/typescript/windows-amd64/src/lib/tsc.exe"):
        pe_executable(src / relative)
    pe_executable(root / "bin/cc_bindings_from_rs.exe")
    drivers = list((root / "bin").glob("rustc_driver*.dll"))
    if not drivers:
        raise ValueError("Crubit rustc_driver runtime missing")
    for driver in drivers:
        metagen.validate_library(file(driver))
        if sha(driver) != sha(src / "third_party/rust-toolchain/bin" / driver.name):
            raise ValueError("Crubit rustc_driver differs from host Rust toolchain")
    if file(src / "build/util/MINIBRANCH").read_bytes() != MINIBRANCH:
        raise ValueError("exact 157 branch MINIBRANCH mismatch")
    declarations = file(src / "third_party/typescript/windows-amd64/src/lib/lib.dom.d.ts").read_text()
    if (declarations.count("set innerHTML(html: string|TrustedHTML);") != 2
            or "set src(url: string|TrustedScriptURL);" not in declarations
            or "hidden: boolean;" not in declarations):
        raise ValueError("Windows157 native TypeScript DOM modifications missing")


def restored_fingerprint(src: Path) -> dict:
    result = {}
    for name, relative in {
        "typescript": "third_party/typescript/windows-amd64/src/lib/tsc.exe",
        "typescript_dom": "third_party/typescript/windows-amd64/src/lib/lib.dom.d.ts",
        "esbuild": "third_party/devtools-frontend/src/third_party/esbuild/esbuild.exe",
        "crubit": CRUBIT_ROOT + "/bin/cc_bindings_from_rs.exe",
    }.items():
        result[name] = {"path": relative, "sha256": sha(src / relative)}
    return result


def cargo_environment(rust: Path, target: Path) -> dict:
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith(("CARGO_BUILD_", "CARGO_TARGET_")) or key in (
                "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"):
            del environment[key]
    environment.update(RUSTC=str(rust / "rustc.exe"), CARGO_HOME=str(target / "cargo_home"),
                       RUSTFLAGS="-Ctarget-feature=+crt-static")
    return environment


def cargo_output(stdout: str, src: Path, target: Path) -> Path:
    expected = target / HOST_TRIPLE / "release/cc_bindings_from_rs.exe"
    manifest = src / CRUBIT_ROOT / "lib/third_party/crubit" / CRUBIT_BIN_MANIFEST
    matches = []
    finished = []
    for line in stdout.splitlines():
        message = json.loads(line)
        if message.get("reason") == "build-finished":
            finished.append(message.get("success"))
        if message.get("reason") != "compiler-artifact" or message.get("target", {}).get("name") != "cc_bindings_from_rs":
            continue
        if (message["target"].get("kind") != ["bin"]
                or message.get("profile", {}).get("test") is not False
                or not message.get("executable") or not message.get("manifest_path")
                or Path(message["manifest_path"]).resolve() != manifest.resolve()
                or Path(message["executable"]).resolve() != expected.resolve()
                or str(message["executable"]) not in message.get("filenames", [])):
            raise ValueError("unverified Crubit Cargo compiler-artifact")
        matches.append(expected)
    if finished != [True] or len(matches) != 1:
        raise ValueError("Crubit Cargo build lacks one authenticated host binary result")
    pe_executable(expected)
    return expected


def prepare(src: Path, work: Path) -> None:
    metagen.validate_runtime(metagen.runtime_info())
    if metagen.source_contract(src, src / "out/Chromix", VERSION) != "x64":
        raise ValueError("Windows157 fresh host tools require x64 source target")
    metagen.validate_sources(src, VERSION)
    for name, expected in TOOLING.items():
        data = file(work / "tooling/ungoogled-chromium-windows" / name).read_bytes()
        if hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() != expected:
            raise ValueError("Windows157 upstream tool SHA256 mismatch: " + name)
    marker = src / "build/util/MINIBRANCH"
    # Exact tag 4b3d0dabb825 has refs/branch-heads/8103, not a minibranch ref.
    if marker.exists():
        if file(marker).read_bytes() != MINIBRANCH:
            raise ValueError("preserved MINIBRANCH differs from exact tag")
    else:
        metagen.local(marker)
        marker.write_bytes(MINIBRANCH)
    root = inputs(src)
    executable = root / "bin/cc_bindings_from_rs.exe"
    if not executable.exists():
        rust = src / "third_party/rust-toolchain/bin"
        for name in ("cargo.exe", "rustc.exe"):
            pe_executable(rust / name)
        target = src / "out/crubit"
        if sha(root / "lib/third_party/crubit" / CRUBIT_BIN_MANIFEST) != CRUBIT_BIN_SHA256:
            raise ValueError("Crubit binary Cargo manifest SHA256 mismatch")
        environment = cargo_environment(rust, target)
        result = subprocess.run([str(rust / "cargo.exe"), "build", "--release", "--locked",
                        "--target", HOST_TRIPLE, "--message-format=json-render-diagnostics",
                        "-p", "cc_bindings_from_rs", "--bin", "cc_bindings_from_rs",
                        "--manifest-path", str(root / "lib/third_party/crubit/Cargo.toml"),
                        "--target-dir", str(target)], cwd=src, env=environment, check=True,
                        stdout=subprocess.PIPE, text=True, timeout=3600)
        built = cargo_output(result.stdout, src, target)
        digest = sha(built)
        executable.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, executable)
        if sha(executable) != digest or sha(built) != digest:
            raise ValueError("Crubit compiler output changed during installation")
        for dll in rust.glob("*.dll"):
            metagen.validate_library(file(dll))
            shutil.copy2(dll, executable.parent / dll.name)
    pe_executable(executable)
    subprocess.run([str(executable), "--help"], check=True, stdout=subprocess.DEVNULL, timeout=60)


def probe(src: Path, out: Path, arch: str) -> None:
    metagen.validate_runtime(metagen.runtime_info())
    metagen.source_contract(src, out, VERSION)
    dependencies(src)
    arguments = file(out / "args.gn").read_text()
    for key, expected in (("chrome_pgo_phase", "2"), ("target_cpu", json.dumps(arch)),
                          ("host_cpu", '"x64"')):
        values = re.findall(r"(?m)^\s*" + key + r"\s*=\s*([^\r\n#]+)", arguments)
        if key == "host_cpu" and not values:
            continue
        if [value.strip() for value in values] != [expected]:
            raise ValueError("Windows157 requires target-matched native PGO2 and AMD64 host")
    for relative, expected in (("third_party/node/win/node.exe", "v24.12.0"),
                               ("third_party/devtools-frontend/src/third_party/esbuild/esbuild.exe", "0.28.2"),
                               ("third_party/typescript/windows-amd64/src/lib/tsc.exe", "Version 7.0.2")):
        result = subprocess.run([str(src / relative), "--version"], check=True, capture_output=True, text=True, timeout=60)
        if result.stdout.strip() != expected:
            raise ValueError("Windows157 host generator version mismatch: " + relative)
    target = "win64" if arch == "x64" else "win-arm64"
    if file(src / f"chrome/build/{target}.pgo.txt").read_text().strip() != PGO[arch]:
        raise ValueError("Windows157 exact target PGO selector mismatch")
    profile = file(src / "chrome/build/pgo_profiles" / PGO[arch])
    subprocess.run([str(src / "third_party/llvm-build/Release+Asserts/bin/llvm-profdata.exe"), "show", str(profile)],
                   check=True, timeout=120)
    subprocess.run([str(src / CRUBIT_ROOT / "bin/cc_bindings_from_rs.exe"), "--help"],
                   check=True, stdout=subprocess.DEVNULL, timeout=60)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--phase", choices=("prepare", "probe"), required=True)
    args = parser.parse_args()
    try:
        checked_pins(args.repo)
        src = args.workdir.resolve() / "src"
        restored = (src / ".chromix-upstream-restored.json").exists()
        if args.phase == "prepare":
            if restored or args.arch != "x64":
                raise ValueError("Windows157 tool preparation is fresh x64 only")
            import windows157_fresh
            windows157_fresh.initialize(args.repo, args.workdir, args.arch, resume=True)
            prepare(src, args.workdir)
        else:
            if args.arch == "arm64" and not restored:
                raise ValueError("Windows157 ARM64 requires exact upstream restore")
            probe(src, src / ("out/Default" if restored else "out/Chromix"), args.arch)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Windows157 host tool validation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
