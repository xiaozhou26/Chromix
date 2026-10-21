#!/usr/bin/env python3
"""Authenticate only the two compiled mac155 stage1 PNG donors."""
from __future__ import annotations

import json
from pathlib import Path
import re

try:
    from . import validate_macos155_stage7_checkpoint as common
except ImportError:
    import validate_macos155_stage7_checkpoint as common

require, read, sha = common.require, common.read, common.sha
METADATA = "build/macos155-png-checkpoints.json"
METADATA_SHA256 = "09306453dfe00eee9d2a779453032a82ae3de061f553c43771639b05937edfc4"


def checkpoints():
    raw = read(Path(__file__).resolve().parents[1], METADATA)
    require(sha(raw) == METADATA_SHA256, "reviewed PNG checkpoint metadata changed")
    return json.loads(raw)


def checkpoint(arch):
    require(arch in ("arm64", "x64"), "native macOS PNG architecture required")
    return checkpoints()[arch]


def validate_metadata(client, repository, artifact_ids, arch):
    d = checkpoint(arch)
    require(repository == d["repository"] and artifact_ids == [a["id"] for a in d["artifacts"]],
            "exact ordered PNG donor archives/repository required")
    run = client.get(f'/actions/runs/{d["run_id"]}')
    expected = dict(id=d["run_id"], head_sha=d["head_sha"], head_branch=d["source_branch"],
                    run_attempt=1, path=f'.github/workflows/{d["workflow"]}.yml',
                    event="workflow_dispatch", status="completed", conclusion="failure")
    require(all(run.get(k) == v for k, v in expected.items())
            and all(run.get(k, {}).get("full_name") == repository
                    for k in ("repository", "head_repository")), "PNG producer run mismatch")
    job = client.get(f'/actions/jobs/{d["job_id"]}')
    expected = dict(id=d["job_id"], run_id=d["run_id"], head_sha=d["head_sha"],
                    head_branch=d["source_branch"], run_attempt=1, status="completed",
                    conclusion="failure", labels=d["labels"], name=d["job_name"])
    require(all(job.get(k) == v for k, v in expected.items()), "native PNG producer job mismatch")
    steps = {s["name"]: s for s in job.get("steps", [])}
    for name, conclusion in {"Run stage 1": "failure", "Verify handoff snapshot": "success",
            "Preserve latest macOS compiled checkpoint": "success",
            "Upload failed macOS runtime bundle": "success",
            **{f"Upload tree part {i}": "success" for i in range(1, 5)}}.items():
        require((steps.get(name, {}).get("status"), steps.get(name, {}).get("conclusion"))
                == ("completed", conclusion), "PNG compiled checkpoint step mismatch: " + name)
    artifacts = sorted((a for a in client.items(f'/actions/runs/{d["run_id"]}/artifacts', "artifacts")
                        if a.get("name", "").startswith(d["pattern"][:-1])), key=lambda a: a["name"])
    require(len(artifacts) == 2, "exact complete two-volume PNG checkpoint required")
    for item, expected in zip(artifacts, d["artifacts"]):
        require(all(item.get(k) == v for k, v in expected.items()), "PNG archive identity mismatch")
        require(all(item.get("workflow_run", {}).get(k) == v for k, v in
                    dict(id=d["run_id"], head_sha=d["head_sha"], head_branch=d["source_branch"]).items()),
                "PNG archive origin mismatch")
    return {**{k: d[k] for k in ("repository", "platform", "arch", "run_id", "attempt", "stage",
            "job_id", "head_sha", "source_tree", "source_branch", "workflow", "version", "pattern", "artifacts")},
            "producer_job": job}


def downloaded(logs, d):
    origin = json.loads(read(logs, "snapshot-origin.json"))
    require(all(origin.get(k) == d[k] for k in ("repository", "platform", "arch", "run_id", "attempt",
            "stage", "job_id", "head_sha", "source_tree", "artifacts")), "exact PNG origin report required")
    report = json.loads(read(logs, "snapshot-download.json"))
    expected = dict(status="success", publication="published", phase="complete",
                    repository=d["repository"], run_id=d["run_id"], head_sha=d["head_sha"])
    require(all(report.get(k) == v for k, v in expected.items()), "digest-verified PNG download required")
    require(len(report.get("artifacts", [])) == 2, "two downloaded PNG archives required")
    for item, expected in zip(report["artifacts"], d["artifacts"]):
        require(all(item.get(k) == v for k, v in expected.items() if k != "expired"),
                "downloaded PNG archive identity mismatch")
        attempts = item.get("attempts", [])
        require(attempts and attempts[-1].get("status") == "success"
                and attempts[-1].get("size_in_bytes") == expected["size_in_bytes"]
                and attempts[-1].get("sha256") == expected["digest"][7:], "PNG archive checksum/size mismatch")
    volumes = report.get("volumes", [])
    require([v.get("name") for v in volumes] == ["tree.tar.zst.001", "tree.tar.zst.002"],
            "exact extracted PNG volumes required")
    for volume, artifact in zip(volumes, d["artifacts"]):
        require(volume.get("artifact_id") == artifact["id"]
                and type(volume.get("size_in_bytes")) is int and volume["size_in_bytes"] > 0
                and re.fullmatch(r"[0-9a-f]{64}", volume.get("sha256", "")), "PNG volume origin/hash required")
    return {name: sha(read(logs, name)) for name in ("snapshot-origin.json", "snapshot-download.json")}


def reject_mixed_receipts(work, d):
    other = checkpoint("x64" if d["arch"] == "arm64" else "arm64")
    for name in other["pinned_files"].keys() - d["pinned_files"].keys():
        require(not (work / name).exists() and not (work / name).is_symlink(),
                "mixed ARM64/Intel PNG donor receipts")


def donor_receipts(work, d):
    reject_mixed_receipts(work, d)
    for name, digest in d["pinned_files"].items():
        require(sha(read(work, name)) == digest, "compiled PNG donor receipt/GN mismatch: " + name)
    latest = json.loads(read(work, d["latest_receipt"]))
    require(latest.get("continuation_run") == str(d["run_id"])
            and latest.get("continuation_attempt") == "1" and latest.get("continuation_stage") == 1
            and latest.get("phase") == "after" and latest.get("patch_count") == 225
            and latest.get("state") == d["state"] and latest.get("key") == d["previous_key"],
            "actual compiled stage1 provenance required, not nested controller donor")
    for name, digest in d["state"].items():
        require(sha(read(work / "src", name)) == digest, "latest compiled Ninja state required: " + name)
    return latest
