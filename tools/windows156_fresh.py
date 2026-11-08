#!/usr/bin/env python3
"""Prepare the explicitly authorized Windows156 native-PGO2 source build."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

from platform_pins import load_pins

VERSION = "156.0.8078.79"
CORE = "28a211ab4ab6b83d919966a4f58a6df23cf4fb29"
WINDOWS = "8492884eb30fdd9417ea7816aa92def805c97c74"
PROFILES = {
    "x64": "chrome-win64-8078-1793181116-168d1f9712e3cd8f41b0065b66266772cb8067d5-d790dcb79631a25968d3346c27061e9d400a51af.profdata",
    "arm64": "chrome-win-arm64-main-1790618366-beb743be875e8f0817603de98b631df4925ff98c-0349482307527363d0a56e28bea6b0f50de564d2.profdata",
}
RECEIPT = ".chromix-windows156-fresh.json"


def checked_pins(repo: Path) -> dict:
    pins = load_pins(repo, "windows")
    if (pins["ChromiumVersion"], pins["UngoogledCommit"], pins["UngoogledWindowsCommit"]) != (VERSION, CORE, WINDOWS):
        raise ValueError("fresh Windows156 requires the exact official source pins")
    return pins


def identity(arch: str) -> dict:
    return dict(schema_version=1, chromium_version=VERSION, core=CORE, windows=WINDOWS,
                arch=arch, host="x64", build_profile="native", chrome_pgo_phase=2, out="out/Chromix")


def write_unchanged(path: Path, data: str) -> None:
    if not path.exists() or path.read_text(encoding="utf-8") != data:
        path.write_text(data, encoding="utf-8")


def initialize(repo: Path, work: Path, arch: str, *, resume: bool) -> None:
    checked_pins(repo)
    path = work / RECEIPT
    expected = identity(arch)
    if path.exists():
        if json.loads(path.read_text()) != expected:
            raise ValueError("fresh source identity mismatch; refusing cold fallback")
    elif resume or (work / "src").exists():
        raise ValueError("fresh source receipt missing; refusing cold fallback")
    else:
        work.mkdir(parents=True, exist_ok=True)
        write_unchanged(path, json.dumps(expected, sort_keys=True) + "\n")
    src = work / "src"
    if (src / ".chromix-upstream-restored.json").exists() or (src / "out/Default").exists():
        raise ValueError("fresh source cannot contain upstream cache output")
    if resume:
        marker = src / ".chromix-source-unpacked"
        if marker.exists() and marker.read_text().strip() != VERSION:
            raise ValueError("preserved source version mismatch; refusing fresh fallback")
        if (src / "out").is_dir() and not (src / ".chromix-source-ready").is_file():
            raise ValueError("preserved output lacks a ready source; refusing fresh fallback")
        if not src.is_dir():
            raise ValueError("preserved source is missing; refusing cold fallback")
        args = src / "out/Chromix/args.gn"
        if args.exists():
            text = args.read_text()
            for key, value in (("chrome_pgo_phase", "2"), ("target_cpu", json.dumps(arch))):
                matches = re.findall(r"(?m)^\s*" + key + r"\s*=\s*([^\r\n#]+)", text)
                if [item.strip() for item in matches] != [value]:
                    raise ValueError("preserved args mismatch: " + key)


def overlay(repo: Path, work: Path, arch: str) -> Path:
    checked_pins(repo)
    initialize(repo, work, arch, resume=True)
    src = (work / "src").resolve()
    path = work / "windows156-native.gn"
    library = src / "third_party/llvm-build/Release+Asserts/bin/libclang.dll"
    bindings = src / "third_party/llvm-build/Release+Asserts/bindings/python"
    text = ('# Windows156 fresh native build; never disable PGO for tarball sources.\n'
            'chrome_pgo_phase = 2\n'
            f'v8_metagen_libclang_so = {json.dumps(library.as_posix())}\n'
            f'v8_metagen_libclang_bindings_dir = {json.dumps(bindings.as_posix())}\n')
    write_unchanged(path, text)
    return path


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def profile(src: Path, arch: str) -> Path:
    target = "win64" if arch == "x64" else "win-arm64"
    name = PROFILES[arch]
    if (src / f"chrome/build/{target}.pgo.txt").read_text().strip() != name:
        raise ValueError("source PGO selector differs from the exact Chromium156 tag")
    directory = src / "chrome/build/pgo_profiles"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    proof = directory / (name + ".chromix-sha256")
    if destination.exists():
        if not proof.is_file() or destination.stat().st_size == 0 or proof.read_text().strip() != file_digest(destination):
            raise ValueError("preserved PGO profile has no matching download proof")
        return destination
    url = "https://storage.googleapis.com/chromium-optimization-profiles/pgo_profiles/" + name
    temporary = directory / (name + ".partial")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            if response.status != 200 or response.geturl() != url:
                raise ValueError("unexpected PGO download response")
            # GCS transparently expands stored gzip profiles using chunked transfer.
            length = response.headers.get("Content-Length")
            expected = int(length) if length is not None else None
            limit = 4 * 1024**3
            if expected is not None and not 0 < expected <= limit:
                raise ValueError("invalid PGO download size")
            size = 0
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > limit or (expected is not None and size > expected):
                    raise ValueError("PGO download exceeds declared length")
                output.write(block)
            if size == 0 or (expected is not None and size != expected):
                raise ValueError("incomplete PGO download")
        write_unchanged(proof, file_digest(temporary) + "\n")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def dependencies(src: Path) -> None:
    for relative in (
        "DEPS", "tools/gn/build/gen.py", "tools/gn/bootstrap/last_commit_position.h",
        "third_party/ninja/ninja.exe", "third_party/node/win/node.exe",
        "third_party/llvm-build/Release+Asserts/bin/clang-cl.exe",
        "third_party/llvm-build/Release+Asserts/bin/libclang.dll",
        "third_party/llvm-build/Release+Asserts/bin/llvm-profdata.exe",
        "third_party/dawn/tools/golang/windows-amd64/bin/go.exe",
        "third_party/devtools-frontend/src/third_party/esbuild/esbuild.exe",
        "third_party/typescript/windows-amd64/src/lib/tsc.exe",
        "third_party/typescript/windows-amd64/src/lib/lib.dom.d.ts",
    ):
        path = src / relative
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("missing fresh build dependency: " + relative)
    bootstrap = (src / "tools/gn/bootstrap/bootstrap.py").read_text()
    if "os.path.join(gn_build_dir, 'gn.exe')" not in bootstrap:
        raise ValueError("official Windows GN bootstrap patch is missing")
    declarations = (src / "third_party/typescript/windows-amd64/src/lib/lib.dom.d.ts").read_text()
    if "set innerHTML(html: string|TrustedHTML);" not in declarations:
        raise ValueError("official TypeScript 7.0.2 DOM patch is missing")


def midl(src: Path) -> None:
    # Official 8492884 overlay still compares shallowly and reuses zero-byte TLBs.
    # Reuse the reviewed transform without inventing an upstream cache identity.
    import repair_windows_midl as repair
    path = src / repair.SCRIPT
    original, info = repair._read(path)
    before = "501ad1740d960ab1f2afeb8278162b05163e3772edb6ea167554f67ef82ca99c"
    after = "f113aabeee53f6ba0fce6d364d45d294b094b656afc03e24029bf7503fb99b3a"
    if (src / ".chromix-domain-substituted").exists():
        before, after = repair.ORIGINAL_SHA256, repair.REPAIRED_SHA256
    repaired = repair._transform(original, before, after)
    if original == repaired:
        return
    out = src / "out/Chromix"
    if out.is_dir() and any(path.suffix in (".ninja", ".obj", ".tlb") for path in out.rglob("*")):
        raise ValueError("unrepaired fresh MIDL already has output; restore the latest valid tree")
    repair._replace(path, original, repaired, info)


def probe(src: Path, arch: str) -> None:
    dependencies(src)
    for relative, option, expected in (
        ("third_party/node/win/node.exe", "--version", "v24.12.0"),
        ("third_party/devtools-frontend/src/third_party/esbuild/esbuild.exe", "--version", "0.28.2"),
        ("third_party/typescript/windows-amd64/src/lib/tsc.exe", "--version", "Version 7.0.2"),
    ):
        result = subprocess.run([str(src / relative), option], check=True, capture_output=True, text=True, timeout=60)
        if result.stdout.strip() != expected:
            raise ValueError("unexpected host generator version: " + relative)
    pgo = profile(src, arch)
    subprocess.run([str(src / "third_party/llvm-build/Release+Asserts/bin/llvm-profdata.exe"),
                    "show", str(pgo)], check=True, timeout=120)


def latest_snapshot(client, repository: str, run_id: int, arch: str, sha: str) -> dict:
    """Select only a complete same-SHA checkpoint; never fall back to fresh."""
    workflow = f"build-win-{arch}-156-fresh"
    run = client.get(f"/actions/runs/{run_id}")
    if (run.get("id") != run_id or run.get("head_sha") != sha or run.get("status") != "completed"
            or run.get("event") != "workflow_dispatch"
            or run.get("path") != f".github/workflows/{workflow}.yml"
            or any(run.get(key, {}).get("full_name") != repository for key in ("repository", "head_repository"))):
        raise ValueError("fresh snapshot producer identity mismatch")
    artifacts = client.items(f"/actions/runs/{run_id}/artifacts", "artifacts")
    candidates = {}
    pattern = re.compile(rf"win-{arch}-156-tree-s([1-9]|1[0-2])-attempt-([1-9][0-9]*)-part([1-4])")
    for item in artifacts:
        match = pattern.fullmatch(item.get("name", ""))
        if match:
            stage, attempt, part = map(int, match.groups())
            candidates.setdefault((attempt, stage), []).append((part, item))
    for (attempt, stage), parts in sorted(candidates.items(), reverse=True):
        if attempt > run.get("run_attempt", 0):
            continue
        jobs = client.items(f"/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
        jobs = [job for job in jobs if job.get("name") == f"Windows156 {arch} stage {stage}"]
        if (len(jobs) != 1 or jobs[0].get("status") != "completed" or jobs[0].get("head_sha") != sha
                or jobs[0].get("run_id") != run_id or jobs[0].get("run_attempt") != attempt):
            continue
        steps = jobs[0].get("steps", [])
        def passed(name):
            matches = [step for step in steps if step.get("name") == name]
            return len(matches) == 1 and matches[0].get("conclusion") == "success"
        if not passed("Ensure build tree snapshot") or not all(passed(f"Upload tree part {n}") for n in range(1, 5)):
            continue
        parts.sort(key=lambda pair: pair[0])
        if [part for part, _ in parts] != list(range(1, len(parts) + 1)):
            continue
        selected = [item for _, item in parts]
        if any(item.get("expired") is not False or not isinstance(item.get("size_in_bytes"), int)
               or item["size_in_bytes"] <= 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("digest", ""))
               or item.get("workflow_run", {}).get("id") != run_id
               or item.get("workflow_run", {}).get("head_sha") != sha for item in selected):
            continue
        return dict(repository=repository, head_sha=sha, run_id=run_id, stage=stage, attempt=attempt,
                    arch=arch, workflow=workflow, artifacts=selected)
    raise ValueError("no valid preserved Windows156 snapshot; refusing fresh fallback")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--arch", choices=tuple(PROFILES), required=True)
    parser.add_argument("--phase", choices=("initialize", "resume", "overlay", "probe", "snapshot", "midl"), required=True)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.phase == "snapshot":
            from validate_windows_snapshot import Client
            if not args.run_id or args.run_id <= 0 or not args.report:
                raise ValueError("snapshot requires a positive donor run ID and report")
            repository, sha = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
            result = latest_snapshot(Client(repository, os.environ.get("GH_TOKEN", "")),
                                     repository, args.run_id, args.arch, sha)
            args.report.write_text(json.dumps(result, sort_keys=True) + "\n")
        elif args.phase in ("initialize", "resume"):
            initialize(args.repo, args.workdir, args.arch, resume=args.phase == "resume")
        elif args.phase == "midl":
            checked_pins(args.repo)
            midl(args.workdir / "src")
        elif args.phase == "overlay":
            overlay(args.repo, args.workdir, args.arch)
        else:
            checked_pins(args.repo)
            probe(args.workdir / "src", args.arch)
        return 0
    except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError) as error:
        print(f"Windows156 fresh preparation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
