#!/usr/bin/env python3
"""Read-only mac157 failed-build checkpoint admission, not compilation success."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fingerprint_acceptance
import patch_selection
import platform_pins
import restore_ninja
import restore_upstream_cache as restore
import verify_patch_stack

REPO = Path(__file__).resolve().parents[1]
READY = "fingerprint-diagnostics/mac157-native-ready.json"
INPUTS = ("src/.chromix-source-ready", "src/.chromix-upstream-restored.json",
          "fingerprint-diagnostics/source-final.json", "upstream-cache-ninja.json",
          "src/out/Default/args.gn", "src/out/Default/build.ninja")


def regular(root: Path, name: str) -> Path:
    path = restore.safe_path(root, Path(name))
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"checkpoint input is not a regular file: {name}")
    return path


def ready_identity(work: Path, arch: str, stage: int) -> dict:
    hashes = {}
    for name in INPUTS:
        with regular(work, name).open("rb") as stream:
            hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"schema_version": 1, "platform": "macos", "arch": arch, "stage": stage,
            "inputs": hashes}


def verify(work: Path, arch: str, stage: int, *, repo: Path = REPO) -> dict:
    work = restore.local_path(work)
    src = restore.safe_path(work, Path("src"))
    pins = platform_pins.load_pins(repo, "macos")
    if not pins["ChromiumVersion"].startswith("157."):
        raise ValueError("failed-build checkpoint admission is mac157-only")
    for path in src.glob(".chromix-*"):
        if "in-progress" in path.name or "blocked" in path.name:
            raise ValueError("checkpoint has unfinished preparation or blocked migration")
    if json.loads(regular(work, READY).read_text()) != ready_identity(work, arch, stage):
        raise ValueError("checkpoint differs from this stage's native preparation/GN identity")
    key = "|".join(("macos", arch, pins["ChromiumVersion"], pins["UngoogledCommit"],
                    pins["UngoogledMacOSCommit"], patch_selection.preparation_key(repo, "macos", "posix")))
    if regular(src, ".chromix-source-ready").read_text().strip() != key:
        raise ValueError("checkpoint preparation identity does not match current pins")
    if patch_selection.source_version(src) != pins["ChromiumVersion"]:
        raise ValueError("checkpoint source version does not match current pins")
    receipt = restore.verify_restored(work, "macos", arch, repo=repo)
    out = restore.safe_path(src, Path("out/Default"))
    restore.importer.validate_gn_target(restore.gn_assignments_last_wins(regular(out, "args.gn")), "macos", arch)
    with regular(out, ".ninja_log").open("rb") as stream:
        match = re.fullmatch(rb"# ninja log v([567])\r?\n", stream.readline(restore_ninja.HEADER_LIMIT + 1))
    if not match:
        raise ValueError("checkpoint Ninja log identity is unsupported")
    log_format = int(match[1])
    with regular(out, ".ninja_deps").open("rb") as stream:
        if stream.read(16) != b"# ninjadeps\n\x04\x00\x00\x00":
            raise ValueError("checkpoint Ninja deps identity is unsupported")
    host = restore_ninja.host_identity()
    report = json.loads(regular(work, restore_ninja.REPORT).read_text())
    if (host != ("macos", arch) or report.get("schema_version") != 1
            or report.get("status") != "selected" or report.get("platform") != "macos"
            or report.get("arch") != arch or report.get("host") != list(host)
            or report.get("log", {}).get("format") != log_format):
        raise ValueError("checkpoint Ninja selection/host identity does not match the tree")
    selected = report["selected"]
    ninja = Path(selected["path"])
    if (not ninja.is_absolute() or ninja.resolve() != ninja
            or arch not in restore_ninja.binary_architectures(ninja, "macos")):
        raise ValueError("checkpoint Ninja executable architecture/path mismatch")
    version = restore_ninja.probe_version(ninja, work)
    if version != selected["version"] or restore_ninja.version_format(version) != log_format:
        raise ValueError("checkpoint Ninja executable/version does not match its log")
    core = restore.safe_path(work, Path("tooling/ungoogled-chromium"))
    tooling = restore.safe_path(work, Path("tooling/ungoogled-chromium-macos"))
    for directory, commit in ((core, pins["UngoogledCommit"]), (tooling, pins["UngoogledMacOSCommit"])):
        actual = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        if actual != commit:
            raise ValueError("checkpoint tooling provenance does not match current pins")
    inputs, patches = verify_patch_stack.load_stack(repo, platform="macos", src=src, core=core)
    targets = {entry[0] for _, _, entries in patches for entry in entries}
    source_final = fingerprint_acceptance.check_source(
        regular(work, "fingerprint-diagnostics/source-final.json"), src, inputs, targets)
    source = verify_patch_stack.verify(src, repo, core=core, tooling=tooling, platform="macos")
    if source["status"] != "verified":
        raise ValueError("checkpoint full patch stack is not verified")
    return {"schema_version": 1, "status": "verified", "platform": "macos", "arch": arch,
            "stage": stage, "source": source, "source_final": source_final, "provenance": receipt,
            "ninja": selected, "qualification": "Resumable failed-build tree only; not compiled or runtime verified."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--stage", type=int, choices=range(1, 9), required=True)
    parser.add_argument("--record-ready", action="store_true")
    args = parser.parse_args()
    try:
        work = restore.local_path(args.workdir)
        if args.record_ready:
            path = restore.safe_path(work, Path(READY))
            path.write_text(json.dumps(ready_identity(work, args.arch, args.stage), sort_keys=True) + "\n")
        else:
            print(json.dumps(verify(work, args.arch, args.stage), sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, restore.LocalError,
            verify_patch_stack.arp.ApplyError, subprocess.SubprocessError) as error:
        print(f"mac157 checkpoint refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
