#!/usr/bin/env python3
"""Validate an exact same-repository POSIX snapshot before downloading its volumes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

LIMIT = 2 * 1024 * 1024


def positive(value: str, label: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ValueError(f"{label} must be a positive integer")
    return int(value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class Client:
    def __init__(self, repository: str, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("invalid repository")
        if not token:
            raise ValueError("GitHub token is required")
        self.base = f"https://api.github.com/repos/{repository}"
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect)

    def get(self, path: str) -> dict:
        request = urllib.request.Request(self.base + path, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with self.opener.open(request, timeout=30) as response:
            data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError("GitHub metadata response exceeds size limit")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("invalid GitHub metadata response")
        return result

    def items(self, path: str, key: str) -> list[dict]:
        result = []
        for page in range(1, 21):
            data = self.get(f"{path}?per_page=100&page={page}")
            batch = data[key]
            if not isinstance(batch, list) or not isinstance(data["total_count"], int):
                raise ValueError("invalid paginated GitHub response")
            result.extend(batch)
            if len(result) == data["total_count"]:
                return result
            if not batch or len(result) > data["total_count"]:
                break
        raise ValueError("incomplete GitHub pagination")


def validate(client, repository: str, run_id: int, stage: int, attempt: int, arch: str,
             expected_artifact_ids: list[int], platform: str = "macos", *,
             recovery_branch: str | None = None) -> dict:
    if (not 1 <= len(expected_artifact_ids) <= 4
            or len(set(expected_artifact_ids)) != len(expected_artifact_ids)
            or any(type(value) is not int or value <= 0 for value in expected_artifact_ids)):
        raise ValueError("supply the complete recorded set of snapshot artifact IDs (1-4 unique IDs)")
    if platform not in ("linux", "macos") or arch not in ("x64", "arm64") or not 1 <= stage <= 8 or attempt < 1 or run_id < 1:
        raise ValueError("invalid POSIX snapshot selection")
    workflow = f"build-{platform}-{arch}"
    run = client.get(f"/actions/runs/{run_id}")
    if (run.get("id") != run_id or run.get("name") != workflow
            or run.get("path") != f".github/workflows/{workflow}.yml"
            or run.get("head_branch") not in ("main", recovery_branch or "main")
            or run.get("event") not in ("push", "workflow_dispatch")
            or (run.get("head_branch") != "main" and run.get("event") != "workflow_dispatch")
            or run.get("repository", {}).get("full_name") != repository
            or run.get("head_repository", {}).get("full_name") != repository
            or run.get("status") != "completed"
            or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))
            or not isinstance(run.get("run_attempt"), int) or attempt > run["run_attempt"]):
        raise ValueError("snapshot run identity, origin, or terminal status mismatch")
    jobs = client.items(f"/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    candidates = [job for job in jobs if re.search(
        rf"(?:^| / ){platform}-{arch} stage {stage} \(", job.get("name", ""))]
    if len(candidates) != 1 or candidates[0].get("status") != "completed":
        raise ValueError("exact donor stage is missing or incomplete")
    job = candidates[0]
    steps = {item["name"]: item.get("conclusion") for item in job.get("steps", [])}
    if steps.get("Verify handoff snapshot") != "success":
        raise ValueError("donor stage has no verified checkpoint")
    if any(steps.get(f"Upload tree part {index}") != "success" for index in range(1, 5)):
        raise ValueError("donor checkpoint upload set is incomplete")
    artifact_platform = "mac" if platform == "macos" else platform
    prefix = f"chromix-{artifact_platform}-{arch}-tree-s{stage}-attempt-{attempt}-part"
    artifacts = [item for item in client.items(f"/actions/runs/{run_id}/artifacts", "artifacts")
                 if item.get("name", "").startswith(prefix)]
    if not 1 <= len(artifacts) <= 4:
        raise ValueError("snapshot artifact set missing or ambiguous")
    if {item.get("id") for item in artifacts} != set(expected_artifact_ids):
        raise ValueError("snapshot artifact set differs from the complete recorded ID set")
    by_part = {}
    for item in artifacts:
        suffix = item["name"][len(prefix):]
        if suffix not in ("1", "2", "3", "4") or suffix in by_part:
            raise ValueError("invalid or duplicate snapshot part")
        if item.get("expired") is not False or not isinstance(item.get("size_in_bytes"), int) or item["size_in_bytes"] <= 0:
            raise ValueError("snapshot part expired or empty")
        digest = item.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("snapshot artifact has no verified SHA-256 digest")
        if steps.get(f"Upload tree part {suffix}") != "success":
            raise ValueError("snapshot upload did not succeed")
        origin = item.get("workflow_run", {})
        if origin.get("id") != run_id or origin.get("head_sha") != run["head_sha"]:
            raise ValueError("snapshot artifact origin mismatch")
        by_part[suffix] = item
    if set(by_part) != {str(index) for index in range(1, len(artifacts) + 1)}:
        raise ValueError("snapshot parts are not contiguous")
    return {
        "repository": repository,
        "platform": platform, "workflow": workflow,
        "run_id": run_id, "attempt": attempt, "stage": stage, "arch": arch,
        "head_sha": run["head_sha"], "job_id": job["id"], "pattern": prefix + "*",
        "artifacts": [{key: item[key] for key in ("id", "name", "size_in_bytes", "expired", "digest")}
                      for _, item in sorted(by_part.items())],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-id", default=os.environ.get("SNAPSHOT_RUN_ID", ""))
    parser.add_argument("--stage", default=os.environ.get("SNAPSHOT_STAGE", ""))
    parser.add_argument("--attempt", default=os.environ.get("SNAPSHOT_ATTEMPT", ""))
    parser.add_argument("--artifact-ids", default=os.environ.get("SNAPSHOT_ARTIFACT_IDS", ""))
    parser.add_argument("--platform", choices=("linux", "macos"), default=os.environ.get("BUILD_PLATFORM", "macos"))
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--recovery-branch", help="Also accept a manual donor from this exact recovery branch")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    client = Client(args.repository, os.environ.get("GH_TOKEN", ""))
    report = validate(client, args.repository, positive(args.run_id, "run ID"),
                      positive(args.stage, "stage"), positive(args.attempt, "attempt"), args.arch,
                      [positive(value.strip(), "artifact ID") for value in args.artifact_ids.split(",")],
                      platform=args.platform, recovery_branch=args.recovery_branch)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"head_sha={report['head_sha']}\npattern={report['pattern']}\n")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
