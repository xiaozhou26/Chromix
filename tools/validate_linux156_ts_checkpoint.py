#!/usr/bin/env python3
"""Authenticate the two complete Linux156 TS-failure stage1 checkpoints."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from . import validate_posix_snapshot as snapshot
except ImportError:
    import validate_posix_snapshot as snapshot

REPOSITORY = "xiaozhou26/Chromix"
HEAD = "3dfe10e164966f9d8f99099849c4f2aa1ed9138c"
DONORS = {
    "x64": {
        "run_id": 42457985068, "job_id": 128660989816,
        "artifact_id": 13608199661, "size": 2555795761,
        "digest": "sha256:5c8df28d29c741b29e3d6a732c875648469fe74e6083c5e59c7068ba4e576910",
        "receipt_sha256": "f6d9db1db3ce5e1f0044f7de7002ca20ba2b769e25a888def23db8392f48416c",
        "args_sha256": "427d7afafa27f2fe20af95c24a9d052ba4937340a243a8af66a7115193955e4b",
        "ninja_sha256": "4a855154a3438e301e9de9cadac4ed062b8491b614e117f0037bb91f935ff068",
        "log_sha256": "8670b20df9a464aae8e1538786955813af68823e6b017a21424456e329470f8c",
        "log_size": 17145051,
    },
    "arm64": {
        "run_id": 42457987604, "job_id": 128660997142,
        "artifact_id": 13609217709, "size": 3338283011,
        "digest": "sha256:e4ac75cda39353a4e8c044026f3e603f07b571443e74452b42facfb55c4c158a",
        "receipt_sha256": "4787b8b0a162f97c302716438a1f1ed35f196db316a78beda7da04a2104b7237",
        "args_sha256": "5a336da25a6b5b90408441adfc29457c85f57a46dc3196c433a3090198f2ac7e",
        "ninja_sha256": "893f3d15c7d964fdca7aeff71074137ca3308b86cb65b4eee0bc731ecb411ac6",
        "log_sha256": "c180d678ac302afb61ffafabe19f36c5fbc30988e7d8c92594a75467a2a0088e",
        "log_size": 27879330,
    },
}
PATCH_RECEIPT_SHA256 = "63d24adcdc0ab6254924371b83e4b7f3092e31ff7065052256cf85d54db7ca2a"


def expected(arch: str) -> dict:
    donor = DONORS[arch]
    prefix = f"chromix-linux-{arch}-tree-s1-attempt-1-part"
    return {
        "repository": REPOSITORY, "platform": "linux", "workflow": f"build-linux-{arch}",
        "run_id": donor["run_id"], "attempt": 1, "stage": 1, "arch": arch,
        "head_sha": HEAD, "job_id": donor["job_id"], "pattern": prefix + "*",
        "artifacts": [{"id": donor["artifact_id"], "name": prefix + "1",
                       "size_in_bytes": donor["size"], "expired": False, "digest": donor["digest"]}],
    }


def check_manifest(manifest: dict, arch: str) -> None:
    if manifest != expected(arch):
        raise ValueError("Linux156 TS checkpoint must match exact latest stage1 donor metadata")
    # JSON booleans compare equal to integers; enforce the serialized identity too.
    if json.dumps(manifest, sort_keys=True) != json.dumps(expected(arch), sort_keys=True):
        raise ValueError("Linux156 TS checkpoint metadata types changed")


def validate(client, arch: str, repository=REPOSITORY) -> dict:
    if repository != REPOSITORY:
        raise ValueError("Linux156 TS checkpoint repository mismatch")
    donor = DONORS[arch]
    report = snapshot.validate(client, repository, donor["run_id"], 1, 1, arch,
                               [donor["artifact_id"]], platform="linux")
    check_manifest(report, arch)
    run = client.get(f"/actions/runs/{donor['run_id']}")
    if (run.get("head_branch") != "main" or run.get("head_sha") != HEAD
            or run.get("run_attempt") != 1 or run.get("conclusion") != "failure"):
        raise ValueError("Linux156 TS donor run no longer matches the recorded failure")
    jobs = client.items(f"/actions/runs/{donor['run_id']}/attempts/1/jobs", "jobs")
    selected = [job for job in jobs if job.get("id") == donor["job_id"]]
    if len(selected) != 1 or selected[0].get("conclusion") != "failure":
        raise ValueError("Linux156 TS exact failed donor job missing")
    steps = {step["name"]: step.get("conclusion") for step in selected[0].get("steps", [])}
    if (steps.get("Run stage 1") != "failure"
            or steps.get("Preserve latest failed Linux source/output checkpoint") != "success"):
        raise ValueError("Linux156 TS donor did not preserve the latest failed tree")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=tuple(DONORS), required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    repository = os.environ.get("GITHUB_REPOSITORY", REPOSITORY)
    report = validate(snapshot.Client(repository, os.environ.get("GH_TOKEN", "")), args.arch, repository)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"head_sha={HEAD}\npattern={report['pattern']}\n")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
