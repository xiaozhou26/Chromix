"""Fixed-artifact/source-profile fixtures are not native fingerprint evidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fingerprint_acceptance as gate
import reverify_windows157_arm64 as recovery


class MetadataFixture:
    def __init__(self):
        self.fixed = recovery.pins()
        repository = {"full_name": self.fixed["repository"], "id": self.fixed["repository_id"]}
        self.run = {**self.fixed["run"], "repository": repository, "head_repository": repository}
        self.attempt = deepcopy(self.run)
        self.jobs = deepcopy(self.fixed["jobs"])
        self.artifacts = deepcopy(self.fixed["artifacts"])

    def get(self, path):
        return self.attempt if path.endswith("/attempts/1") else self.run

    def items(self, path, key):
        return self.jobs if key == "jobs" else self.artifacts


def test_exact_metadata_and_real_failure_details():
    client = MetadataFixture()
    result = recovery.validate_metadata(client, client.fixed)
    assert result["runs"][0]["id"] == 44816651803
    assert result["jobs"][0]["id"] == 137038008909
    assert result["jobs"][1]["id"] == 137060205519
    assert result["jobs"][1]["conclusion"] == "failure"
    assert [a["id"] for a in result["artifacts"]] == [14735195326, 14734394816, 14734633750, 14734828422]
    error = client.fixed["known_error"]
    assert error["errors"] == ["ValueError: source receipt was produced for a different patch stack"]
    assert error["suites"] == [] and error["ci_gate_passed"] is False
    assert error["producer_manifest"] != error["validator_manifest"]
    assert client.fixed["source_profile"]["patch_count"] == 235


@pytest.mark.parametrize("target,key,value", [
    ("run", "run_attempt", 2), ("attempt", "run_attempt", True), ("run", "conclusion", "success"),
    ("run", "head_sha", "0" * 40), ("attempt", "head_branch", "other"), ("run", "event", "push"),
    ("run", "workflow_id", 1), ("run", "path", "other.yml"),
    ("run", "repository", {"full_name": "fork/Chromix", "id": 1342691290}),
])
def test_run_metadata_mismatch(target, key, value):
    client = MetadataFixture()
    getattr(client, target)[key] = value
    with pytest.raises(ValueError):
        recovery.validate_metadata(client, client.fixed)


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("key,value", [("id", 1), ("name", "wrong"), ("size_in_bytes", 1),
    ("digest", "sha256:" + "0" * 64), ("expired", True), ("expired", 0),
    ("created_at", "2026-11-21T00:00:00Z"), ("workflow_run", {})])
def test_artifact_identity_is_fully_pinned(index, key, value):
    client = MetadataFixture()
    client.artifacts[index][key] = value
    with pytest.raises(ValueError):
        recovery.validate_metadata(client, client.fixed)


@pytest.mark.parametrize("mutation", ["missing_artifact", "duplicate_name", "missing_job", "duplicate_job",
                                     "failed_upload", "changed_native_error", "wrong_build_head"])
def test_producer_chain_fails_closed(mutation):
    client = MetadataFixture()
    if mutation == "missing_artifact":
        client.artifacts.pop()
    elif mutation == "duplicate_name":
        client.artifacts.append({**client.artifacts[0], "id": 999})
    elif mutation == "missing_job":
        client.jobs.pop()
    elif mutation == "duplicate_job":
        client.jobs.append(deepcopy(client.jobs[0]))
    elif mutation == "failed_upload":
        next(s for s in client.jobs[0]["steps"] if s["name"] == "Upload final bundle")["conclusion"] = "failure"
    elif mutation == "changed_native_error":
        next(s for s in client.jobs[1]["steps"] if s["name"] == "Verify native ARM64 fingerprint behavior")["conclusion"] = "success"
    else:
        client.jobs[0]["head_sha"] = "0" * 40
    with pytest.raises(ValueError):
        recovery.validate_metadata(client, client.fixed)


@pytest.fixture
def source_fixture(tmp_path, monkeypatch):
    fixed = recovery.pins()
    inputs = {"selection": deepcopy(fixed["source_profile"]["selection"]),
              "series_sha256": fixed["source_profile"]["series_sha256"],
              "patches": [{"path": f"patches/{i}.patch", "sha256": "a" * 64} for i in range(235)]}
    outputs = {f"source/{i}.cc": "b" * 64 for i in range(172)}
    receipt = {"schema_version": 1, "status": "verified", "method": "reverse-forward-in-scratch",
               "domain_substituted": True, "patch_count": 235,
               "identity": {"series": {"sha256": inputs["series_sha256"], "patches": inputs["patches"]},
                            "selection": inputs["selection"]}, "outputs": outputs}
    admission = {"schema_version": 1, "status": "migrated", "arch": "arm64", "version": recovery.VERSION,
                 "target_sha": fixed["run"]["head_sha"], "consumer_run_id": str(fixed["run"]["id"]),
                 "consumer_attempt": "1", "outputs_preserved": True,
                 "binding": {"recipe_base": fixed["source_profile"]["commit"],
                             "profile": "windows157-arm64-stage6-cache232-to235"}, "source": deepcopy(receipt)}
    admission["source"]["method"] = "compiled-private-roundtrip"
    source, proof = tmp_path / "source-verification.json", tmp_path / recovery.ADMISSION

    def save():
        for path, value in ((source, receipt), (proof, admission)):
            path.write_text(json.dumps(value))
            fixed["files"][path.name] = {"size": path.stat().st_size, "sha256": recovery.sha256(path)}

    save()
    monkeypatch.setattr(recovery, "source_profile", lambda *_: (deepcopy(inputs), sorted(outputs)))
    return fixed, inputs, receipt, admission, source, proof, save


def test_correct_exact_source_profile_preserves_receipt(source_fixture, tmp_path):
    fixed, inputs, receipt, admission, source, proof, _ = source_fixture
    before = source.read_bytes()
    result = recovery.source_identity(source, proof, tmp_path, fixed)
    assert result["patch_count"] == 235 and result["target_count"] == 172
    assert result["recipe_commit"] != result["build_commit"]
    assert result["browser_sha256"] == recovery.BROWSER_SHA256
    assert result["check"] == "producer-receipt-only"
    assert source.read_bytes() == before


def test_run_head_manifest_mismatch_still_rejected(source_fixture):
    fixed, inputs, receipt, admission, source, proof, _ = source_fixture
    wrong = deepcopy(inputs)
    wrong["selection"]["manifest"] = fixed["known_error"]["validator_manifest"]
    wrong["selection"]["review_manifest"] = fixed["known_error"]["validator_review_manifest"]
    assert wrong["patches"] == inputs["patches"]
    with pytest.raises(ValueError, match="different patch stack"):
        gate.check_source(source, None, wrong, receipt["outputs"])


@pytest.mark.parametrize("mutation", ["232", "patch_hash", "patch_order", "manifest", "outputs", "status",
                                     "build_head", "recipe_head", "browser_receipt_bytes"])
def test_no_forged_or_unbound_source(source_fixture, tmp_path, mutation):
    fixed, inputs, receipt, admission, source, proof, save = source_fixture
    if mutation == "232":
        receipt["patch_count"] = 232
        receipt["identity"]["series"]["patches"] = receipt["identity"]["series"]["patches"][:232]
    elif mutation == "patch_hash":
        receipt["identity"]["series"]["patches"][0]["sha256"] = "f" * 64
    elif mutation == "patch_order":
        receipt["identity"]["series"]["patches"].reverse()
    elif mutation == "manifest":
        receipt["identity"]["selection"]["manifest"] = fixed["known_error"]["validator_manifest"]
    elif mutation == "outputs":
        receipt["outputs"]["unexpected.cc"] = "c" * 64
    elif mutation == "status":
        receipt["status"] = "failed"
    elif mutation == "build_head":
        admission["target_sha"] = "0" * 40
    elif mutation == "recipe_head":
        admission["binding"]["recipe_base"] = fixed["run"]["head_sha"]
    else:
        source.write_bytes(source.read_bytes() + b" ")
    if mutation != "browser_receipt_bytes":
        save()
    with pytest.raises(ValueError):
        recovery.source_identity(source, proof, tmp_path, fixed)


def test_wrong_recipe_commit_never_runs_selector(tmp_path, monkeypatch):
    fixed = recovery.pins()
    monkeypatch.setattr(recovery, "git", lambda *_: fixed["run"]["head_sha"])
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("wrong recipe executed"))
    with pytest.raises(ValueError, match="wrong_source_recipe_commit"):
        recovery.source_profile(tmp_path, fixed)


def test_real_historical_profile_when_git_object_available(tmp_path):
    fixed = recovery.pins()
    commit = fixed["source_profile"]["commit"]
    probe = subprocess.run(["git", "cat-file", "-e", commit], cwd=recovery.REPO, capture_output=True)
    if probe.returncode:
        pytest.skip("historical recipe object absent in shallow checkout; no network fetch")
    clone = tmp_path / "recipe"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", str(recovery.REPO), str(clone)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "--detach", commit], check=True, capture_output=True)
    inputs, targets = recovery.source_profile(clone, fixed)
    assert len(inputs["patches"]) == 235 and len(targets) == 172
    assert inputs["selection"] == fixed["source_profile"]["selection"]
    (clone / inputs["patches"][0]["path"]).write_bytes(b"changed patch")
    with pytest.raises(ValueError, match="dirty_source_recipe"):
        recovery.source_profile(clone, fixed)


def test_bundle_identity_covers_dll_and_all_files(tmp_path, monkeypatch):
    browser = tmp_path / "chrome.exe"
    browser.write_bytes(b"fixture")
    monkeypatch.setattr(recovery, "BROWSER_SHA256", recovery.sha256(browser))
    (tmp_path / "chrome.dll").write_bytes(b"original")
    before = recovery.bundle_identity(tmp_path)
    (tmp_path / "chrome.dll").write_bytes(b"changed")
    assert recovery.bundle_identity(tmp_path) != before
    browser.write_bytes(b"different browser")
    with pytest.raises(ValueError, match="browser_hash_mismatch"):
        recovery.bundle_identity(tmp_path)


def test_workflow_cannot_build_publish_or_select_latest():
    path = recovery.REPO / ".github/workflows/reverify-win157-arm64.yml"
    text = path.read_text()
    workflow = yaml.safe_load(text)
    assert workflow["on"] == {"workflow_dispatch": {}}
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    job = workflow["jobs"]["reverify"]
    assert job["runs-on"] == "windows-11-arm"
    assert "continue-on-error" not in text
    assert "--control" not in text and "release-browser" not in text and "ci-stage" not in text
    checkouts = [s for s in job["steps"] if s.get("uses") == "actions/checkout@v4"]
    assert checkouts[1]["with"]["ref"] == recovery.pins()["source_profile"]["commit"]
    assert all(s["with"]["persist-credentials"] is False for s in checkouts)
    uploads = [s for s in job["steps"] if s.get("uses") == "actions/upload-artifact@v4"]
    assert len(uploads) == 1 and uploads[0]["with"]["path"].endswith("/diagnostics/")
    assert recovery.BROWSER_SHA256 in recovery.pins()["known_error"]["browser"]["sha256"]


@pytest.mark.parametrize("mutation", [None, "gate_failure", "missing_suite", "changed_bundle", "changed_source"])
def test_acceptance_keeps_all_suites_and_unchanged_checks(tmp_path, monkeypatch, mutation):
    import verify_windows_bundle as bundle
    fixed = recovery.pins()
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    identity = {"sha": "verifier", "run_id": "new-run"}
    source = {"sha256": "receipt", "browser_sha256": recovery.BROWSER_SHA256}
    (diagnostics / "preparation.json").write_text(json.dumps({"status": "prepared", "verifier": identity,
                                                           "source": source}))
    browser = tmp_path / "smoke/chromix/chrome.exe"
    smoke = {"arch": "arm64", "bundle_dir": str(browser.parent),
             "runtime": {"status": "passed", "version": recovery.VERSION, "native_arch": "arm64", "exit_code": 0},
             "static": {"status": "passed", "pe_files": {"chrome.exe": {"sha256": recovery.BROWSER_SHA256}}}}
    (diagnostics / "native-smoke.json").write_text(json.dumps(smoke))
    monkeypatch.setattr(recovery, "verifier_identity", lambda: identity)
    monkeypatch.setattr(recovery, "verify_file", lambda *_: None)
    monkeypatch.setattr(bundle, "native_architecture", lambda: "arm64")
    monkeypatch.setattr(bundle, "extract_archive", lambda *_: None)
    monkeypatch.setattr(recovery.sys, "platform", "win32")
    state = {"finished": False, "profile_reads": 0}
    monkeypatch.setattr(recovery, "source_identity", lambda *_: (
        {**source, "sha256": "changed"} if state["finished"] and mutation == "changed_source" else source))
    monkeypatch.setattr(recovery, "bundle_identity", lambda *_: (
        {"chrome.dll": "changed"} if state["finished"] and mutation == "changed_bundle" else {"chrome.dll": "original"}))
    current = {"patch_inputs": {"wrong": "latest-main"}, "patch_targets": [], "runner_files": {"probe": "hash"},
               "commit": "current-verifier", "packages": {"driver": "version"}}
    original_provenance = lambda: deepcopy(current)
    monkeypatch.setattr(gate, "provenance", original_provenance)

    def profile(*_):
        state["profile_reads"] += 1
        return {"exact": "235-source-profile"}, ["file.cc"]

    monkeypatch.setattr(recovery, "source_profile", profile)

    def run(argv):
        assert "--control" not in argv and "--source-report" in argv
        assert argv[argv.index("--expected-sha256") + 1] == recovery.BROWSER_SHA256
        first, last = gate.provenance(), gate.provenance()
        assert first == last
        assert first["patch_inputs"] == {"exact": "235-source-profile"}
        assert first["patch_targets"] == ["file.cc"]
        assert first["runner_files"] == current["runner_files"]
        assert first["packages"] == current["packages"] and first["commit"] == current["commit"]
        assert first["source_repository_commit"] == fixed["source_profile"]["commit"]
        assert first["source_build_commit"] == fixed["run"]["head_sha"]
        suites = [{"name": s[0]} for s in gate.SUITES]
        if mutation == "missing_suite":
            suites.pop()
        failed = mutation == "gate_failure"
        output = diagnostics / "fingerprint"
        output.mkdir()
        (output / "acceptance.json").write_text(json.dumps({"status": "failed" if failed else "passed",
            "ci_gate_passed": not failed, "control": False, "errors": ["probe failed"] if failed else [],
            "suites": suites, "gaps": [], "full_acceptance": False}))
        state["finished"] = True
        return int(failed)

    monkeypatch.setattr(gate, "main", run)
    report = {}
    if mutation:
        with pytest.raises(ValueError):
            recovery.acceptance(tmp_path, tmp_path / "recipe", report)
        assert report.get("ci_gate_passed") is not True
    else:
        recovery.acceptance(tmp_path, tmp_path / "recipe", report)
        assert report["ci_gate_passed"] is True and report["status"] == "reverified"
    assert state["profile_reads"] == 2
    assert gate.provenance is original_provenance
