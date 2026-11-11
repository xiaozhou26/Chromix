#!/usr/bin/env python3
"""Resume the exact compiled ARM64 stage3 without changing source or preparation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

try:
    from . import validate_macos156_arm64_stage8_checkpoint as arm
    from . import validate_macos156_arm64_stage1_checkpoint as previous
except ImportError:
    import validate_macos156_arm64_stage8_checkpoint as arm
    import validate_macos156_arm64_stage1_checkpoint as previous

RUN = 42703307545
JOB = 129654545984
ATTEMPT = 1
SHA = "240a988cc23ecfdbdf00763cf90b10243af1299e"
TREE = "eb93b96098daa9b18db1d9a57f06a9c275b44b12"
PREFIX = "chromix-mac156-arm64-stage1-resume-tree-s3-attempt-1-part"
ARTIFACTS = [
    dict(id=13748238839, name=PREFIX + "1", size_in_bytes=9663676674,
         digest="sha256:7ddd75897e0c7d5b3a6e3c3e49eec29f896ad84a13d4d606e27b0d64c8c58675", expired=False),
    dict(id=13748243740, name=PREFIX + "2", size_in_bytes=1018669270,
         digest="sha256:c0ebaa8b42c7f79cbf7ea933d7643e63d0749a01a0513f0f8086f928d31bedfb", expired=False),
]
VOLUME_SIZES = [9663676416, 1018669124]
LOG_HASH = "9747d907a8425dee8757d0d42c6000e4f4b056ac9f2e04ba7f5960153bed03c9"
LOG_SIZE = 568176
RECEIPT = "fingerprint-diagnostics/mac156-arm64-stage3-resume.json"
CONTINUATION = "mac156-arm64-stage3-continuation"
HANDOFF = "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s3-after-attempt-1.json"
FAILURE = "fingerprint-diagnostics/runtime-s3-1794299594-14569/acceptance.json"
DONOR_RECEIPTS = {
    "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s1-after-attempt-1-source.json":
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
    "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s1-after-attempt-1.json":
        "df81d936d001cd23ad585f1c76e313fd9e34f382e28b08f454c0a101d1a25b06",
    "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s1-before-attempt-1-source.json":
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
    "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s1-before-attempt-1.json":
        "9c9aa2965415f9606375a58cea5f3bcad1bb432caf933b49899ed70ebceaecbd",
    "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s2-after-attempt-1-source.json":
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
    "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s2-after-attempt-1.json":
        "c430306d54c4badb7030a8cccd905c9ac7319bd31fcf8adb01360d2540b1d566",
    "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s2-before-attempt-1-source.json":
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
    "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s2-before-attempt-1.json":
        "325d513d25672a2ea96329dc596dcc25478ce5946c50ec2b2c28c39b23df081e",
    "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s3-after-attempt-1-source.json":
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
    "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s3-after-attempt-1.json":
        "4064e1e5308bf52a9cd5224c60c4de9f5a5f83d3ab189e499274c8d3efc395ff",
    "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s3-before-attempt-1-source.json":
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
    "fingerprint-diagnostics/mac156-arm64-stage1-continuation-s3-before-attempt-1.json":
        "d2def72b606e233e2f90cdc4f66d68c94cf68b2b5fb4f43328374b5f06f82215",
    "fingerprint-diagnostics/mac156-arm64-stage1-resume.json":
        "c47c120f1b07672ccd14cad05b9e4adb704238cca936112adb5991d4bd17a683",
    "fingerprint-diagnostics/mac156-arm64-stage1-source-admission.json":
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
    "fingerprint-diagnostics/mac156-arm64-stage8-continuation-s1-after-attempt-1-source.json":
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
    "fingerprint-diagnostics/mac156-arm64-stage8-continuation-s1-after-attempt-1.json":
        "e369c5b0af2b0d40cf22378d823f9c1c60a4a7519f4ae0af42b1016be1f0dd51",
    "fingerprint-diagnostics/mac156-arm64-stage8-continuation-s1-before-attempt-1-source.json":
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
    "fingerprint-diagnostics/mac156-arm64-stage8-continuation-s1-before-attempt-1.json":
        "c931f5509b7842c83acf6ca56315c61dac9686a509b0848d7ea4551541b39997",
    "fingerprint-diagnostics/mac156-arm64-stage8-resume.json":
        "ebc9701d85a30b29a961f40d7622c015316505d53cd9fa21f3fd9a7a026f2269",
    "fingerprint-diagnostics/mac156-arm64-stage8-source-admission.json":
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
    "fingerprint-diagnostics/source-final.json":
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
    "fingerprint-diagnostics/runtime-s3-1794299594-14569/acceptance.json":
        "ab153c8ea1785a2e46c650fbbc976ce1426f220dd8be5cb383f77c83b4294192",
    "fingerprint-diagnostics/runtime-s3-1794299594-14569/media_policy.json":
        "c4d99181febc16157656e3000cb470eeaa4b24def7320f80e9dab6b3fe22bea9",
}
require = arm.require


def expected_manifest():
    return dict(repository=arm.REPOSITORY, platform="macos", arch="arm64",
                workflow="build-mac156-arm64-stage1-resume", run_id=RUN, attempt=ATTEMPT,
                stage=3, head_sha=SHA, job_id=JOB, pattern=PREFIX + "*", artifacts=ARTIFACTS)


def producer_log():
    return subprocess.run(["gh", "api", "--allow-escape-sequences",
                           f"repos/{arm.REPOSITORY}/actions/jobs/{JOB}/logs"],
                          check=True, capture_output=True, timeout=120).stdout


def validate_metadata(client, repository, artifact_ids, *, log_loader=producer_log):
    require(repository == arm.REPOSITORY and artifact_ids == [a["id"] for a in ARTIFACTS],
            "exact latest ARM64 stage3 repository/artifacts required")
    run = client.get(f"/actions/runs/{RUN}")
    expected = dict(id=RUN, head_sha=SHA, head_branch="main", event="workflow_dispatch",
                    path=".github/workflows/build-mac156-arm64-stage1-resume.yml",
                    run_attempt=ATTEMPT, status="completed", conclusion="failure")
    require(type(run.get("run_attempt")) is int and all(run.get(k) == v for k, v in expected.items())
            and run.get("repository", {}).get("full_name") == repository
            and run.get("head_repository", {}).get("full_name") == repository,
            "exact failed continuation run required")
    job = client.get(f"/actions/jobs/{JOB}")
    expected = dict(id=JOB, run_id=RUN, head_sha=SHA, head_branch="main", run_attempt=ATTEMPT,
                    status="completed", conclusion="failure", labels=["macos-26"],
                    name="macOS156 ARM64 (read-only stage1 admission, logical 10..17) / "
                         "macos-arm64 stage 3 (resume compile)")
    require(type(job.get("run_attempt")) is int and all(job.get(k) == v for k, v in expected.items()),
            "exact failed compiled stage3 producer job required")
    steps = {s["name"]: s for s in job.get("steps", [])}
    required_steps = {"Run stage 3": "failure", "Verify handoff snapshot": "success",
                      "Download tree from previous stage": "success",
                      "Preserve latest macOS compiled checkpoint": "success",
                      "Upload failed macOS runtime bundle": "success",
                      **{f"Upload tree part {i}": "success" for i in range(1, 5)}}
    for name, conclusion in required_steps.items():
        require((steps.get(name, {}).get("status"), steps.get(name, {}).get("conclusion")) ==
                ("completed", conclusion), "incomplete producer step: " + name)
    raw = log_loader()
    require(len(raw) == LOG_SIZE and arm.sha(raw) == LOG_HASH, "exact stage3 producer log required")
    for text in ("macOS bundle verified:", "16 Mach-O files, only arm64",
                 "fingerprint regression gate failed;",
                 "packed 10682345540 bytes as 2 volumes (streaming)",
                 *(f"Final size is {a['size_in_bytes']} bytes. Artifact ID is {a['id']}" for a in ARTIFACTS),
                 *(f".snapshot-stage-3/p{i}/. No artifacts will be uploaded." for i in (3, 4))):
        require(text.encode() in raw, "stage3 snapshot/upload evidence missing")
    artifacts = sorted((a for a in client.items(f"/actions/runs/{RUN}/artifacts", "artifacts")
                        if a.get("name", "").startswith(PREFIX)), key=lambda a: a["name"])
    require(len(artifacts) == len(ARTIFACTS), "complete exact stage3 artifact set required")
    for actual, expected in zip(artifacts, ARTIFACTS):
        require(all(actual.get(k) == v for k, v in expected.items()) and actual.get("expired") is False,
                "stage3 artifact ID/name/size/digest/expiry mismatch")
        origin = actual.get("workflow_run", {})
        require(all(origin.get(k) == v for k, v in dict(id=RUN, head_sha=SHA, head_branch="main").items()),
                "stage3 artifact origin mismatch")
    return expected_manifest()


def download_identity(origin, report):
    manifest = json.loads(origin.read_bytes())
    require(manifest == expected_manifest(), "exact latest stage3 manifest required")
    data = json.loads(report.read_bytes())
    require((data.get("status"), data.get("publication"), data.get("phase")) ==
            ("success", "published", "complete"), "verified published download required")
    require(all(data.get(k) == manifest[k] for k in ("repository", "head_sha", "run_id")),
            "stage3 download origin mismatch")
    artifacts, volumes = data.get("artifacts", []), data.get("volumes", [])
    require(len(artifacts) == len(volumes) == 2, "both latest stage3 parts required")
    for index, (actual, expected, volume, size) in enumerate(zip(artifacts, ARTIFACTS, volumes, VOLUME_SIZES), 1):
        require(all(actual.get(k) == v for k, v in expected.items()), "stage3 downloaded artifact mismatch")
        attempts = actual.get("attempts", [])
        require(attempts and attempts[-1].get("status") == "success"
                and attempts[-1].get("size_in_bytes") == expected["size_in_bytes"]
                and "sha256:" + attempts[-1].get("sha256", "") == expected["digest"],
                "stage3 verified ZIP digest/size required")
        require(volume.get("name") == f"tree.tar.zst.{index:03d}"
                and volume.get("artifact_id") == expected["id"] and volume.get("size_in_bytes") == size
                and re.fullmatch(r"[0-9a-f]{64}", volume.get("sha256", "")),
                "stage3 packed volume mismatch")
    require(data.get("total_size_in_bytes") == sum(VOLUME_SIZES), "stage3 packed total mismatch")
    return dict(manifest=manifest, volumes=volumes, producer_log_sha256=LOG_HASH, producer_log_size=LOG_SIZE)


def continuation_identity():
    return dict(status="verified", operation="unchanged-mac156-arm64-stage3-continuation",
                repository=arm.REPOSITORY, run_id=RUN, attempt=ATTEMPT, stage=3, job_id=JOB,
                head_sha=SHA, tree=TREE, artifacts=ARTIFACTS, version=arm.VERSION, platform="macos", arch="arm64",
                preparation_key=arm.KEY, source_identity=arm.SOURCE_IDENTITY, producer_log_sha256=LOG_HASH,
                completed_logical_stage=12,
                continuation_stages="1..8 after compiled stage3 (logical 13..20)", changed_files=[])


def donor_identity(work):
    for name, digest in DONOR_RECEIPTS.items():
        require(arm.sha(arm.read(work, name)) == digest, "exact stage3 receipt required: " + name)
    previous.donor_identity(work)
    receipt = json.loads(arm.read(work, previous.RECEIPT))
    require(all(receipt.get(k) == v for k, v in previous.continuation_identity().items())
            and receipt.get("continuation_run") == str(RUN)
            and receipt.get("provenance", {}).get("manifest") == previous.expected_manifest(),
            "original stage1 admission provenance changed")
    for stage in range(1, 4):
        pattern = re.compile(rf"mac156-arm64-stage1-continuation-s{stage}-after-attempt-([1-9][0-9]*)\.json")
        require([p.name for p in (work / "fingerprint-diagnostics").iterdir() if pattern.fullmatch(p.name)] ==
                [f"mac156-arm64-stage1-continuation-s{stage}-after-attempt-1.json"],
                "exact single donor handoff required")
    handoff = json.loads(arm.read(work, HANDOFF))
    expected = dict(previous.continuation_identity(), continuation_run=str(RUN), continuation_attempt="1",
                    continuation_stage=3, logical_stage=12, phase="after")
    require(all(handoff.get(k) == v for k, v in expected.items()), "exact stage3 handoff provenance required")
    failure = json.loads(arm.read(work, FAILURE))
    require(failure.get("status") == "failed" and failure.get("ci_gate_passed") is False
            and failure.get("full_acceptance") is False and failure.get("errors") == [
                "media_policy: suite reported failure",
                "media_policy: native: hevc: supported recorder failed to record",
                "media_policy: suite exited nonzero"], "original native HEVC failure must remain valid")
    return receipt, handoff


# New tool revisions require explicit reviewed byte identities; source recipes never use this map.
TOOL_BASELINES = {
    "tools/fingerprint_backend_probe.js":
        "26121b7dec6071a7a19ab32ce3251da559d4bf842c739d87407b6d03e79e40c7",
    "tools/fingerprint_media_policy_audit.py":
        "67a5c6162c604d5cb0487732f1b1c75fbe3de2fb68cda177e98ab4fcdd84beea",
    "tools/fingerprint_backend_audit.py":
        "510cb28b1732605c526bf6351b3fe50fd91b326b5ff46443120c03462c099edb",
    "tools/fingerprint_acceptance.py":
        "11a5bbd334f5ed520018f621310c3b9c7ae4ca5927fe901429333aaefe517939",
}
APPROVED_TOOL_REVISIONS = {}


def samebrowser_recipes(repo, donor=None, receipt=None):
    require(arm.migration.source_ready_key(repo, "macos", "arm64") == arm.KEY,
            "same-browser preparation key changed; migration/cold fallback forbidden")
    result = {}
    for name in dict.fromkeys((*arm.RECIPE_FILES, *TOOL_BASELINES)):
        digest = arm.sha(arm.recipe_bytes(name, arm.read(repo, name)))
        if name in TOOL_BASELINES:
            require(digest in (TOOL_BASELINES[name], *APPROVED_TOOL_REVISIONS.get(name, ())),
                    "unreviewed same-browser tool revision: " + name)
        result[name] = digest
    if donor is not None:
        original = arm.recipes(donor)
        require(original == receipt.get("recipes"), "original admitted recipes changed")
        for name, digest in original.items():
            require(name in TOOL_BASELINES or result[name] == digest, "source/build recipe changed: " + name)
        for name, digest in TOOL_BASELINES.items():
            require(arm.sha(arm.read(donor, name)) == digest, "donor tool identity changed: " + name)
    return result


def compiled_browser(repo, work):
    browser = work / "src/out/Default/Chromium.app/Contents/MacOS/Chromium"
    identity = json.loads(arm.read(work, FAILURE))["browser"]
    require(browser.is_file() and os.access(browser, os.X_OK)
            and browser.stat().st_size == identity["size"]
            and arm.sha(browser.read_bytes()) == identity["sha256"], "compiled donor browser changed")
    subprocess.run([sys.executable, str(repo / "tools/verify_macos_bundle.py"),
                    "--app", str(work / "src/out/Default/Chromium.app"), "--arch", "arm64"], check=True)


def admit(work, donor, repo, origin, download_report):
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", "")), "continuation run required")
    provenance = download_identity(origin, download_report)
    require(arm.migration._donor_git(donor, repo, "rev-parse", "HEAD").decode().strip() == SHA
            and arm.migration._donor_git(donor, repo, "rev-parse", "HEAD^{tree}").decode().strip() == TREE
            and not arm.migration._donor_git(donor, repo, "status", "--porcelain", "--untracked-files=all"),
            "clean exact stage3 producer checkout required")
    old, handoff = donor_identity(work)
    recipes = samebrowser_recipes(repo, donor, old)
    require(arm.arp.patch_selection.select(repo, "macos") == arm.arp.patch_selection.select(donor, "macos"),
            "stage3 selected patches changed")
    state = arm.source_state(repo, work)
    require(state == handoff.get("state") and state["graph"] == old["state"]["graph"],
            "latest stage3 Ninja state/graph required")
    inventory = arm.output_inventory(work / "src/out/Default")
    arm.native_state(repo, work)
    compiled_browser(repo, work)
    arm.migration.restore.verify_restored(work, "macos", "arm64", repo=repo)
    arm.migration._verify_tooling(work, repo, (work, donor, repo), "macos")
    output = work / RECEIPT
    require(not output.exists(), "stage3 recovery already admitted")
    arm.live_source(repo, work, output.with_name("mac156-arm64-stage3-source-admission.json"))
    require(arm.source_state(repo, work) == state and arm.output_inventory(work / "src/out/Default") == inventory,
            "stage3 admission modified existing build state")
    tool_changes = {name: dict(before=digest, after=recipes[name])
                    for name, digest in TOOL_BASELINES.items() if recipes[name] != digest}
    result = dict(continuation_identity(), provenance=provenance, recipes=recipes, state=state,
                  continuation_run=os.environ["GITHUB_RUN_ID"], donor_receipts=DONOR_RECEIPTS,
                  tool_only_changes=tool_changes)
    output.write_bytes(arm.arp._json(result))
    return result


def continuation(repo, work, stage, phase):
    donor_identity(work)
    receipt_name, prefix, base = RECEIPT, CONTINUATION, 12
    require(phase in ("before", "after"), "invalid continuation phase")
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", "")), "continuation run ID required")
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ATTEMPT", "")), "continuation attempt required")
    require(1 <= stage <= 8, "continuation stage must be 1..8")
    require(os.environ.get("CHROMIX_BUILD_PROFILE") == "fast"
            and os.environ.get("CHROMIX_USE_UPSTREAM_CACHE") == "1",
            "continuation requires fast profile and mandatory restored cache")
    receipt = json.loads(arm.read(work, receipt_name))
    expected = continuation_identity()
    manifest = expected_manifest()
    require(all(receipt.get(key) == value for key, value in expected.items()),
            "missing or changed arm64-stage3 continuation provenance")
    require(receipt.get("continuation_run") == os.environ.get("GITHUB_RUN_ID")
            and receipt.get("provenance", {}).get("manifest") == manifest,
            "continuation run/donor isolation mismatch")
    require(samebrowser_recipes(repo) == receipt.get("recipes"), "admitted build recipes changed")
    state = arm.source_state(repo, work)
    require(state["graph"] == receipt["state"]["graph"],
            "GN graph changed; regeneration forbidden")
    if phase == "before":
        arm.native_state(repo, work)
    if stage == 1 and phase == "before":
        require(state == receipt.get("state"), "initial Ninja state changed before continuation")
    if phase == "before" and stage > 1:
        arm.previous_handoff(work, prefix, stage, base, expected, state)
    output = work / "fingerprint-diagnostics" / (
        f"{prefix}-s{stage}-{phase}-attempt-{os.environ['GITHUB_RUN_ATTEMPT']}.json")
    arm.live_source(repo, work, output.with_name(output.stem + "-source.json"))
    result = dict(expected, continuation_stage=stage, logical_stage=base + stage, phase=phase,
                  continuation_run=os.environ.get("GITHUB_RUN_ID"),
                  continuation_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"), state=state)
    output.write_bytes(arm.arp._json(result))
    return result


def compile_tree(repo, work, stage):
    donor_identity(work)
    receipt = json.loads(arm.read(work, RECEIPT))
    require(samebrowser_recipes(repo) == receipt.get("recipes"), "admitted same-browser recipes changed")
    return arm.compile_tree(repo, work, stage, receipt_name=RECEIPT, prefix=CONTINUATION,
                            base=12, expected=continuation_identity())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("metadata", "admit", "check", "compile"), required=True)
    for flag in ("repo", "workdir", "previous-repo", "origin", "download-report", "report"):
        parser.add_argument("--" + flag, type=Path)
    parser.add_argument("--stage", type=int)
    parser.add_argument("--phase", choices=("before", "after"))
    args = parser.parse_args(argv)
    try:
        if args.mode == "metadata":
            require(args.report is not None, "metadata report required")
            repository = os.environ.get("GITHUB_REPOSITORY", arm.REPOSITORY)
            result = validate_metadata(arm.snapshot.Client(repository, os.environ.get("GH_TOKEN", "")),
                                       repository, [a["id"] for a in ARTIFACTS])
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_bytes(arm.arp._json(result))
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(f"head_sha={SHA}\npattern={result['pattern']}\n")
        else:
            require(args.workdir is not None, "workdir required")
            work = args.workdir.resolve()
            repo = (args.repo or Path(__file__).resolve().parents[1]).resolve()
            if args.mode == "admit":
                require(all((args.previous_repo, args.origin, args.download_report)), "admission provenance required")
                admit(work, args.previous_repo.resolve(), repo, args.origin, args.download_report)
            elif args.mode == "check":
                require(args.stage is not None and args.phase is not None, "stage and phase required")
                continuation(repo, work, args.stage, args.phase)
            else:
                return compile_tree(repo, work, args.stage)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"mac156 ARM64 stage3 recovery rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
