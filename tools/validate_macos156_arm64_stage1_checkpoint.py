#!/usr/bin/env python3
"""Resume the exact successful ARM64 continuation stage1, preserving its original admission."""
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
except ImportError:
    import validate_macos156_arm64_stage8_checkpoint as arm

RUN = 42652225672
JOB = 129281322673
ATTEMPT = 1
SHA = "9ea3a9272fcadc189dbaa312f3055ba641af8e88"
TREE = "f55eddd8a1d4ea4b0c2a95b6ed7ac7ab249bf755"
PREFIX = "chromix-mac156-arm64-stage8-resume-tree-s1-attempt-1-part"
ARTIFACTS = [
    dict(id=13706643929, name=PREFIX + "1", size_in_bytes=9663676674,
         digest="sha256:cfa9f53ec44bb36a10f87c3ea20fd9188ebfd4f7e6214371598942b64ea06220", expired=False),
    dict(id=13707496932, name=PREFIX + "2", size_in_bytes=90201676,
         digest="sha256:c3ed0fb825bc4ab569ccbcf7d38b920b7269a42e85ee7d9bc3b1abfa73e41480", expired=False),
]
VOLUME_SIZES = [9663676416, 90201530]
LOG_HASH = "e2f8019ec9e37b0121d561e7e0ffb3c5ba8bb0d3173ae9bff3f752a8cf8ce133"
LOG_SIZE = 819347
RECEIPT = "fingerprint-diagnostics/mac156-arm64-stage1-resume.json"
CONTINUATION = "mac156-arm64-stage1-continuation"
DONOR_RECEIPTS = {
    arm.RECEIPT: "ebc9701d85a30b29a961f40d7622c015316505d53cd9fa21f3fd9a7a026f2269",
    "fingerprint-diagnostics/mac156-arm64-stage8-continuation-s1-after-attempt-1.json":
        "e369c5b0af2b0d40cf22378d823f9c1c60a4a7519f4ae0af42b1016be1f0dd51",
    "fingerprint-diagnostics/mac156-arm64-stage8-continuation-s1-after-attempt-1-source.json":
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
}
require = arm.require


def expected_manifest():
    return dict(repository=arm.REPOSITORY, platform="macos", arch="arm64",
                workflow="build-mac156-arm64-stage8-resume", run_id=RUN, attempt=ATTEMPT,
                stage=1, head_sha=SHA, job_id=JOB, pattern=PREFIX + "*", artifacts=ARTIFACTS)


def producer_log():
    return subprocess.run(["gh", "api", "--allow-escape-sequences",
                           f"repos/{arm.REPOSITORY}/actions/jobs/{JOB}/logs"],
                          check=True, capture_output=True, timeout=120).stdout


def validate_metadata(client, repository, artifact_ids, *, log_loader=producer_log):
    require(repository == arm.REPOSITORY and artifact_ids == [a["id"] for a in ARTIFACTS],
            "exact latest ARM64 stage1 repository/artifacts required")
    run = client.get(f"/actions/runs/{RUN}")
    expected = dict(id=RUN, head_sha=SHA, head_branch="main", event="workflow_dispatch",
                    path=".github/workflows/build-mac156-arm64-stage8-resume.yml",
                    run_attempt=ATTEMPT, status="completed", conclusion="failure")
    require(type(run.get("run_attempt")) is int and all(run.get(k) == v for k, v in expected.items())
            and run.get("repository", {}).get("full_name") == repository
            and run.get("head_repository", {}).get("full_name") == repository,
            "exact failed continuation run required")
    job = client.get(f"/actions/jobs/{JOB}")
    expected = dict(id=JOB, run_id=RUN, head_sha=SHA, head_branch="main", run_attempt=ATTEMPT,
                    status="completed", conclusion="success", labels=["macos-26"],
                    name="macOS156 ARM64 (read-only stage8 admission, logical 9..16) / "
                         "macos-arm64 stage 1 (prepare + first compile)")
    require(type(job.get("run_attempt")) is int and all(job.get(k) == v for k, v in expected.items()),
            "exact successful stage1 producer job required")
    steps = {s["name"]: s for s in job.get("steps", [])}
    for name in ("Run stage 1", "Verify handoff snapshot", "Restore and migrate selected POSIX checkpoint",
                 *(f"Upload tree part {i}" for i in range(1, 5))):
        require((steps.get(name, {}).get("status"), steps.get(name, {}).get("conclusion")) ==
                ("completed", "success"), "incomplete producer step: " + name)
    raw = log_loader()
    require(len(raw) == LOG_SIZE and arm.sha(raw) == LOG_HASH, "exact stage1 producer log required")
    for text in ("stage 1: deadline reached after 198m; snapshotting unfinished work",
                 "packed 9753877946 bytes as 2 volumes (streaming)",
                 *(f"Final size is {a['size_in_bytes']} bytes. Artifact ID is {a['id']}" for a in ARTIFACTS),
                 *(f".snapshot-stage-1/p{i}/. No artifacts will be uploaded." for i in (3, 4))):
        require(text.encode() in raw, "stage1 snapshot/upload evidence missing")
    artifacts = sorted((a for a in client.items(f"/actions/runs/{RUN}/artifacts", "artifacts")
                        if a.get("name", "").startswith(PREFIX)), key=lambda a: a["name"])
    require(len(artifacts) == len(ARTIFACTS), "complete exact stage1 artifact set required")
    for actual, expected in zip(artifacts, ARTIFACTS):
        require(all(actual.get(k) == v for k, v in expected.items()) and actual.get("expired") is False,
                "stage1 artifact ID/name/size/digest/expiry mismatch")
        origin = actual.get("workflow_run", {})
        require(all(origin.get(k) == v for k, v in dict(id=RUN, head_sha=SHA, head_branch="main").items()),
                "stage1 artifact origin mismatch")
    return expected_manifest()


def download_identity(origin, report):
    manifest = json.loads(origin.read_bytes())
    require(manifest == expected_manifest(), "exact latest stage1 manifest required")
    data = json.loads(report.read_bytes())
    require((data.get("status"), data.get("publication"), data.get("phase")) ==
            ("success", "published", "complete"), "verified published download required")
    require(all(data.get(k) == manifest[k] for k in ("repository", "head_sha", "run_id")),
            "stage1 download origin mismatch")
    artifacts, volumes = data.get("artifacts", []), data.get("volumes", [])
    require(len(artifacts) == len(volumes) == 2, "both latest stage1 parts required")
    for index, (actual, expected, volume, size) in enumerate(zip(artifacts, ARTIFACTS, volumes, VOLUME_SIZES), 1):
        require(all(actual.get(k) == v for k, v in expected.items()), "stage1 downloaded artifact mismatch")
        attempts = actual.get("attempts", [])
        require(attempts and attempts[-1].get("status") == "success"
                and attempts[-1].get("size_in_bytes") == expected["size_in_bytes"]
                and "sha256:" + attempts[-1].get("sha256", "") == expected["digest"],
                "stage1 verified ZIP digest/size required")
        require(volume.get("name") == f"tree.tar.zst.{index:03d}"
                and volume.get("artifact_id") == expected["id"] and volume.get("size_in_bytes") == size
                and re.fullmatch(r"[0-9a-f]{64}", volume.get("sha256", "")),
                "stage1 packed volume mismatch")
    require(data.get("total_size_in_bytes") == sum(VOLUME_SIZES), "stage1 packed total mismatch")
    return dict(manifest=manifest, volumes=volumes, producer_log_sha256=LOG_HASH, producer_log_size=LOG_SIZE)


def continuation_identity():
    return dict(status="verified", operation="unchanged-mac156-arm64-stage1-continuation",
                repository=arm.REPOSITORY, run_id=RUN, attempt=ATTEMPT, stage=1, job_id=JOB,
                head_sha=SHA, tree=TREE, artifacts=ARTIFACTS, version=arm.VERSION, platform="macos", arch="arm64",
                preparation_key=arm.KEY, source_identity=arm.SOURCE_IDENTITY, producer_log_sha256=LOG_HASH,
                continuation_stages="1..8 after continuation stage1 (logical 10..17)", changed_files=[])


def donor_identity(work):
    for name, digest in DONOR_RECEIPTS.items():
        require(arm.sha(arm.read(work, name)) == digest, "exact stage1 receipt required: " + name)
    receipt = json.loads(arm.read(work, arm.RECEIPT))
    require(all(receipt.get(k) == v for k, v in arm.continuation_identity().items())
            and receipt.get("continuation_run") == str(RUN)
            and receipt.get("provenance", {}).get("manifest") == arm.expected_manifest(),
            "original stage8 admission provenance changed")
    handoff = json.loads(arm.read(work, "fingerprint-diagnostics/"
                                  "mac156-arm64-stage8-continuation-s1-after-attempt-1.json"))
    expected = dict(arm.continuation_identity(), continuation_run=str(RUN), continuation_attempt="1",
                    continuation_stage=1, logical_stage=9, phase="after")
    require(all(handoff.get(k) == v for k, v in expected.items()), "exact stage1 handoff provenance required")
    return receipt, handoff


def admit(work, donor, repo, origin, download_report):
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", "")), "continuation run required")
    provenance = download_identity(origin, download_report)
    require(arm.migration._donor_git(donor, repo, "rev-parse", "HEAD").decode().strip() == SHA
            and arm.migration._donor_git(donor, repo, "rev-parse", "HEAD^{tree}").decode().strip() == TREE
            and not arm.migration._donor_git(donor, repo, "status", "--porcelain", "--untracked-files=all"),
            "clean exact stage1 producer checkout required")
    old, handoff = donor_identity(work)
    recipes = arm.recipes(repo)
    require(recipes == old.get("recipes") == arm.recipes(donor), "stage1 admitted recipes changed")
    require(arm.arp.patch_selection.select(repo, "macos") == arm.arp.patch_selection.select(donor, "macos"),
            "stage1 selected patches changed")
    state = arm.source_state(repo, work)
    require(state == handoff.get("state") and state["graph"] == old["state"]["graph"],
            "latest stage1 Ninja state/graph required")
    inventory = arm.output_inventory(work / "src/out/Default")
    arm.native_state(repo, work)
    arm.migration.restore.verify_restored(work, "macos", "arm64", repo=repo)
    arm.migration._verify_tooling(work, repo, (work, donor, repo), "macos")
    output = work / RECEIPT
    require(not output.exists(), "stage1 recovery already admitted")
    arm.live_source(repo, work, output.with_name("mac156-arm64-stage1-source-admission.json"))
    require(arm.source_state(repo, work) == state and arm.output_inventory(work / "src/out/Default") == inventory,
            "stage1 admission modified existing build state")
    result = dict(continuation_identity(), provenance=provenance, recipes=recipes, state=state,
                  continuation_run=os.environ["GITHUB_RUN_ID"], donor_receipts=DONOR_RECEIPTS)
    output.write_bytes(arm.arp._json(result))
    return result


def continuation(repo, work, stage, phase):
    donor_identity(work)
    return arm.continuation(repo, work, stage, phase, receipt_name=RECEIPT, prefix=CONTINUATION,
                            expected=continuation_identity(), manifest=expected_manifest(), base=9)


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
                donor_identity(work)
                return arm.compile_tree(repo, work, args.stage, receipt_name=RECEIPT, prefix=CONTINUATION,
                                        base=9, expected=continuation_identity())
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"mac156 ARM64 stage1 recovery rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
