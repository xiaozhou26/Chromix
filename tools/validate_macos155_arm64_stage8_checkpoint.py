#!/usr/bin/env python3
"""Admit the exact macOS 155 ARM64 stage8 donor without changing its build identity."""
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
except ImportError:
    import apply_restored_patches as arp
    import migrate_restored_snapshot as migration

REPOSITORY = "xiaozhou26/Chromix"
WORKFLOW = "build-mac155-arm64-stage8-resume"
RUN = 39104897472
JOB = 118034411322
STAGE = 8
ATTEMPT = 3
SHA = "a436049a48c9a84cdb83297f7ada1dad911cda7b"
TREE = "a89b1b52b7d35f32db7982f213af6aebdeeb351c"
BRANCH = "build/macos-new-donors"
VERSION = "155.0.8059.39"
ARTIFACT = 12199650604
SIZE = 9570134092
DIGEST = "sha256:e636f8c9e2e5553df2dca616529df1e59897581809b7bc9ded36d0a7c9c69914"
KEY = ("macos|arm64|155.0.8059.39|ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a|"
       "b1238bb72b02cd36219991d9029c1c65183ea295|"
       "0962b146a7bf1d19e0a4cfa92277924cb829fd75fc8d0d96d69a896e344dbe4a")
SOURCE_IDENTITY = "28d28ee38e0c18cc73e492a604badcbfebd91dbc6040fe2d554a688492248697"
SOURCE_OUTPUTS = "42b460cb4558a901f8bd7d3faff62d02b29927edcb95cbed1238c5c2e22b082e"
UPSTREAM_IDENTITY = "e90e181993cde786eeec09036ced5283a8401dc86520ec1f699cc6c39f4acc0d"
RECEIPT = "fingerprint-diagnostics/mac155-arm64-stage8-resume.json"
PINNED_FILES = {
    "src/out/Default/args.gn": "aca4229e636c0cfff25e7fe0e8bfae588ce82fef5a0a8a7cff4b8333aa59e750",
    "src/.chromix-upstream-restored.json": "752abe8e2e542f9c79308f6a27e9f1d07f540957cdb283680100f2c837061cbd",
    "src/.chromix-restored-patches.json": "d3736f61b2f273ddb677f29aa734efd7c54fa0381a9e4b4c29a57d73d83a4ca3",
    "fingerprint-diagnostics/source-final.json": "b53ba63cc6a17ec4c8c26a4e2f898604cb30f54e219c26fa7df0a5a7136cccc4",
}
PREPARATION_HASH = "5d3ea69bcda4023e911db5e80ec8d7c420ace0df38a67db615572078f62e8c59"
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


JOB_LOG_HASH = "19a730ce9e9229e72efed6c9b147982dd6ac555aacf1e3f1e338a81cf9c1e3d1"
JOB_LOG_SIZE = 414601


def producer_log():
    result = subprocess.run(
        ["gh", "api", "--allow-escape-sequences", f"repos/{REPOSITORY}/actions/jobs/{JOB}/logs"],
        check=True, capture_output=True, timeout=120)
    return result.stdout


def validate_metadata(client, repository, artifact_ids, *, log_loader=producer_log):
    require(repository == REPOSITORY and artifact_ids == [ARTIFACT],
            "exact mac155 ARM64 stage8 repository/artifact required")
    run = client.get(f"/actions/runs/{RUN}")
    require(run.get("id") == RUN and run.get("head_sha") == SHA
            and run.get("path") == ".github/workflows/build-macos-arm64.yml"
            and run.get("head_branch") == BRANCH and run.get("event") == "workflow_dispatch"
            and run.get("run_attempt") == ATTEMPT and run.get("status") == "completed"
            and run.get("conclusion") == "failure"
            and run.get("repository", {}).get("full_name") == REPOSITORY
            and run.get("head_repository", {}).get("full_name") == REPOSITORY,
            "exact failed manual ARM64 attempt3 required")
    job = client.get(f"/actions/jobs/{JOB}")
    require(job.get("id") == JOB and job.get("run_id") == RUN
            and job.get("head_sha") == SHA and job.get("run_attempt") == ATTEMPT
            and job.get("head_branch") == BRANCH and job.get("status") == "completed"
            and job.get("conclusion") == "failure" and job.get("labels") == ["macos-15"]
            and job.get("name") == ("macOS Apple Silicon (8-stage snapshot/resume) / "
                                    "macos-arm64 stage 8 (resume compile)"),
            "recorded native ARM64 stage8 producer job mismatch")
    steps = {step["name"]: step for step in job.get("steps", [])}
    for name, conclusion in {"Run stage 8": "failure", "Verify handoff snapshot": "success",
                             "Preserve latest macOS compiled checkpoint": "skipped",
                             "Upload failed macOS runtime bundle": "skipped"}.items():
        require(steps.get(name, {}).get("status") == "completed"
                and steps[name].get("conclusion") == conclusion, f"producer step mismatch: {name}")
    # This exact terminal job has stale upload-step API state; pinned artifact hashes
    # and the complete pinned job log prove finalization without rewriting that state.
    for part in range(1, 5):
        step = steps.get(f"Upload tree part {part}", {})
        observed = (step.get("status"), step.get("conclusion"))
        require(observed in (("completed", "success"),
                             ("in_progress" if part == 1 else "pending", None)),
                f"unexpected recorded upload state: part{part}")
    raw = log_loader()
    require(len(raw) == JOB_LOG_SIZE and sha(raw) == JOB_LOG_HASH,
            "exact producer job log size/SHA-256 required")
    for evidence in (b"stage 8 reached max-stages 8 without finishing",
                     b"Artifact chromix-mac-arm64-tree-s8-attempt-3-part1.zip successfully finalized. Artifact ID 12199650604",
                     b"Final size is 9570134092 bytes. Artifact ID is 12199650604"):
        require(evidence in raw, "producer log lacks exact exhaustion/finalization evidence")
    prefix = "chromix-mac-arm64-tree-s8-attempt-3-part"
    artifacts = [item for item in client.items(f"/actions/runs/{RUN}/artifacts", "artifacts")
                 if item.get("name", "").startswith(prefix)]
    require(len(artifacts) == 1, "complete exact ARM64 stage8 artifact set required")
    artifact = artifacts[0]
    require((artifact.get("id"), artifact.get("name"), artifact.get("size_in_bytes"),
             artifact.get("digest"), artifact.get("expired")) ==
            (ARTIFACT, prefix + "1", SIZE, DIGEST, False),
            "ARM64 stage8 artifact ID/name/size/SHA-256/expiry mismatch")
    origin = artifact.get("workflow_run", {})
    require(origin.get("id") == RUN and origin.get("head_sha") == SHA
            and origin.get("head_branch") == BRANCH, "ARM64 artifact origin mismatch")
    return dict(repository=REPOSITORY, platform="macos", arch="arm64", workflow="build-macos-arm64",
                run_id=RUN, attempt=ATTEMPT, stage=STAGE, head_sha=SHA, job_id=JOB,
                pattern=prefix + "*", artifacts=[{key: artifact[key] for key in
                ("id", "name", "size_in_bytes", "digest", "expired")}], version=VERSION,
                source_branch=BRANCH, source_tree=TREE, producer_job=job,
                producer_log_sha256=JOB_LOG_HASH, producer_log_size=JOB_LOG_SIZE,
                continuation_stages="1..8 after donor stage8 (logical 9..16)")


def recipes(repo, donor=None):
    require(migration.source_ready_key(repo, "macos", "arm64") == KEY,
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
            require(sha(raw) == PREPARATION_HASH, f"arm64-stage8 preparation receipt mismatch: {name}")
        data = json.loads(raw)
        require((data.get("platform"), data.get("arch")) == ("macos", "arm64")
                and data.get("ready_for_gn") is True
                and data.get("host") == {"platform": "macos", "arch": "arm64"}
                and all(data.get(field) is True for field in
                        ("native_tools", "compilers_native", "toolchains_native"))
                and identity_hash(data.get("source_identity")) == UPSTREAM_IDENTITY,
                "prepared native mac155 ARM64 source identity required")
        require(all(data.get("counters", {}).get(field) == 0 for field in
                    ("first_finish", "tool_swap_invalidations", "toolchain_invalidated_outputs")),
                "full preparation/toolchain/output invalidation forbidden")


def source_state(repo, work, *, initial=False):
    src = work / "src"
    for name in (migration.TRANSACTION, arp.IN_PROGRESS):
        require(not (src / name).exists(), f"interrupted source operation: {name}")
    migration._ready(src, KEY)
    require(migration.source_ready_key(repo, "macos", "arm64") == KEY, "preparation key changed")
    require(arp.patch_selection.source_version(src) == VERSION, "source version changed")
    for name, digest in PINNED_FILES.items():
        require(sha(read(work, name)) == digest, f"arm64-stage8 source/GN receipt changed: {name}")
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
            "live mac155 source patch identity/output hashes differ from arm64-stage8")


def admit(work, donor, repo):
    require(migration._donor_git(donor, repo, "rev-parse", "HEAD").decode().strip() == SHA,
            "exact arm64-stage8 donor checkout required")
    require(migration._donor_git(donor, repo, "rev-parse", "HEAD^{tree}").decode().strip() == TREE,
            "arm64-stage8 donor tree mismatch")
    require(not migration._donor_git(donor, repo, "status", "--porcelain", "--untracked-files=all"),
            "donor checkout must be clean")
    recipe = recipes(repo, donor)
    require(migration.source_ready_key(donor, "macos", "arm64") == KEY, "donor prepare key mismatch")
    state = source_state(repo, work, initial=True)
    migration.restore.verify_restored(work, "macos", "arm64", repo=repo)
    migration._verify_tooling(work, repo, (work, donor, repo), "macos")
    report = work / RECEIPT
    require(not report.exists(), "donor must not contain a continuation receipt")
    live_source(repo, work, report.with_name("mac155-arm64-stage8-source-admission.json"))
    require(source_state(repo, work, initial=True) == state, "admission changed donor build state")
    result = dict(status="verified", operation="unchanged-arm64-stage8-continuation", repository=REPOSITORY,
                  run_id=RUN, attempt=ATTEMPT, stage=STAGE, job_id=JOB, head_sha=SHA, tree=TREE,
                  artifact_id=ARTIFACT, artifact_digest=DIGEST, version=VERSION, platform="macos", arch="arm64",
                  preparation_key=KEY, source_identity=SOURCE_IDENTITY, recipes=recipe, state=state,
                  producer_log_sha256=JOB_LOG_HASH,
                  continuation_stages="1..8 after donor stage8 (logical 9..16)", changed_files=[])
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_bytes(arp._json(result))
    return result


def continuation(repo, work, stage, phase):
    require(1 <= stage <= 8, "continuation stage must be 1..8")
    require(os.environ.get("CHROMIX_BUILD_PROFILE") == "fast"
            and os.environ.get("CHROMIX_USE_UPSTREAM_CACHE") == "1",
            "continuation requires fast profile and mandatory restored cache")
    receipt = json.loads(read(work, RECEIPT))
    expected = dict(status="verified", operation="unchanged-arm64-stage8-continuation", repository=REPOSITORY,
                    run_id=RUN, attempt=ATTEMPT, stage=STAGE, job_id=JOB, head_sha=SHA, tree=TREE,
                    artifact_id=ARTIFACT, artifact_digest=DIGEST, version=VERSION, platform="macos", arch="arm64",
                    preparation_key=KEY, source_identity=SOURCE_IDENTITY, producer_log_sha256=JOB_LOG_HASH,
                    continuation_stages="1..8 after donor stage8 (logical 9..16)", changed_files=[])
    require(all(receipt.get(key) == value for key, value in expected.items()),
            "missing or changed arm64-stage8 continuation provenance")
    require(recipes(repo) == receipt.get("recipes"), "admitted build recipes changed")
    state = source_state(repo, work)
    if stage == 1 and phase == "before":
        require(state == receipt.get("state"), "initial Ninja state changed before continuation")
    output = work / "fingerprint-diagnostics" / (
        f"mac155-arm64-stage8-continuation-s{stage}-{phase}-attempt-{os.environ.get('GITHUB_RUN_ATTEMPT', 'local')}.json")
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
        print(f"mac155 arm64-stage8 continuation rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
