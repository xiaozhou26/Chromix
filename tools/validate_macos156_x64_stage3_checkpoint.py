#!/usr/bin/env python3
"""Admit the exact Intel stage3 xcrun-timeout checkpoint without changing build inputs."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

try:
    from . import validate_macos156_x64_stage2_checkpoint as previous
except ImportError:
    import validate_macos156_x64_stage2_checkpoint as previous

intel = previous.intel

RUN = 42735558594
JOB = 129753489228
ATTEMPT = 1
SHA = "f6da88f521078f8577c65b4b8d5426ab36a4606d"
TREE = "d7a048f17267d9c233ef7720fabeca47812f5ddc"
PREFIX = "chromix-mac156-x64-stage2-resume-tree-s3-attempt-1-part"
ARTIFACTS = [
    dict(id=13757361205, name=PREFIX + "1", size_in_bytes=8738426126,
         digest="sha256:a9a261d11c512e4529e9a1a159c55295de9bbf3c3c6601c64751560aaf87d759", expired=False),
]
VOLUME_SIZES = [8738425868]
LOG_HASH = "30213179d75d2639d4e589b7344bb1c3ec4ce25e0394ec911d29a2a4acc8b6fd"
LOG_SIZE = 136694
RECEIPT = "fingerprint-diagnostics/mac156-x64-stage3-resume.json"
CONTINUATION = "mac156-x64-stage3-continuation"
HANDOFF = "fingerprint-diagnostics/mac156-x64-stage2-continuation-s2-after-attempt-1.json"
DONOR_RECEIPTS = {
    **previous.DONOR_RECEIPTS,
    'fingerprint-diagnostics/mac156-x64-stage2-continuation-s1-after-attempt-1-source.json':
        'afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421',
    'fingerprint-diagnostics/mac156-x64-stage2-continuation-s1-after-attempt-1.json':
        '2d0310ff4b3588bf9e9059fb27249aa45a9ab3999375921639f4f926467f0118',
    'fingerprint-diagnostics/mac156-x64-stage2-continuation-s1-before-attempt-1-source.json':
        'afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421',
    'fingerprint-diagnostics/mac156-x64-stage2-continuation-s1-before-attempt-1.json':
        'b3b6dfb1a6d71214aabad28e43559236423602690c5310c2895c3c88ba4bcbf2',
    'fingerprint-diagnostics/mac156-x64-stage2-continuation-s2-after-attempt-1-source.json':
        'afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421',
    'fingerprint-diagnostics/mac156-x64-stage2-continuation-s2-after-attempt-1.json':
        'fa46c1bc1848e38dd38cfd91a18c6ba921d2aa7928015f0009096f8f1c2c85fa',
    'fingerprint-diagnostics/mac156-x64-stage2-continuation-s2-before-attempt-1-source.json':
        'afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421',
    'fingerprint-diagnostics/mac156-x64-stage2-continuation-s2-before-attempt-1.json':
        '5698e7053de73a59da7670b7481943219d986138513feec20ea747d3b7264d0b',
    'fingerprint-diagnostics/mac156-x64-stage2-resume.json':
        'd0ffd1fb614a48c2fd09eed4c1b0bcda3b74848ce69086717a4a589a14f5788e',
    'fingerprint-diagnostics/mac156-x64-stage2-source-admission.json':
        'afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421',
}
require = intel.require


def expected_manifest():
    return dict(repository=intel.REPOSITORY, platform="macos", arch="x64",
                workflow="build-mac156-x64-stage2-resume", run_id=RUN, attempt=ATTEMPT,
                stage=3, head_sha=SHA, job_id=JOB, pattern=PREFIX + "*", artifacts=ARTIFACTS)


def producer_log():
    return subprocess.run(["gh", "api", "--allow-escape-sequences",
                           f"repos/{intel.REPOSITORY}/actions/jobs/{JOB}/logs"],
                          check=True, capture_output=True, timeout=120).stdout


def validate_metadata(client, repository, artifact_ids, *, log_loader=producer_log):
    require(repository == intel.REPOSITORY and artifact_ids == [a["id"] for a in ARTIFACTS],
            "exact latest Intel stage3 repository/artifacts required")
    run = client.get(f"/actions/runs/{RUN}")
    expected = dict(id=RUN, head_sha=SHA, head_branch="main", event="workflow_dispatch",
                    path=".github/workflows/build-mac156-x64-stage2-resume.yml",
                    run_attempt=ATTEMPT, status="completed", conclusion="failure")
    require(type(run.get("run_attempt")) is int and all(run.get(k) == v for k, v in expected.items())
            and run.get("repository", {}).get("full_name") == repository
            and run.get("head_repository", {}).get("full_name") == repository,
            "exact failed continuation run required")
    job = client.get(f"/actions/jobs/{JOB}")
    expected = dict(id=JOB, run_id=RUN, head_sha=SHA, head_branch="main", run_attempt=ATTEMPT,
                    status="completed", conclusion="failure", labels=["macos-26-intel"],
                    name="macOS156 Intel (read-only stage2 admission, logical 10..17) / "
                         "macos-x64 stage 3 (resume compile)")
    require(type(job.get("run_attempt")) is int and all(job.get(k) == v for k, v in expected.items()),
            "exact failed stage3 producer job required")
    steps = {s["name"]: s for s in job.get("steps", [])}
    required_steps = {"Run stage 3": "failure", "Verify handoff snapshot": "success",
                      "Download tree from previous stage": "success",
                      "Select compatible Xcode": "success",
                      "Verify complete Mac SDK contents": "success",
                      "Preserve latest macOS compiled checkpoint": "success",
                      "Upload failed macOS runtime bundle": "skipped",
                      **{f"Upload tree part {i}": "success" for i in range(1, 5)}}
    for name, conclusion in required_steps.items():
        require((steps.get(name, {}).get("status"), steps.get(name, {}).get("conclusion")) ==
                ("completed", conclusion), "incomplete producer step: " + name)
    raw = log_loader()
    require(len(raw) == LOG_SIZE and intel.sha(raw) == LOG_HASH, "exact stage3 producer log required")
    for text in ("mac156 Intel stage2 recovery rejected: Command "
                 "'('xcrun', '--sdk', 'macosx', '--show-sdk-build-version')' timed out after 30 seconds",
                 "packed 8738425868 bytes as 1 volumes (streaming)",
                 *(f"Final size is {a['size_in_bytes']} bytes. Artifact ID is {a['id']}" for a in ARTIFACTS),
                 *(f".snapshot-stage-3/p{i}/. No artifacts will be uploaded." for i in (2, 3, 4))):
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
    require(len(artifacts) == len(volumes) == 1, "single latest stage3 part required")
    for index, (actual, expected, volume, size) in enumerate(zip(artifacts, ARTIFACTS, volumes, VOLUME_SIZES), 1):
        require(all(actual.get(k) == v for k, v in expected.items()), "stage3 downloaded artifact mismatch")
        attempts = actual.get("attempts", [])
        require(attempts and attempts[-1].get("status") == "success"
                and attempts[-1].get("size_in_bytes") == expected["size_in_bytes"]
                and "sha256:" + attempts[-1].get("sha256", "") == expected["digest"],
                "stage3 verified ZIP digest/size required")
        # The digest-pinned ZIP authenticates the extracted volume; its hash is retained in provenance.
        require(volume.get("name") == f"tree.tar.zst.{index:03d}"
                and volume.get("artifact_id") == expected["id"] and volume.get("size_in_bytes") == size
                and re.fullmatch(r"[0-9a-f]{64}", volume.get("sha256", "")),
                "stage3 packed volume mismatch")
    require(data.get("total_size_in_bytes") == sum(VOLUME_SIZES), "stage3 packed total mismatch")
    return dict(manifest=manifest, volumes=volumes, producer_log_sha256=LOG_HASH, producer_log_size=LOG_SIZE)


def continuation_identity():
    return dict(status="verified", operation="unchanged-mac156-x64-stage3-continuation",
                repository=intel.REPOSITORY, run_id=RUN, attempt=ATTEMPT, stage=3, job_id=JOB,
                head_sha=SHA, tree=TREE, artifacts=ARTIFACTS, version=intel.VERSION, platform="macos", arch="x64",
                preparation_key=intel.KEY, source_identity=intel.SOURCE_IDENTITY, producer_log_sha256=LOG_HASH,
                completed_logical_stage=11,
                continuation_stages="1..8 after failed stage3; stage2 handoff (logical 12..19)", changed_files=[])


def donor_identity(work):
    previous.donor_identity(work)
    for name, digest in DONOR_RECEIPTS.items():
        require(intel.sha(intel.read(work, name)) == digest, "exact packed stage3 history required: " + name)
    receipt = json.loads(intel.read(work, previous.RECEIPT))
    require(all(receipt.get(k) == v for k, v in previous.continuation_identity().items())
            and receipt.get("continuation_run") == str(RUN)
            and receipt.get("provenance", {}).get("manifest") == previous.expected_manifest()
            and receipt.get("donor_receipts") == previous.DONOR_RECEIPTS,
            "original stage2 admission provenance changed")
    directory = work / "fingerprint-diagnostics"
    expected_names = {Path(name).name for name in DONOR_RECEIPTS
                      if Path(name).name.startswith(previous.CONTINUATION)}
    require({p.name for p in directory.glob(previous.CONTINUATION + "-*.json")} == expected_names,
            "unexpected stage2 continuation history or failed stage3 compile evidence")
    handoff = json.loads(intel.read(work, HANDOFF))
    expected = dict(previous.continuation_identity(), continuation_run=str(RUN), continuation_attempt="1",
                    continuation_stage=2, logical_stage=11, phase="after")
    require(all(handoff.get(k) == v for k, v in expected.items())
            and handoff["state"]["graph"] == receipt["state"]["graph"],
            "exact stage2 handoff provenance and unchanged graph required")
    return receipt, handoff


def admit(work, donor, repo, origin, download_report):
    return previous.admit(work, donor, repo, origin, download_report, checkpoint=sys.modules[__name__])


def continuation(repo, work, stage, phase):
    return previous.continuation(repo, work, stage, phase, checkpoint=sys.modules[__name__])


def compile_tree(repo, work, stage):
    return previous.compile_tree(repo, work, stage, checkpoint=sys.modules[__name__])


def main(argv=None):
    return previous.main(argv, checkpoint=sys.modules[__name__])


if __name__ == "__main__":
    raise SystemExit(main())
