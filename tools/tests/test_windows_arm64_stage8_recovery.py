"""Tests for exact Windows ARM64 stage-8 recovery guards."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT / "tools"))
import verify_windows_arm64_stage8_recovery as recovery


def git(root: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, env=env).stdout


def commit(root: Path, message: str = "fixture") -> str:
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD").strip()


def put(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def snapshot_files():
    return {
        ".chromix-target-arch": "arm64\n",
        "src/.chromix-upstream-restored.json": json.dumps({
            "status": "restored", "platform": "windows", "arch": "arm64",
            "identity": {"platform": "windows", "arch": "arm64"},
        }),
        "src/.chromix-restored-patches.json": json.dumps({"patch_count": 216, "outputs": {"fixture": "hash"}}),
        "src/.chromix-source-ready": "154.0.8037.57|pinned|patches\n",
        "src/.chromix-source-unpacked": "154.0.8037.57\n",
        "src/out/Default/args.gn": 'target_cpu = "arm64"\nhost_cpu = "x64"\ntarget_os = "win"\n',
    }


def test_snapshot_markers_prove_arm64_and_reject_x64_or_cold_output():
    assert recovery.validate_snapshot_files({key: value.encode() for key, value in snapshot_files().items()})["status"] == "verified"
    bad = snapshot_files()
    bad[".chromix-target-arch"] = "x64\n"
    with pytest.raises(ValueError, match="architecture"):
        recovery.validate_snapshot_files({key: value.encode() for key, value in bad.items()})
    bad = snapshot_files()
    bad["src/out/Default/args.gn"] += 'target_cpu = "arm64"\n'
    with pytest.raises(ValueError, match="GN arguments"):
        recovery.validate_snapshot_files({key: value.encode() for key, value in bad.items()})


def test_snapshot_archive_rejects_only_exact_state_markers(monkeypatch, tmp_path):
    required = {"chromix/" + name: value.encode() for name, value in snapshot_files().items()}

    def run_with(listing):
        def fake_run(command, *args, **kwargs):
            if command[1] == "l":
                return subprocess.CompletedProcess(command, 0, stdout=listing.encode(), stderr=b"")
            member = command[3].removeprefix("chromix/")
            return subprocess.CompletedProcess(command, 0, stdout=required[command[3]], stderr=b"")
        return fake_run

    forbidden = "\n".join([
        *[f"Path = {name}" for name in required],
        "Path = chromix/src/chrome/browser/web_applications/ash/migrations",
        "Path = chromix/src/web/in-progress-example.txt",
        "Path = chromix/src/.chromix-restored-patches-in-progress",
    ])
    monkeypatch.setattr(recovery.subprocess, "run", run_with(forbidden))
    with pytest.raises(ValueError, match="migration or interrupted"):
        recovery.verify_snapshot_archive(tmp_path / "tree.7z.001", "7z")

    ordinary_names = "\n".join([
        "Path = chromix/src/chrome/browser/web_applications/ash/migrations",
        "Path = chromix/src/web/in-progress-example.txt",
        *[f"Path = {name}" for name in required],
    ])
    monkeypatch.setattr(recovery.subprocess, "run", run_with(ordinary_names))
    assert recovery.verify_snapshot_archive(tmp_path / "tree.7z.001", "7z")["status"] == "verified"


def test_source_proof_allows_only_recovery_files_and_requires_donor_ancestor(tmp_path):
    donor = tmp_path / "donor"
    target = tmp_path / "target"
    donor.mkdir()
    subprocess.run(["git", "init", "-q", str(donor)], check=True)
    put(donor, "build/args.windows.gn", "target_cpu = \"arm64\"\n")
    put(donor, "patches/series", "patches/0001.patch\n")
    put(donor, "CHROMIUM_WINDOWS_VERSION", "154.0.8037.57\n")
    donor_sha = commit(donor)
    old_donor_sha, old_allowed = recovery.DONOR_SHA, recovery.ALLOWED_TARGET_CHANGES
    recovery.DONOR_SHA = donor_sha
    recovery.ALLOWED_TARGET_CHANGES = frozenset({"tools/tests/test_windows_arm64_stage8_recovery.py"})
    try:
        subprocess.run(["git", "clone", "-q", str(donor), str(target)], check=True)
        put(target, "tools/tests/test_windows_arm64_stage8_recovery.py", "fixture\n")
        commit(target)
        result = recovery.verify_source_proof(donor, target, target_sha=git(target, "rev-parse", "HEAD").strip())
        assert result["source_inputs_changed"] == []
        put(target, "patches/series", "changed\n")
        commit(target)
        with pytest.raises(ValueError, match="outside the recovery allowlist|source, pin"):
            recovery.verify_source_proof(donor, target, target_sha=git(target, "rev-parse", "HEAD").strip())
    finally:
        recovery.DONOR_SHA = old_donor_sha
        recovery.ALLOWED_TARGET_CHANGES = old_allowed


def test_metadata_validator_requires_exact_artifacts():
    class Client:
        def __init__(self):
            self.calls = []

        def get(self, path):
            self.calls.append(path)
            return {
                "id": recovery.RUN_ID, "name": "warm profile=native jobs=auto cache=true upstream=36093095856",
                "path": ".github/workflows/build-win-arm64-github.yml", "event": "workflow_dispatch",
                "head_branch": recovery.DONOR_BRANCH, "head_sha": recovery.DONOR_SHA,
                "status": "completed", "conclusion": "failure", "run_attempt": recovery.ATTEMPT,
                "repository": {"full_name": recovery.REPOSITORY},
                "head_repository": {"full_name": recovery.REPOSITORY},
            }

        def items(self, path, key):
            if key == "jobs":
                return [{"id": recovery.DONOR_JOB_ID, "name": "stage 7 (resume compile)", "status": "completed",
                         "conclusion": "success", "head_sha": recovery.DONOR_SHA, "run_id": recovery.RUN_ID,
                         "run_attempt": recovery.ATTEMPT, "steps": [
                             {"name": "Run stage 7", "conclusion": "success"},
                             *[{"name": f"Upload tree part {part}", "conclusion": "success"} for part in (1, 2, 3, 4)],
                         ]}]
            return [{**item, "workflow_run": {"id": recovery.RUN_ID, "head_sha": recovery.DONOR_SHA,
                                                "head_branch": recovery.DONOR_BRANCH}} for item in recovery.ARTIFACTS]

    report = recovery.validate_metadata(Client())
    assert report["run_id"] == recovery.RUN_ID
    assert [item["id"] for item in report["artifacts"]] == [item["id"] for item in recovery.ARTIFACTS]
