#!/usr/bin/env python3
"""Authenticate the failed mac155 ARM64 continuation stage6, never its stage8 ancestor."""
from __future__ import annotations

import json
import re

try:
    from . import validate_macos155_arm64_stage8_checkpoint as ancestor
except ImportError:
    import validate_macos155_arm64_stage8_checkpoint as ancestor

require = ancestor.require
sha = ancestor.sha
read = ancestor.read
REPOSITORY = ancestor.REPOSITORY
WORKFLOW = "build-mac155-arm64-controller-resume"
RUN = 39280602881
ATTEMPT = 1
STAGE = 6
JOB = 118598389953
SHA = "77b940f4a5d72b2299f4bcb8776ee8aa5d8351ae"
TREE = "62597df18e217d315c7ea7db32de0bd5fb576634"
BRANCH = "build/mac155-arm64-stage8-resume"
PREFIX = "chromix-mac-arm64-tree-s6-attempt-1-part"
ARTIFACTS = [
    dict(id=12266957738, name=PREFIX + "1", size_in_bytes=9663676674,
         digest="sha256:6e958fc5f671167dec1523df6938f39d600de46d8d9d33f97ccb068c9ec81ba9", expired=False),
    dict(id=12267091707, name=PREFIX + "2", size_in_bytes=488618433,
         digest="sha256:fa4bb1a7e0ce10858fd3272b43260225ceb83396ede0a8d7d0de1decddee435e", expired=False),
]
IDS = [item["id"] for item in ARTIFACTS]
LATEST_RECEIPT = "fingerprint-diagnostics/mac155-arm64-stage8-continuation-s6-after-attempt-1.json"
PINNED_FILES = {
    **ancestor.PINNED_FILES,
    ancestor.RECEIPT: "dc4d14d3d075fc3ccddf1bc2f5088f52e9d5f53423cb1f00b3fd9024f81302de",
    LATEST_RECEIPT: "ca2f3762ce83eed9c571bdb709aac842b0b98beb5cddbdeedc93e9faf464925e",
    "fingerprint-diagnostics/mac155-arm64-stage8-continuation-s6-after-attempt-1-source.json":
        "b53ba63cc6a17ec4c8c26a4e2f898604cb30f54e219c26fa7df0a5a7136cccc4",
    "upstream-cache-preparation.json": "9db40c4651bef5fb4adacfada73abafbf905e95e0b1ed61e8df30f6a6b3c3087",
    "src/.chromix-restored-build-prepared.json": "9db40c4651bef5fb4adacfada73abafbf905e95e0b1ed61e8df30f6a6b3c3087",
}
STATE = {
    "out/Default/.ninja_deps": "6a32d93835bcb3cfb943213a794c6c664cb5856a20aa29ca0e5998b242e4a507",
    "out/Default/.ninja_log": "3a49942e23574dafe4e7f9ce01263d83dccd4ed488c35f62072204cf6538b4b1",
    "out/Default/build.ninja": "a97bf8cdbbed4f430c9503c9769b2c880214ceffa7cbda0e0aef0322f4e2aba7",
}


def validate_metadata(client, repository, artifact_ids):
    require(repository == REPOSITORY and artifact_ids == IDS,
            "exact latest stage6 repository and both ordered artifact IDs required")
    run = client.get(f"/actions/runs/{RUN}")
    expected = dict(id=RUN, head_sha=SHA, head_branch=BRANCH, run_attempt=ATTEMPT,
                    path=".github/workflows/build-mac155-arm64-stage8-resume.yml",
                    event="workflow_dispatch", status="completed", conclusion="failure")
    require(all(run.get(k) == v for k, v in expected.items())
            and all(run.get(k, {}).get("full_name") == REPOSITORY
                    for k in ("repository", "head_repository")), "latest stage6 donor run mismatch")
    job = client.get(f"/actions/jobs/{JOB}")
    expected = dict(id=JOB, run_id=RUN, head_sha=SHA, head_branch=BRANCH, run_attempt=ATTEMPT,
                    status="completed", conclusion="failure", labels=["macos-15"],
                    name="mac155 ARM64 (original stage8 + isolated continuation 1..8) / "
                         "macos-arm64 stage 6 (resume compile)")
    require(all(job.get(k) == v for k, v in expected.items()), "latest native stage6 job mismatch")
    steps = {s["name"]: s for s in job.get("steps", [])}
    for name, conclusion in {
        "Run stage 6": "failure", "Verify handoff snapshot": "success",
        "Preserve latest macOS compiled checkpoint": "skipped",
        "Upload failed macOS runtime bundle": "skipped",
        **{f"Upload tree part {i}": "success" for i in range(1, 5)},
    }.items():
        require((steps.get(name, {}).get("status"), steps.get(name, {}).get("conclusion"))
                == ("completed", conclusion), f"latest stage6 producer step mismatch: {name}")
    artifacts = [a for a in client.items(f"/actions/runs/{RUN}/artifacts", "artifacts")
                 if a.get("name", "").startswith(PREFIX)]
    require(len(artifacts) == 2, "complete exact two-volume stage6 artifact set required")
    artifacts.sort(key=lambda a: a["name"])
    for item, expected in zip(artifacts, ARTIFACTS):
        require(all(item.get(k) == v for k, v in expected.items()), "stage6 archive identity mismatch")
        origin = item.get("workflow_run", {})
        require(all(origin.get(k) == v for k, v in
                    dict(id=RUN, head_sha=SHA, head_branch=BRANCH).items()), "stage6 archive origin mismatch")
    return dict(repository=REPOSITORY, platform="macos", arch="arm64", run_id=RUN,
                attempt=ATTEMPT, stage=STAGE, job_id=JOB, head_sha=SHA, source_tree=TREE,
                source_branch=BRANCH, workflow="build-mac155-arm64-stage8-resume",
                version=ancestor.VERSION, pattern=PREFIX + "*", artifacts=ARTIFACTS,
                producer_job=job)


def downloaded(logs):
    """Require the independently verified archive download, not merely an origin assertion."""
    origin = json.loads(read(logs, "snapshot-origin.json"))
    require(all(origin.get(k) == v for k, v in dict(
        repository=REPOSITORY, run_id=RUN, attempt=ATTEMPT, stage=STAGE, job_id=JOB,
        head_sha=SHA, source_tree=TREE, platform="macos", arch="arm64", artifacts=ARTIFACTS).items()),
        "exact stage6 origin report required")
    report = json.loads(read(logs, "snapshot-download.json"))
    require(all(report.get(k) == v for k, v in dict(status="success", publication="published",
        phase="complete", repository=REPOSITORY, run_id=RUN, head_sha=SHA).items()),
        "successful digest-verified stage6 download required")
    require(len(report.get("artifacts", [])) == 2, "both downloaded archives required")
    for item, expected in zip(report["artifacts"], ARTIFACTS):
        require(all(item.get(k) == v for k, v in expected.items() if k != "expired"),
                "downloaded archive identity mismatch")
        attempts = item.get("attempts", [])
        require(attempts and attempts[-1].get("status") == "success"
                and attempts[-1].get("size_in_bytes") == expected["size_in_bytes"]
                and attempts[-1].get("sha256") == expected["digest"][7:],
                "downloaded archive checksum/size not verified")
    volumes = report.get("volumes", [])
    require([v.get("name") for v in volumes] == ["tree.tar.zst.001", "tree.tar.zst.002"],
            "exact two extracted volumes required")
    for volume, artifact in zip(volumes, ARTIFACTS):
        require(volume.get("artifact_id") == artifact["id"]
                and type(volume.get("size_in_bytes")) is int and volume["size_in_bytes"] > 0
                and re.fullmatch(r"[0-9a-f]{64}", volume.get("sha256", "")),
                "extracted volume origin/size/hash required")
    return {name: sha(read(logs, name)) for name in ("snapshot-origin.json", "snapshot-download.json")}


def donor_receipts(work):
    for name, digest in PINNED_FILES.items():
        require(sha(read(work, name)) == digest, f"latest stage6 receipt/GN mismatch: {name}")
    receipt = json.loads(read(work, LATEST_RECEIPT))
    require(receipt.get("continuation_run") == str(RUN)
            and receipt.get("continuation_attempt") == str(ATTEMPT)
            and receipt.get("continuation_stage") == STAGE and receipt.get("logical_stage") == 14
            and receipt.get("phase") == "after" and receipt.get("status") == "verified"
            and receipt.get("source_identity") == ancestor.SOURCE_IDENTITY
            and receipt.get("state") == STATE, "actual latest continuation provenance required")
    ancestor.preparation(work)
    return receipt


def donor_state(work):
    donor_receipts(work)
    state = {name: sha(read(work / "src", name)) for name in STATE}
    require(state == STATE, "latest stage6 Ninja state required; original stage8 is not admissible")
    return state
