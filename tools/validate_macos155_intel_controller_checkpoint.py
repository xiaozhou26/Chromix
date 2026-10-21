#!/usr/bin/env python3
"""Authenticate the failed mac155 Intel continuation stage8, never its stage7 ancestor."""
from __future__ import annotations

import json
import re

try:
    from . import validate_macos155_stage7_checkpoint as ancestor
except ImportError:
    import validate_macos155_stage7_checkpoint as ancestor

require = ancestor.require
sha = ancestor.sha
read = ancestor.read
REPOSITORY = ancestor.REPOSITORY
WORKFLOW = "build-mac155-intel-controller-resume"
RUN = 39277477057
ATTEMPT = 1
STAGE = 8
JOB = 118881046382
SHA = "a16e1769b42dcd13c6f6f226fdd5492f7b598e43"
TREE = "dfb836a35b84d9816952e99d91593345e7c343fe"
BRANCH = "build/mac155-x64-stage7-resume"
PREFIX = "chromix-mac-x64-tree-s8-attempt-1-part"
ARTIFACTS = [
    {
        "id": 12312896348,
        "name": "chromix-mac-x64-tree-s8-attempt-1-part1",
        "size_in_bytes": 9024567219,
        "expired": False,
        "digest": "sha256:eb54b3d56951d178e9a68475450291f46cd406c542611f34e93368385d4ddc06"
    }
]
IDS = [item["id"] for item in ARTIFACTS]
LATEST_RECEIPT = "fingerprint-diagnostics/mac155-stage7-continuation-s8-after-attempt-1.json"
LATEST_SOURCE = LATEST_RECEIPT.replace(".json", "-source.json")
PINNED_FILES = {
    "src/out/Default/args.gn": "1dc792400a3fdf9f787b2068039caf246843361f3eb8deb46de1fd6e6bba6eba",
    "src/.chromix-upstream-restored.json": "5ed98fda38aba6a69e91882590cb19d1e71a0e65b786555f6bd5e68b83add330",
    "src/.chromix-restored-patches.json": "d3736f61b2f273ddb677f29aa734efd7c54fa0381a9e4b4c29a57d73d83a4ca3",
    "fingerprint-diagnostics/source-final.json": "b53ba63cc6a17ec4c8c26a4e2f898604cb30f54e219c26fa7df0a5a7136cccc4",
    "fingerprint-diagnostics/mac155-stage7-resume.json": "75985a8e29917056ea4e9b206db418844f0f2685e1423dd8562788e603f7d096",
    "fingerprint-diagnostics/mac155-stage7-continuation-s8-after-attempt-1.json": "c7a82706f1961ecdc76315bf0523b7a7d30dfdec5f6f7ea7106d74a85a5a5777",
    "fingerprint-diagnostics/mac155-stage7-continuation-s8-after-attempt-1-source.json": "b53ba63cc6a17ec4c8c26a4e2f898604cb30f54e219c26fa7df0a5a7136cccc4",
    "upstream-cache-preparation.json": "80b6a94b486f3587cf9c62f8a506ef1ed8aa30b48a26a0652de4a2daaa2041de",
    "src/.chromix-restored-build-prepared.json": "80b6a94b486f3587cf9c62f8a506ef1ed8aa30b48a26a0652de4a2daaa2041de"
}
STATE = {
    "out/Default/.ninja_deps": "20325e3bfcf52dee922f03e7422e46e5efdf940eaa1f54dc8caef24c92176bbf",
    "out/Default/.ninja_log": "81b5c1dcfef5125a5e6bf29c4b28fe8276d81e9602a764bce60838af591ef7a9",
    "out/Default/build.ninja": "f01ef5846a5981fb868ff8eee2a99177b86d520e5e45497dede45f47f0e1eb49"
}


def validate_metadata(client, repository, artifact_ids):
    require(repository == REPOSITORY and artifact_ids == IDS,
            "exact latest stage8 repository and single artifact ID required")
    run = client.get(f"/actions/runs/{RUN}")
    expected = dict(id=RUN, head_sha=SHA, head_branch=BRANCH, run_attempt=ATTEMPT,
                    path=".github/workflows/build-mac155-x64-stage7-resume.yml",
                    event="workflow_dispatch", status="completed", conclusion="failure")
    require(all(run.get(k) == v for k, v in expected.items())
            and all(run.get(k, {}).get("full_name") == REPOSITORY
                    for k in ("repository", "head_repository")), "latest stage8 donor run mismatch")
    job = client.get(f"/actions/jobs/{JOB}")
    expected = dict(id=JOB, run_id=RUN, head_sha=SHA, head_branch=BRANCH, run_attempt=ATTEMPT,
                    status="completed", conclusion="failure", labels=["macos-15-intel"],
                    name="mac155 Intel (original stage7 + isolated continuation 1..8) / "
                         "macos-x64 stage 8 (resume compile)")
    require(all(job.get(k) == v for k, v in expected.items()), "latest native stage8 job mismatch")
    steps = {s["name"]: s for s in job.get("steps", [])}
    for name, conclusion in {
        "Run stage 8": "failure", "Verify handoff snapshot": "success",
        "Preserve latest macOS compiled checkpoint": "skipped",
        "Upload failed macOS runtime bundle": "skipped",
        **{f"Upload tree part {i}": "success" for i in range(1, 5)},
    }.items():
        require((steps.get(name, {}).get("status"), steps.get(name, {}).get("conclusion"))
                == ("completed", conclusion), f"latest stage8 producer step mismatch: {name}")
    artifacts = [a for a in client.items(f"/actions/runs/{RUN}/artifacts", "artifacts")
                 if a.get("name", "").startswith(PREFIX)]
    require(len(artifacts) == 1, "complete exact single-volume stage8 artifact set required")
    artifacts.sort(key=lambda a: a["name"])
    for item, expected in zip(artifacts, ARTIFACTS):
        require(all(item.get(k) == v for k, v in expected.items()), "stage8 archive identity mismatch")
        origin = item.get("workflow_run", {})
        require(all(origin.get(k) == v for k, v in
                    dict(id=RUN, head_sha=SHA, head_branch=BRANCH).items()), "stage8 archive origin mismatch")
    return dict(repository=REPOSITORY, platform="macos", arch="x64", run_id=RUN,
                attempt=ATTEMPT, stage=STAGE, job_id=JOB, head_sha=SHA, source_tree=TREE,
                source_branch=BRANCH, workflow="build-mac155-x64-stage7-resume",
                version=ancestor.VERSION, pattern=PREFIX + "*", artifacts=ARTIFACTS,
                producer_job=job)


def downloaded(logs):
    """Require the independently verified archive download, not merely an origin assertion."""
    origin = json.loads(read(logs, "snapshot-origin.json"))
    require(all(origin.get(k) == v for k, v in dict(
        repository=REPOSITORY, run_id=RUN, attempt=ATTEMPT, stage=STAGE, job_id=JOB,
        head_sha=SHA, source_tree=TREE, platform="macos", arch="x64", artifacts=ARTIFACTS).items()),
        "exact stage8 origin report required")
    report = json.loads(read(logs, "snapshot-download.json"))
    require(all(report.get(k) == v for k, v in dict(status="success", publication="published",
        phase="complete", repository=REPOSITORY, run_id=RUN, head_sha=SHA).items()),
        "successful digest-verified stage8 download required")
    require(len(report.get("artifacts", [])) == 1, "exact downloaded archive required")
    for item, expected in zip(report["artifacts"], ARTIFACTS):
        require(all(item.get(k) == v for k, v in expected.items() if k != "expired"),
                "downloaded archive identity mismatch")
        attempts = item.get("attempts", [])
        require(attempts and attempts[-1].get("status") == "success"
                and attempts[-1].get("size_in_bytes") == expected["size_in_bytes"]
                and attempts[-1].get("sha256") == expected["digest"][7:],
                "downloaded archive checksum/size not verified")
    volumes = report.get("volumes", [])
    require([v.get("name") for v in volumes] == ["tree.tar.zst.001"],
            "exact single extracted volume required")
    for volume, artifact in zip(volumes, ARTIFACTS):
        require(volume.get("artifact_id") == artifact["id"]
                and type(volume.get("size_in_bytes")) is int and volume["size_in_bytes"] > 0
                and re.fullmatch(r"[0-9a-f]{64}", volume.get("sha256", "")),
                "extracted volume origin/size/hash required")
    return {name: sha(read(logs, name)) for name in ("snapshot-origin.json", "snapshot-download.json")}


def donor_receipts(work):
    for name, digest in PINNED_FILES.items():
        require(sha(read(work, name)) == digest, f"latest stage8 receipt/GN mismatch: {name}")
    receipt = json.loads(read(work, LATEST_RECEIPT))
    require(receipt.get("continuation_run") == str(RUN)
            and receipt.get("continuation_attempt") == str(ATTEMPT)
            and receipt.get("continuation_stage") == STAGE and receipt.get("logical_stage") == 15
            and receipt.get("phase") == "after" and receipt.get("status") == "verified"
            and receipt.get("source_identity") == ancestor.SOURCE_IDENTITY
            and receipt.get("state") == STATE, "actual latest continuation provenance required")
    expected = dict(status="verified", operation="unchanged-stage7-continuation",
                    repository=REPOSITORY, run_id=ancestor.RUN, attempt=ancestor.ATTEMPT,
                    stage=ancestor.STAGE, job_id=ancestor.JOB, head_sha=ancestor.SHA,
                    tree=ancestor.TREE, artifact_id=ancestor.ARTIFACT, artifact_digest=ancestor.DIGEST,
                    platform="macos", arch="x64", version=ancestor.VERSION,
                    preparation_key=ancestor.KEY, source_identity=ancestor.SOURCE_IDENTITY,
                    continuation_stages="1..8 after donor stage7", changed_files=[])
    original = json.loads(read(work, ancestor.RECEIPT))
    require(all(original.get(k) == v and receipt.get(k) == v for k, v in expected.items()),
            "nested original stage7 and latest stage8 identities must remain distinct and exact")
    source = json.loads(read(work, LATEST_SOURCE))
    require(source.get("status") == "verified" and source.get("patch_count") == 224
            and source.get("domain_substituted") is True
            and ancestor.identity_hash(source.get("identity")) == ancestor.SOURCE_IDENTITY
            and ancestor.identity_hash(source.get("outputs")) == ancestor.SOURCE_OUTPUTS,
            "latest Intel source identity/output hashes required")
    ancestor.preparation(work)
    return receipt


def donor_state(work):
    donor_receipts(work)
    state = {name: sha(read(work / "src", name)) for name in STATE}
    require(state == STATE, "latest stage8 Ninja state required; original stage7 is not admissible")
    return state
