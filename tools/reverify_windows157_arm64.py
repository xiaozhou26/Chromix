#!/usr/bin/env python3
"""Verify one fixed Windows157 ARM64 package with its actual frozen source recipe."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

from reverify_windows_arm64_artifacts import (
    Metadata, dispatch_guard, extract_outer, git, matches, require, sha256,
    timestamp, verifier_identity, verify_file, write_new,
)

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "build/windows/windows157-arm64-native-artifacts.json"
MANIFEST_SHA256 = "931fe56ba5818e99fb5414f10c24178487861abb742da504b5126752935d681c"
ADMISSION = "compiled-continuation-28615c452bb34fe596b5bed8b3cbeb69.json"
BROWSER_SHA256 = "824a5b0b7e479959e65e4579680ea456b5df372e0334821ae185f7397239cae8"
VERSION = "157.0.8103.78"


def pins():
    from fingerprint_acceptance import read_json
    require(sha256(MANIFEST) == MANIFEST_SHA256, "fixed_manifest_changed")
    return read_json(MANIFEST)


def validate_metadata(client, fixed):
    run_pin = fixed["run"]
    path = f'/actions/runs/{run_pin["id"]}'
    runs = [client.get(path), client.get(path + "/attempts/1")]
    for run in runs:
        matches(run, run_pin, "original_run_mismatch")
        for key in ("repository", "head_repository"):
            matches(run.get(key), {"full_name": fixed["repository"], "id": fixed["repository_id"]},
                    "repository_mismatch")
    jobs = client.items(path + "/attempts/1/jobs", "jobs")
    selected_jobs = []
    for expected in fixed["jobs"]:
        found = [j for j in jobs if j.get("id") == expected["id"] or j.get("name") == expected["name"]]
        require(len(found) == 1, "missing_or_ambiguous_job")
        matches(found[0], {k: expected[k] for k in (
            "id", "run_id", "run_attempt", "workflow_name", "head_sha", "head_branch", "name",
            "status", "conclusion", "started_at", "completed_at", "steps")}, "original_job_mismatch")
        selected_jobs.append(found[0])
    artifacts = client.items(path + "/artifacts", "artifacts")
    selected = []
    uploads = ((0, "Upload final bundle"), (0, "Upload final source receipt"),
               (0, "Upload compiled migration evidence"), (1, "Upload native verification diagnostics"))
    for expected, (job_index, step_name) in zip(fixed["artifacts"], uploads, strict=True):
        found = [a for a in artifacts if a.get("id") == expected["id"] or a.get("name") == expected["name"]]
        require(len(found) == 1, "missing_or_ambiguous_artifact")
        matches(found[0], expected, "fixed_artifact_mismatch")
        job = selected_jobs[job_index]
        steps = [s for s in job["steps"] if s["name"] == step_name]
        require(len(steps) == 1 and steps[0]["conclusion"] == "success", "upload_not_successful")
        require(timestamp(steps[0]["started_at"]) <= timestamp(found[0]["created_at"])
                <= timestamp(found[0]["updated_at"]) <= timestamp(steps[0]["completed_at"]),
                "artifact_outside_producer_upload")
        selected.append(found[0])
    return {"runs": runs, "jobs": selected_jobs, "artifacts": selected}


def source_profile(recipe, fixed):
    from apply_restored_patches import transform_patch
    from patch_selection import read

    expected = fixed["source_profile"]
    require(recipe.resolve() != REPO and git(recipe, "rev-parse", "HEAD") == expected["commit"],
            "wrong_source_recipe_commit")
    require(not git(recipe, "status", "--porcelain", "--untracked-files=all"), "dirty_source_recipe")
    for key in ("manifest", "review_manifest"):
        entry = expected["selection"][key]
        require(sha256(recipe / entry["path"]) == entry["sha256"], "source_manifest_mismatch")
    # The authenticated recipe selector, not today's main selector, defines this build's identity.
    result = subprocess.run([sys.executable, "-B", str(recipe / "tools/patch_selection.py"),
                             "--repo", str(recipe), "--platform", "windows", "--json"],
                            check=True, capture_output=True, text=True, encoding="utf-8", timeout=60)
    inputs = json.loads(result.stdout)
    matches(inputs, {"selection": expected["selection"], "series_sha256": expected["series_sha256"]},
            "source_profile_mismatch")
    require(len(inputs["patches"]) == expected["patch_count"], "source_patch_count_mismatch")
    targets = set()
    for entry in inputs["patches"]:
        raw = read(recipe, entry["path"])
        require(sha256(recipe / entry["path"]) == entry["sha256"], "source_patch_changed")
        _, entries = transform_patch(raw, set(), [])
        targets.update(item[0] for item in entries)
    require(len(targets) == expected["target_count"], "source_target_count_mismatch")
    return inputs, sorted(targets)


def source_identity(source, admission, recipe, fixed):
    from fingerprint_acceptance import check_source, read_json

    for path in (source, admission):
        pin = fixed["files"][path.name]
        verify_file(path, pin["size"], pin["sha256"])
    inputs, targets = source_profile(recipe, fixed)
    receipt, proof = read_json(source), read_json(admission)
    matches(receipt, {"schema_version": 1, "status": "verified", "method": "reverse-forward-in-scratch",
                      "domain_substituted": True, "patch_count": 235}, "source_receipt_mismatch")
    matches(proof, {"schema_version": 1, "status": "migrated", "arch": "arm64", "version": VERSION,
                    "target_sha": fixed["run"]["head_sha"], "consumer_run_id": str(fixed["run"]["id"]),
                    "consumer_attempt": "1", "outputs_preserved": True}, "compiled_admission_mismatch")
    matches(proof.get("binding"), {"recipe_base": fixed["source_profile"]["commit"],
            "profile": "windows157-arm64-stage6-cache232-to235"}, "compiled_recipe_binding_mismatch")
    for key in ("identity", "outputs", "patch_count", "domain_substituted"):
        require(proof["source"].get(key) == receipt.get(key), "compiled_source_receipt_mismatch: " + key)
    checked = check_source(source, None, inputs, targets)
    return {**checked, "recipe_commit": fixed["source_profile"]["commit"],
            "build_commit": fixed["run"]["head_sha"], "build_run": fixed["run"]["id"],
            "build_attempt": 1, "browser_sha256": BROWSER_SHA256,
            "admission_sha256": sha256(admission), "patch_count": len(inputs["patches"]),
            "target_count": len(targets), "selection": inputs["selection"],
            "binding": "digest-pinned package/receipt/admission from successful stage12 upload",
            "qualification": "producer receipt only; not live-source replay or signed binary/source attestation"}


def original_failure(archive, fixed):
    with zipfile.ZipFile(archive) as stream:
        names = stream.namelist()
        require(len(names) == 4 and set(names) == {"fingerprint/acceptance.json", "native-smoke.json",
                                                 "headless.stdout", "headless.stderr"},
                "original_diagnostics_layout_mismatch")
        reports = {}
        for name in ("fingerprint/acceptance.json", "native-smoke.json"):
            pin = fixed["files"][name]
            info = stream.getinfo(name)
            require(info.file_size == pin["size"], "original_diagnostic_size_mismatch")
            raw = stream.read(name)
            require(hashlib.sha256(raw).hexdigest() == pin["sha256"], "original_diagnostic_hash_mismatch")
            reports[name] = json.loads(raw)
    failure = reports["fingerprint/acceptance.json"]
    matches(failure, {k: fixed["known_error"][k] for k in
                     ("status", "ci_gate_passed", "errors", "suites", "browser")}, "known_error_mismatch")
    require(failure["provenance"]["commit"] == fixed["run"]["head_sha"], "original_validator_mismatch")
    return reports


def prepare(directory, recipe, report):
    fixed = pins()
    report["verifier"] = verifier_identity()
    client = Metadata()
    report["phase"] = "metadata"
    report["original"] = validate_metadata(client, fixed)
    downloads = directory / "downloads"
    downloads.mkdir()
    diagnostics = directory / "diagnostics"
    report["downloads"] = []
    layouts = ({"chromix-win-arm64.zip": fixed["bundle"]["size"], "SHA256SUMS": 65536},
               {"source-verification.json": fixed["files"]["source-verification.json"]["size"]},
               {ADMISSION: fixed["files"][ADMISSION]["size"]})
    destinations = (downloads / "bundle", diagnostics / "source-receipt", diagnostics / "admission")
    deadline = time.monotonic() + 1200
    for index, artifact in enumerate(fixed["artifacts"]):
        report["phase"] = "download_" + artifact["name"]
        record = {**artifact, "attempts": []}
        report["downloads"].append(record)
        outer = client.download(fixed["repository"], artifact, downloads, deadline, record)
        verify_file(outer, artifact["size_in_bytes"], artifact["digest"].removeprefix("sha256:"))
        if index < 3:
            extract_outer(outer, destinations[index], layouts[index])
        else:
            report["original_reports"] = original_failure(outer, fixed)
        outer.unlink()
    verify_file(destinations[0] / "chromix-win-arm64.zip", fixed["bundle"]["size"], fixed["bundle"]["sha256"])
    report["phase"] = "source_identity"
    report["source"] = source_identity(destinations[1] / "source-verification.json",
                                       destinations[2] / ADMISSION, recipe, fixed)
    original = report["original_reports"]["fingerprint/acceptance.json"]["provenance"]["patch_inputs"]
    inputs, _ = source_profile(recipe, fixed)
    require(original["patches"] == inputs["patches"] and original["series_sha256"] == inputs["series_sha256"],
            "original_failure_not_manifest_only")
    report.update(status="prepared", phase="complete")


def bundle_identity(bundle):
    require(bundle.is_dir(), "missing_extracted_bundle")
    result = {}
    for path in sorted(bundle.rglob("*")):
        require(not path.is_symlink(), "bundle_symlink")
        if path.is_file():
            result[path.relative_to(bundle).as_posix()] = {"size": path.stat().st_size, "sha256": sha256(path)}
    require(result.get("chrome.exe", {}).get("sha256") == BROWSER_SHA256, "browser_hash_mismatch")
    return result


def acceptance(directory, recipe, report):
    import fingerprint_acceptance as gate
    from verify_windows_bundle import extract_archive, native_architecture

    fixed = pins()
    diagnostics = directory / "diagnostics"
    prepared = gate.read_json(diagnostics / "preparation.json")
    report["verifier"] = verifier_identity()
    require(prepared.get("status") == "prepared" and prepared.get("verifier") == report["verifier"],
            "preparation_not_verified")
    require(sys.platform == "win32" and native_architecture() == "arm64", "native_windows_arm64_required")
    source, admission = diagnostics / "source-receipt/source-verification.json", diagnostics / "admission" / ADMISSION
    report["source"] = source_identity(source, admission, recipe, fixed)
    require(report["source"] == prepared.get("source"), "source_changed_after_preparation")
    archive = directory / "downloads/bundle/chromix-win-arm64.zip"
    verify_file(archive, fixed["bundle"]["size"], fixed["bundle"]["sha256"])
    # Re-extract the authenticated package for byte-for-byte comparison with the tested tree.
    reference = directory / "authenticated-bundle"
    extract_archive(archive, directory / "downloads/bundle/SHA256SUMS", reference, "arm64")
    browser = directory / "smoke/chromix/chrome.exe"
    original_bundle = bundle_identity(reference / "chromix")
    require(bundle_identity(browser.parent) == original_bundle, "extracted_bundle_mismatch")
    smoke = gate.read_json(diagnostics / "native-smoke.json")
    matches(smoke, {"arch": "arm64"}, "native_smoke_arch_mismatch")
    matches(smoke.get("runtime"), {"status": "passed", "version": VERSION, "native_arch": "arm64",
                                  "exit_code": 0}, "native_smoke_not_passed")
    matches(smoke.get("static"), {"status": "passed"}, "static_smoke_not_passed")
    require(Path(smoke["bundle_dir"]).resolve() == browser.parent.resolve(), "smoke_browser_path_mismatch")
    require(smoke["static"]["pe_files"]["chrome.exe"]["sha256"] == BROWSER_SHA256, "smoke_hash_mismatch")
    current_provenance = gate.provenance

    def exact_source_provenance():
        identity = current_provenance()
        inputs, targets = source_profile(recipe, fixed)
        identity.update(patch_inputs=inputs, patch_targets=targets,
                        source_repository_commit=fixed["source_profile"]["commit"],
                        source_build_commit=fixed["run"]["head_sha"])
        return identity

    report["phase"] = "fingerprint_acceptance"
    gate.provenance = exact_source_provenance
    try:
        code = gate.main(["--browser", str(browser), "--expected-sha256", BROWSER_SHA256,
                          "--expected-version", VERSION, "--source-report", str(source),
                          "--output-dir", str(diagnostics / "fingerprint")])
    finally:
        gate.provenance = current_provenance
    observed = gate.read_json(diagnostics / "fingerprint/acceptance.json")
    report["acceptance"] = {k: observed.get(k) for k in
                            ("status", "ci_gate_passed", "full_acceptance", "errors", "gaps", "qualification")}
    require(bundle_identity(browser.parent) == original_bundle, "bundle_changed_during_acceptance")
    require(source_identity(source, admission, recipe, fixed) == report["source"], "source_changed_during_acceptance")
    require(code == 0 and observed.get("ci_gate_passed") is True and observed.get("control") is False
            and observed.get("errors") == [], "fingerprint_acceptance_failed")
    require([s.get("name") for s in observed.get("suites", [])] == [s[0] for s in gate.SUITES],
            "incomplete_acceptance_suites")
    require(verifier_identity() == report["verifier"], "verifier_changed_during_acceptance")
    report.update(status="reverified", phase="complete", ci_gate_passed=True,
                  browser_sha256=BROWSER_SHA256, version=VERSION)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "acceptance"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, required=True)
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "status": "failed", "phase": "guard", "ci_gate_passed": False,
              "original_run_id": 44816651803, "original_run_conclusion": "failure",
              "publication": "not_requested; wait for all six platforms"}
    output = None
    try:
        dispatch_guard(os.environ.get("GITHUB_EVENT_NAME"), os.environ.get("GITHUB_REPOSITORY"),
                       json.loads(os.environ.get("REVERIFY_INPUTS", "{}")))
        fixed = pins()
        report["known_error"] = fixed["known_error"]
        report["fixed_manifest_sha256"] = MANIFEST_SHA256
        require(args.directory.is_absolute() and not args.directory.is_symlink(), "absolute_directory_required")
        if args.operation == "prepare":
            args.directory.mkdir()
            (args.directory / "diagnostics").mkdir()
        output = args.directory / "diagnostics" / ("preparation.json" if args.operation == "prepare" else "reverified.json")
        require(not os.path.lexists(output), "report_already_exists")
        (prepare if args.operation == "prepare" else acceptance)(args.directory, args.recipe, report)
    except Exception as error:
        report.update(status="failed", ci_gate_passed=False, error_type=type(error).__name__, reason=str(error))
        print("Windows157 ARM64 re-verification failed: " + report["reason"], file=sys.stderr)
    finally:
        if output is not None and not os.path.lexists(output):
            write_new(output, report)
    return int(report["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
