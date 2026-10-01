#!/usr/bin/env python3
"""Verify the pinned Windows ARM64 stage-12 recovery inputs without running donor code."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Callable

from validate_posix_snapshot import Client

REPOSITORY = "xiaozhou26/Chromix"
WORKFLOW = "build-win-arm64-stage8-recovery"
RUN_NAME = "build-win-arm64-stage8-recovery"
RUN_ID = 36677869521
ATTEMPT = 1
DONOR_SHA = "91adf3cc3e2df651ad5af43f0fd72aba0f0f0e9a"
DONOR_BRANCH = "build/windows154-arm64-stage8-sdk-20260930"
DONOR_JOB_ID = 110184885191
DONOR_STAGE = 12
CHROMIUM_VERSION = "154.0.8037.57"
ARTIFACTS = (
    {"id": 11145248351, "name": "win-arm64-tree-s12-attempt-1-part1", "size_in_bytes": 9663676664,
     "expired": False, "digest": "sha256:d015246fc348cde6f7d2555d8fe09263dd5f32ca6646ac6e18240c5310b50d36"},
    {"id": 11144669229, "name": "win-arm64-tree-s12-attempt-1-part2", "size_in_bytes": 3030570342,
     "expired": False, "digest": "sha256:a3253bc52ae0251a90e771df000d3eebf2d2f672f7886d4346e88400f60afe12"},
)

# Parent-owned host fixes are permitted; all source and build-input changes remain forbidden.
ALLOWED_TARGET_CHANGES = frozenset({
    ".github/workflows/build-win-arm64-stage8-recovery.yml",
    "build/windows/assert-arm64-toolchain.ps1",
    "build/windows/ci-stage.ps1",
    "tools/tests/test_windows_arm64_build.py",
    "tools/tests/test_windows_arm64_stage8_recovery.py",
    "tools/tests/test_windows_arm64_stage8_recovery_workflow.py",
    "tools/verify_windows_arm64_stage8_recovery.py",
})
SOURCE_INPUT_PREFIXES = (
    "patches/", "build/args", "build/ungoogled-revisions.psd1", "build/upstream-cache.json",
    "CHROMIUM_", "UNGOOGLED_", ".github/workflows/build-win-arm64-github.yml",
)
SNAPSHOT_FILES = (
    ".chromix-target-arch",
    "src/.chromix-upstream-restored.json",
    "src/.chromix-restored-patches.json",
    "src/.chromix-source-ready",
    "src/.chromix-source-unpacked",
    "src/out/Default/args.gn",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _exact_artifacts(items) -> bool:
    return tuple({key: item.get(key) for key in ARTIFACTS[0]} for item in items) == ARTIFACTS


def validate_metadata(client: Client) -> dict:
    run = client.get(f"/actions/runs/{RUN_ID}")
    _require(run.get("id") == RUN_ID and run.get("name") == RUN_NAME,
             "stage12 run identity or profile mismatch")
    _require(run.get("path") == f".github/workflows/{WORKFLOW}.yml" and run.get("event") == "workflow_dispatch",
             "stage12 run workflow or event mismatch")
    _require(run.get("head_branch") == DONOR_BRANCH and run.get("head_sha") == DONOR_SHA,
             "stage12 run branch or SHA mismatch")
    _require(run.get("repository", {}).get("full_name") == REPOSITORY
             and run.get("head_repository", {}).get("full_name") == REPOSITORY,
             "stage12 run repository mismatch")
    _require(run.get("status") == "completed" and run.get("conclusion") == "failure"
             and run.get("run_attempt") == ATTEMPT, "stage12 run is not the completed attempt-1 failure")

    jobs = client.items(f"/actions/runs/{RUN_ID}/attempts/{ATTEMPT}/jobs", "jobs")
    matches = [job for job in jobs if job.get("id") == DONOR_JOB_ID]
    _require(len(matches) == 1, "exact stage12 job is missing or ambiguous")
    job = matches[0]
    _require(job.get("name") == "stage 12 (resume compile)" and job.get("status") == "completed"
             and job.get("conclusion") == "failure" and job.get("head_sha") == DONOR_SHA
             and job.get("run_id") == RUN_ID and job.get("run_attempt") == ATTEMPT,
             "stage12 job identity or completion mismatch")
    steps = {step.get("name"): step.get("conclusion") for step in job.get("steps", [])}
    _require(steps.get("Run stage 12") == "failure" and all(
        steps.get(f"Upload tree part {part}") == "success" for part in (1, 2)),
        "stage12 compile or checkpoint upload did not produce the exact checkpoint")
    _require(steps.get("Upload tree part 3") in ("success", "skipped")
             and steps.get("Upload tree part 4") in ("success", "skipped"),
             "stage12 checkpoint upload metadata is incomplete")

    listed = client.items(f"/actions/runs/{RUN_ID}/artifacts", "artifacts")
    prefix = "win-arm64-tree-s12-attempt-1-part"
    checkpoint = [item for item in listed if item.get("name", "").startswith(prefix)]
    _require(len(checkpoint) == len(ARTIFACTS) and _exact_artifacts(sorted(checkpoint, key=lambda item: item["name"])),
             "stage12 artifact IDs, names, sizes, or digests differ from the pinned set")
    selected = checkpoint
    for item in selected:
        origin = item.get("workflow_run", {})
        _require(origin.get("id") == RUN_ID and origin.get("head_sha") == DONOR_SHA
                 and origin.get("head_branch") == DONOR_BRANCH,
                 "stage12 artifact provenance mismatch")
    return {
        "repository": REPOSITORY, "workflow": WORKFLOW, "run_name": RUN_NAME,
        "head_branch": DONOR_BRANCH, "head_sha": DONOR_SHA, "run_id": RUN_ID,
        "attempt": ATTEMPT, "stage": DONOR_STAGE, "job_id": DONOR_JOB_ID,
        "pattern": "win-arm64-tree-s12-attempt-1-part*", "artifacts": list(ARTIFACTS),
    }


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout


def _tree(root: Path, revision: str) -> dict[str, tuple[str, str, str]]:
    raw = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", revision],
                         check=True, capture_output=True).stdout
    result = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, name = record.split(b"\t", 1)
        mode, kind, sha = header.split()
        result[name.decode("utf-8")] = (mode.decode(), kind.decode(), sha.decode())
    return result


def verify_source_proof(previous_repo: Path, repo: Path, *, target_sha: str) -> dict:
    previous_repo = previous_repo.resolve()
    repo = repo.resolve()
    _require(previous_repo != repo and previous_repo.is_dir() and repo.is_dir(),
             "donor and target checkouts must be separate directories")
    donor_head = _git(previous_repo, "rev-parse", "HEAD").strip()
    target_head = _git(repo, "rev-parse", "HEAD").strip()
    _require(donor_head == DONOR_SHA, "donor checkout is not the exact stage7 source SHA")
    _require(target_head == target_sha and re.fullmatch(r"[0-9a-f]{40}", target_sha),
             "target checkout is not the current recovery commit")
    donor_object = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{DONOR_SHA}^{{commit}}"])
    _require(donor_object.returncode == 0, "target checkout cannot resolve the exact donor commit")
    ancestry = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", DONOR_SHA, target_sha])
    _require(ancestry.returncode == 0, "recovery target is not descended from the exact donor commit")
    parents = _git(repo, "rev-list", "--parents", "-n", "1", target_sha).strip().split()
    _require(len(parents) == 2 and parents[1] == DONOR_SHA,
             "recovery target must be a direct child of the exact donor commit")

    old, new = _tree(repo, DONOR_SHA), _tree(repo, target_sha)
    changed = sorted(name for name in set(old) | set(new) if old.get(name) != new.get(name))
    unexpected = [name for name in changed if name not in ALLOWED_TARGET_CHANGES]
    _require(not unexpected, "donor source/build inputs changed outside the recovery allowlist: " + ", ".join(unexpected))
    source_changes = [name for name in changed if any(name.startswith(prefix) for prefix in SOURCE_INPUT_PREFIXES)]
    _require(not source_changes, "source, pin, patch, or GN profile inputs changed: " + ", ".join(source_changes))
    for name in changed:
        _require(new.get(name, (None, None, None))[1] == "blob", "recovery change is not a regular file: " + name)
    return {"status": "verified", "operation": "windows-arm64-stage8-source-proof", "previous_sha": DONOR_SHA,
            "target_sha": target_sha, "changed_files": changed, "source_inputs_changed": []}


def _read_marker(files: dict[str, bytes], name: str) -> str:
    _require(name in files, "snapshot marker is missing: " + name)
    try:
        return files[name].decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        raise ValueError("snapshot marker is not UTF-8: " + name) from None


def validate_snapshot_files(files: dict[str, bytes]) -> dict:
    _require(_read_marker(files, ".chromix-target-arch") == "arm64", "snapshot target architecture marker is not arm64")
    receipt = json.loads(_read_marker(files, "src/.chromix-upstream-restored.json"))
    _require(receipt.get("status") == "restored" and receipt.get("platform") == "windows"
             and receipt.get("arch") == "arm64" and receipt.get("identity", {}).get("arch") == "arm64",
             "snapshot upstream receipt is not verified for Windows ARM64")
    patches = json.loads(_read_marker(files, "src/.chromix-restored-patches.json"))
    identity = patches.get("identity") if isinstance(patches, dict) else None
    selection = identity.get("selection") if isinstance(identity, dict) else None
    series = identity.get("series") if isinstance(identity, dict) else None
    _require(
        isinstance(patches, dict)
        and patches.get("schema_version") == 1
        and isinstance(patches.get("identity_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", patches["identity_sha256"])
        and isinstance(patches.get("outputs"), dict)
        and bool(patches["outputs"])
        and isinstance(identity, dict)
        and identity.get("platform") == "windows"
        and isinstance(selection, dict)
        and selection.get("version") == CHROMIUM_VERSION
        and isinstance(series, dict)
        and isinstance(series.get("patches"), list)
        and bool(series["patches"]),
        "snapshot restored-patches receipt is missing or invalid",
    )
    ready = _read_marker(files, "src/.chromix-source-ready")
    _require(ready.startswith(CHROMIUM_VERSION + "|"), "snapshot source-ready receipt has the wrong Chromium version")
    _require(_read_marker(files, "src/.chromix-source-unpacked") == CHROMIUM_VERSION,
             "snapshot source-unpacked receipt has the wrong Chromium version")
    args = _read_marker(files, "src/out/Default/args.gn")
    _require(len(re.findall(r"(?m)^\s*target_cpu\s*=", args)) == 1
             and re.search(r'(?m)^\s*target_cpu\s*=\s*"arm64"\s*(?:#.*)?$', args)
             and re.search(r'(?m)^\s*target_os\s*=\s*"win"\s*(?:#.*)?$', args),
             "snapshot GN arguments do not identify Windows ARM64")
    return {"status": "verified", "architecture": "arm64", "platform": "windows",
            "source_ready": True, "restored_patches": len(series["patches"]),
            "restored_outputs": len(patches["outputs"])}


def _seven_zip(path: str | None) -> str:
    if path:
        return path
    command = shutil.which("7z.exe") or shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")
    if command:
        return command
    raise ValueError("7z is required to inspect the Windows snapshot")


def verify_snapshot_archive(archive: Path, seven_zip: str | None = None) -> dict:
    seven_zip = _seven_zip(seven_zip)
    listed = subprocess.run([seven_zip, "l", "-slt", str(archive)], check=True, capture_output=True).stdout.decode("utf-8", "replace")
    members = {line[7:].replace("\\", "/") for line in listed.splitlines() if line.startswith("Path = ")}
    required = {"chromix/" + name for name in SNAPSHOT_FILES}
    _require(required <= members, "snapshot archive is missing required ARM64 markers")
    _require(not any(name.startswith("chromix/src/out/Chromix/") for name in members),
             "snapshot archive contains a silent cold-source migration output")
    forbidden_state_markers = {
        "chromix/src/.chromix-windows-snapshot-migration.json",
        "chromix/src/.chromix-domain-substitution-in-progress",
        "chromix/src/.chromix-restored-patches-in-progress",
        "chromix/src/.chromix-layer-in-progress",
        "chromix/src/.chromix-patch-in-progress",
    }
    _require(not (forbidden_state_markers & members),
             "snapshot archive contains migration or interrupted-restore state")
    files = {}
    for name in SNAPSHOT_FILES:
        member = "chromix/" + name
        result = subprocess.run([seven_zip, "e", str(archive), member, "-so"], check=True, capture_output=True)
        files[name] = result.stdout
    result = validate_snapshot_files(files)
    result["archive"] = str(archive)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--metadata-report", type=Path)
    group.add_argument("--source-proof", action="store_true")
    group.add_argument("--snapshot", type=Path)
    parser.add_argument("--previous-repo", type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--target-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--seven-zip")
    args = parser.parse_args(argv)
    if args.metadata_report:
        result = validate_metadata(Client(REPOSITORY, os.environ.get("GH_TOKEN", "")))
        args.metadata_report.parent.mkdir(parents=True, exist_ok=True)
        args.metadata_report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    elif args.source_proof:
        if not args.previous_repo:
            parser.error("--source-proof requires --previous-repo")
        result = verify_source_proof(args.previous_repo, args.repo, target_sha=args.target_sha)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        result = verify_snapshot_archive(args.snapshot, args.seven_zip)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
