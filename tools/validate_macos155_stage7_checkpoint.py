#!/usr/bin/env python3
"""Admit the exact macOS 155 Intel stage7 donor without changing its build identity."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

try:
    from . import apply_restored_patches as arp
    from . import migrate_restored_snapshot as migration
    from . import validate_posix_snapshot as snapshots
except ImportError:
    import apply_restored_patches as arp
    import migrate_restored_snapshot as migration
    import validate_posix_snapshot as snapshots

REPOSITORY = "xiaozhou26/Chromix"
WORKFLOW = "build-mac155-x64-stage7-resume"
RUN = 39104895441
JOB = 117929863888
FAILED_JOB = 118020489702
STAGE = 7
ATTEMPT = 2
SHA = "a436049a48c9a84cdb83297f7ada1dad911cda7b"
TREE = "a89b1b52b7d35f32db7982f213af6aebdeeb351c"
BRANCH = "build/macos-new-donors"
VERSION = "155.0.8059.39"
ARTIFACT = 12180797398
SIZE = 8155132787
DIGEST = "sha256:5b57e2683e6717e0f952d9c3d3067a909a12c7331d226a34250f1367b4c4d3a1"
KEY = ("macos|x64|155.0.8059.39|ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a|"
       "b1238bb72b02cd36219991d9029c1c65183ea295|"
       "0962b146a7bf1d19e0a4cfa92277924cb829fd75fc8d0d96d69a896e344dbe4a")
SOURCE_IDENTITY = "28d28ee38e0c18cc73e492a604badcbfebd91dbc6040fe2d554a688492248697"
SOURCE_OUTPUTS = "42b460cb4558a901f8bd7d3faff62d02b29927edcb95cbed1238c5c2e22b082e"
UPSTREAM_IDENTITY = "a88c4299fbc4030582bfbaeb7b915fb20dca01adca71463a5fb57c63d662971e"
RECEIPT = "fingerprint-diagnostics/mac155-stage7-resume.json"
PINNED_FILES = {
    "src/out/Default/args.gn": "1dc792400a3fdf9f787b2068039caf246843361f3eb8deb46de1fd6e6bba6eba",
    "src/.chromix-upstream-restored.json": "5ed98fda38aba6a69e91882590cb19d1e71a0e65b786555f6bd5e68b83add330",
    "src/.chromix-restored-patches.json": "d3736f61b2f273ddb677f29aa734efd7c54fa0381a9e4b4c29a57d73d83a4ca3",
    "fingerprint-diagnostics/source-final.json": "b53ba63cc6a17ec4c8c26a4e2f898604cb30f54e219c26fa7df0a5a7136cccc4",
}
PREPARATION_HASH = "1ebff3491e82342b2e17d2658cab886f444274ae7982af018326ad283d53435a"
STATE_FILES = ("out/Default/build.ninja", "out/Default/.ninja_log", "out/Default/.ninja_deps")
RECIPE_FILES = (
    *migration.PIN_FILES, *migration.SCRIPT_FILES, "patches/series",
    "build/macos/build.sh", "build/args.macos.gn", "build/posix/upstream-cache.sh",
    "build/posix/prepare-restored-tools.sh", "tools/prepare_restored_build.py",
    "tools/restore_upstream_cache.py", "tools/merge_gn_args.py", "tools/bootstrap_gn.py",
    "tools/verify_patch_stack.py", "tools/restore_ninja.py", "tools/macos_runtime.py",
    "tools/install_macos_generators.py", "tools/macos_restored_generators.py",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def identity_hash(value):
    return sha(arp._json(value))


def read(root, name):
    return arp.patch_selection.read(root, name)


def validate_metadata(client, repository, artifact_ids):
    require(repository == REPOSITORY and artifact_ids == [ARTIFACT],
            "exact mac155 Intel stage7 repository/artifact required")
    report = snapshots.validate(client, repository, RUN, STAGE, ATTEMPT, "x64", artifact_ids,
                                platform="macos", recovery_branch=BRANCH)
    require(report["head_sha"] == SHA and report["job_id"] == JOB,
            "stage7 producer SHA/job mismatch")
    run = client.get(f"/actions/runs/{RUN}")
    require(run.get("head_branch") == BRANCH and run.get("event") == "workflow_dispatch"
            and run.get("run_attempt") == ATTEMPT and run.get("conclusion") == "failure",
            "exact failed manual attempt2 required")
    for identifier, stage, conclusion in ((JOB, 7, "success"), (FAILED_JOB, 8, "failure")):
        job = client.get(f"/actions/jobs/{identifier}")
        require(job.get("id") == identifier and job.get("run_id") == RUN
                and job.get("head_sha") == SHA and job.get("run_attempt") == ATTEMPT
                and job.get("status") == "completed" and job.get("conclusion") == conclusion,
                f"recorded stage{stage} job identity mismatch")
        steps = {step["name"]: step.get("conclusion") for step in job.get("steps", [])}
        expected = {f"Run stage {stage}": conclusion,
                    "Verify handoff snapshot": "success" if stage == 7 else "failure",
                    **{f"Upload tree part {part}": "success" if stage == 7 else "skipped"
                       for part in range(1, 5)}}
        require(all(steps.get(name) == value for name, value in expected.items()),
                f"recorded stage{stage} handoff steps mismatch")
    actual = [(item["id"], item["size_in_bytes"], item["digest"]) for item in report["artifacts"]]
    require(actual == [(ARTIFACT, SIZE, DIGEST)], "stage7 artifact ID/size/SHA-256 mismatch")
    artifacts = client.items(f"/actions/runs/{RUN}/artifacts", "artifacts")
    require(not any(item.get("name", "").startswith("chromix-mac-x64-tree-s8-attempt-2-")
                    for item in artifacts), "stage8 artifact exists; re-review latest valid donor")
    report.update(version=VERSION, source_branch=BRANCH, source_tree=TREE,
                  failed_job_id=FAILED_JOB, continuation_stages="1..8 after donor stage7")
    return report


def recipes(repo, donor=None):
    require(migration.source_ready_key(repo, "macos", "x64") == KEY,
            "donor preparation key changed; migration/cold fallback forbidden")
    result = {}
    for name in dict.fromkeys(RECIPE_FILES):
        data = read(repo, name)
        if donor is not None:
            require(data == read(donor, name), f"preparation/build recipe changed: {name}")
        result[name] = sha(data)
    return result


def preparation(work, *, initial=False):
    for name in ("upstream-cache-preparation.json", "src/.chromix-restored-build-prepared.json"):
        raw = read(work, name)
        if initial:
            require(sha(raw) == PREPARATION_HASH, f"stage7 preparation receipt mismatch: {name}")
        data = json.loads(raw)
        require((data.get("platform"), data.get("arch")) == ("macos", "x64")
                and data.get("ready_for_gn") is True
                and identity_hash(data.get("source_identity")) == UPSTREAM_IDENTITY,
                "prepared native mac155 Intel source identity required")
        require(all(data.get("counters", {}).get(field) == 0 for field in
                    ("first_finish", "tool_swap_invalidations", "toolchain_invalidated_outputs")),
                "full preparation/toolchain/output invalidation forbidden")


def source_state(repo, work, *, initial=False):
    src = work / "src"
    for name in (migration.TRANSACTION, arp.IN_PROGRESS):
        require(not (src / name).exists(), f"interrupted source operation: {name}")
    migration._ready(src, KEY)
    require(migration.source_ready_key(repo, "macos", "x64") == KEY, "preparation key changed")
    require(arp.patch_selection.source_version(src) == VERSION, "source version changed")
    for name, digest in PINNED_FILES.items():
        require(sha(read(work, name)) == digest, f"stage7 source/GN receipt changed: {name}")
    read(src, ".chromix-toolchain-ready")
    read(src, ".chromix-domain-substituted")
    state = {}
    for name in STATE_FILES:
        data = read(src, name)
        require(bool(data), f"missing incremental build state: {name}")
        state[name] = sha(data)
    preparation(work, initial=initial)
    return state


def live_source(repo, work, output):
    require(not output.exists(), "live source report must be new")
    subprocess.run([sys.executable, str(repo / "tools/verify_patch_stack.py"),
                    "--src", str(work / "src"), "--repo", str(repo),
                    "--core", str(work / "tooling/ungoogled-chromium"),
                    "--platform-tooling", str(work / "tooling/ungoogled-chromium-macos"),
                    "--platform", "macos", "--output", str(output)], check=True)
    receipt = json.loads(output.read_text())
    require(receipt.get("status") == "verified" and receipt.get("patch_count") == 224
            and receipt.get("domain_substituted") is True
            and identity_hash(receipt.get("identity")) == SOURCE_IDENTITY
            and identity_hash(receipt.get("outputs")) == SOURCE_OUTPUTS,
            "live mac155 source patch identity/output hashes differ from stage7")


def admit(work, donor, repo):
    require(migration._donor_git(donor, repo, "rev-parse", "HEAD").decode().strip() == SHA,
            "exact stage7 donor checkout required")
    require(migration._donor_git(donor, repo, "rev-parse", "HEAD^{tree}").decode().strip() == TREE,
            "stage7 donor tree mismatch")
    require(not migration._donor_git(donor, repo, "status", "--porcelain", "--untracked-files=all"),
            "donor checkout must be clean")
    recipe = recipes(repo, donor)
    require(migration.source_ready_key(donor, "macos", "x64") == KEY, "donor prepare key mismatch")
    state = source_state(repo, work, initial=True)
    migration.restore.verify_restored(work, "macos", "x64", repo=repo)
    migration._verify_tooling(work, repo, (work, donor, repo), "macos")
    report = work / RECEIPT
    require(not report.exists(), "donor must not contain a continuation receipt")
    live_source(repo, work, report.with_name("mac155-stage7-source-admission.json"))
    require(source_state(repo, work, initial=True) == state, "admission changed donor build state")
    result = dict(status="verified", operation="unchanged-stage7-continuation", repository=REPOSITORY,
                  run_id=RUN, attempt=ATTEMPT, stage=STAGE, job_id=JOB, head_sha=SHA, tree=TREE,
                  artifact_id=ARTIFACT, artifact_digest=DIGEST, version=VERSION, platform="macos", arch="x64",
                  preparation_key=KEY, source_identity=SOURCE_IDENTITY, recipes=recipe, state=state,
                  continuation_stages="1..8 after donor stage7", changed_files=[])
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_bytes(arp._json(result))
    return result


def continuation(repo, work, stage, phase):
    require(1 <= stage <= 8, "continuation stage must be 1..8")
    require(os.environ.get("CHROMIX_BUILD_PROFILE") == "fast"
            and os.environ.get("CHROMIX_USE_UPSTREAM_CACHE") == "1",
            "continuation requires fast profile and mandatory restored cache")
    receipt = json.loads(read(work, RECEIPT))
    expected = dict(status="verified", operation="unchanged-stage7-continuation", repository=REPOSITORY,
                    run_id=RUN, attempt=ATTEMPT, stage=STAGE, job_id=JOB, head_sha=SHA, tree=TREE,
                    artifact_id=ARTIFACT, artifact_digest=DIGEST, version=VERSION, platform="macos", arch="x64",
                    preparation_key=KEY, source_identity=SOURCE_IDENTITY,
                    continuation_stages="1..8 after donor stage7", changed_files=[])
    require(all(receipt.get(key) == value for key, value in expected.items()),
            "missing or changed stage7 continuation provenance")
    require(recipes(repo) == receipt.get("recipes"), "admitted build recipes changed")
    state = source_state(repo, work)
    if stage == 1 and phase == "before":
        require(state == receipt.get("state"), "initial Ninja state changed before continuation")
    output = work / "fingerprint-diagnostics" / (
        f"mac155-stage7-continuation-s{stage}-{phase}-attempt-{os.environ.get('GITHUB_RUN_ATTEMPT', 'local')}.json")
    live_source(repo, work, output.with_name(output.stem + "-source.json"))
    result = dict(expected, continuation_stage=stage, logical_stage=STAGE + stage, phase=phase,
                  continuation_run=os.environ.get("GITHUB_RUN_ID"),
                  continuation_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"), state=state)
    output.write_bytes(arp._json(result))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--stage", type=int, required=True)
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    args = parser.parse_args(argv)
    try:
        continuation(args.repo.resolve(), args.workdir.resolve(), args.stage, args.phase)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"mac155 stage7 continuation rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
