#!/usr/bin/env python3
"""Admit the exact macOS 156 Intel stage8 donor without changing its build identity."""
from __future__ import annotations

import argparse
import re
import shutil
import json
import os
from pathlib import Path
import subprocess
import sys

try:
    from .validate_macos156_arm64_stage8_checkpoint import (
        require, sha, identity_hash, read, recipe_bytes, output_inventory, frozen_ninja)
    from . import apply_restored_patches as arp
    from . import migrate_restored_snapshot as migration
    from . import validate_posix_snapshot as snapshot
    from . import prepare_restored_build as prepared
    from . import restore_ninja, macos_runtime, macos_restored_generators
except ImportError:
    from validate_macos156_arm64_stage8_checkpoint import (
        require, sha, identity_hash, read, recipe_bytes, output_inventory, frozen_ninja)
    import apply_restored_patches as arp
    import migrate_restored_snapshot as migration
    import validate_posix_snapshot as snapshot
    import prepare_restored_build as prepared
    import restore_ninja, macos_runtime, macos_restored_generators

REPOSITORY = "xiaozhou26/Chromix"
RUN = 42455177686
JOB = 129225688830
STAGE = 8
ATTEMPT = 2
SHA = "2e2a85fdeb42f37a7528a7d1ced247deab337452"
TREE = "45453c3968522416c696ea431f93db835890d7d2"
BRANCH = "main"
VERSION = "156.0.8078.79"
ARTIFACT = 13701073762
SIZE = 8423029662
DIGEST = "sha256:109b96ef9a38c34145de46c2b11ef6e7eb809c42e5df9721b0675be06e821fc7"
KEY = ("macos|x64|156.0.8078.79|28a211ab4ab6b83d919966a4f58a6df23cf4fb29|"
       "31f01756a43d55b2c4211e73ca329bfab4ebfcb2|"
       "0c828be269b7a653ffcec6ff9392346a72f6f96eb9716792c270bd9240b17914")
SOURCE_IDENTITY = "c92c9cd3507915524acee33a5a8feaae7987b391c5e183a57e46a21ae56277b0"
SOURCE_OUTPUTS = "3bfbbf5889a11bd7744c712e5c16a0f34d5c196c0ec154c4600e794e7485722e"
UPSTREAM_IDENTITY = "7840a85e9ba0a872feb9bac55128749af97a2c4e7e1fdfa78ee94d7c466b95ae"
RECEIPT = "fingerprint-diagnostics/mac156-x64-stage8-resume.json"
PINNED_FILES = {
    "src/out/Default/args.gn": "1dc792400a3fdf9f787b2068039caf246843361f3eb8deb46de1fd6e6bba6eba",
    "src/.chromix-upstream-restored.json": "67443e6bddbc0a6cdd1b4cfeae7f6f5882d3f8004a8575de6804bce6e4207da4",
    "src/.chromix-restored-patches.json": "ebc244dc51b54f30d55f842536b4ae243fc0c89697e44c72eb2e6241d389c5e5",
    "upstream-cache-restore.json": "e9ab3571c99c4cfc950e03ab9d02e0ec7ae385d8084bac482bcc4e7f9916444b",
    "fingerprint-diagnostics/source-final.json": "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
}
PREPARATION_HASH = "6cff202b10958e453ba3dd369d81727ee6c1dc08d8b85bee7fabec43cd52bcee"
STATE_FILES = ("out/Default/build.ninja", "out/Default/.ninja_log", "out/Default/.ninja_deps")
RECIPE_FILES = (
    *migration.PIN_FILES, *migration.SCRIPT_FILES, "patches/series",
    "build/macos/build.sh", "build/args.macos.gn", "build/posix/upstream-cache.sh",
    "build/posix/prepare-restored-tools.sh", "tools/prepare_restored_build.py",
    "tools/restore_upstream_cache.py", "tools/merge_gn_args.py", "tools/bootstrap_gn.py",
    "tools/verify_patch_stack.py", "tools/restore_ninja.py", "tools/macos_runtime.py",
    "tools/install_macos_generators.py", "tools/macos_restored_generators.py",
    "build/macos/select-xcode.sh", "build/macos/package-macos.sh", "tools/verify_macos_bundle.py",
    "tools/macos_browser_smoke.py", "tools/fingerprint_acceptance.py",
)


JOB_LOG_HASH = "8821b4c7719a3cc2ece5901fe8df082b95f166cf13df5b64a1ceb7db993f270d"
JOB_LOG_SIZE = 715091


def producer_log():
    result = subprocess.run(
        ["gh", "api", "--allow-escape-sequences", f"repos/{REPOSITORY}/actions/jobs/{JOB}/logs"],
        check=True, capture_output=True, timeout=120)
    return result.stdout


def validate_metadata(client, repository, artifact_ids, *, log_loader=producer_log):
    require(repository == REPOSITORY and artifact_ids == [ARTIFACT],
            "exact mac156 Intel stage8 repository/artifact required")
    run = client.get(f"/actions/runs/{RUN}")
    require(type(run.get("run_attempt")) is int, "integer producer attempt required")
    require(run.get("id") == RUN and run.get("head_sha") == SHA
            and run.get("path") == ".github/workflows/build-macos-x64.yml"
            and run.get("head_branch") == BRANCH and run.get("event") == "workflow_dispatch"
            and run.get("run_attempt") == ATTEMPT and run.get("status") == "completed"
            and run.get("conclusion") == "failure"
            and run.get("repository", {}).get("full_name") == REPOSITORY
            and run.get("head_repository", {}).get("full_name") == REPOSITORY,
            "exact failed manual Intel attempt2 required")
    job = client.get(f"/actions/jobs/{JOB}")
    require(type(job.get("run_attempt")) is int, "integer job attempt required")
    require(job.get("id") == JOB and job.get("run_id") == RUN
            and job.get("head_sha") == SHA and job.get("run_attempt") == ATTEMPT
            and job.get("head_branch") == BRANCH and job.get("status") == "completed"
            and job.get("conclusion") == "failure" and job.get("labels") == ["macos-26-intel"]
            and job.get("name") == ("macOS Intel (8-stage snapshot/resume) / "
                                    "macos-x64 stage 8 (resume compile)"),
            "recorded native Intel stage8 producer job mismatch")
    steps = {step["name"]: step for step in job.get("steps", [])}
    for name, conclusion in {"Run stage 8": "failure", "Verify handoff snapshot": "success",
                             "Preserve latest macOS compiled checkpoint": "skipped",
                             "Upload failed macOS runtime bundle": "skipped"}.items():
        require(steps.get(name, {}).get("status") == "completed"
                and steps[name].get("conclusion") == conclusion, f"producer step mismatch: {name}")
    for part in range(1, 5):
        step = steps.get(f"Upload tree part {part}", {})
        require((step.get("status"), step.get("conclusion")) == ("completed", "success"),
                f"incomplete upload step: part{part}")
    raw = log_loader()
    require(len(raw) == JOB_LOG_SIZE and sha(raw) == JOB_LOG_HASH,
            "exact producer job log size/SHA-256 required")
    for evidence in (b"stage 8 reached max-stages 8 without finishing",
                     b"Artifact chromix-mac-x64-tree-s8-attempt-2-part1.zip successfully finalized. Artifact ID 13701073762",
                     b"Final size is 8423029662 bytes. Artifact ID is 13701073762"):
        require(evidence in raw, "producer log lacks exact exhaustion/finalization evidence")
    require(b"packed 8423029404 bytes as 1 volumes (streaming)" in raw,
            "exact single packed volume required")
    for part in range(2, 5):
        require(f".snapshot-stage-8/p{part}/. No artifacts will be uploaded.".encode() in raw,
                "empty upload evidence missing")
    prefix = "chromix-mac-x64-tree-s8-attempt-2-part"
    artifacts = [item for item in client.items(f"/actions/runs/{RUN}/artifacts", "artifacts")
                 if item.get("name", "").startswith(prefix)]
    require(len(artifacts) == 1, "complete exact Intel stage8 artifact set required")
    artifact = artifacts[0]
    require(artifact.get("expired") is False, "artifact must be unexpired")
    require((artifact.get("id"), artifact.get("name"), artifact.get("size_in_bytes"),
             artifact.get("digest"), artifact.get("expired")) ==
            (ARTIFACT, prefix + "1", SIZE, DIGEST, False),
            "Intel stage8 artifact ID/name/size/SHA-256/expiry mismatch")
    origin = artifact.get("workflow_run", {})
    require(origin.get("id") == RUN and origin.get("head_sha") == SHA
            and origin.get("head_branch") == BRANCH, "Intel artifact origin mismatch")
    return expected_manifest()


def expected_manifest():
    return dict(repository=REPOSITORY, platform="macos", arch="x64", workflow="build-macos-x64",
                run_id=RUN, attempt=ATTEMPT, stage=STAGE, head_sha=SHA, job_id=JOB,
                pattern="chromix-mac-x64-tree-s8-attempt-2-part*",
                artifacts=[dict(id=ARTIFACT, name="chromix-mac-x64-tree-s8-attempt-2-part1",
                                size_in_bytes=SIZE, digest=DIGEST, expired=False)])


def download_identity(origin, report):
    manifest = json.loads(origin.read_bytes())
    require(json.dumps(manifest, sort_keys=True) == json.dumps(expected_manifest(), sort_keys=True),
            "exact donor manifest required")
    data = json.loads(report.read_bytes())
    require((data.get("status"), data.get("publication"), data.get("phase")) ==
            ("success", "published", "complete"), "download must be verified and published")
    for key in ("repository", "head_sha", "run_id"):
        require(data.get(key) == manifest[key], "download origin mismatch: " + key)
    artifacts = data.get("artifacts", [])
    require(len(artifacts) == 1 and all(artifacts[0].get(k) == v for k, v in manifest["artifacts"][0].items()),
            "download artifact mismatch")
    attempts = artifacts[0].get("attempts", [])
    require(attempts and attempts[-1].get("status") == "success"
            and attempts[-1].get("size_in_bytes") == SIZE
            and "sha256:" + attempts[-1].get("sha256", "") == DIGEST,
            "download digest/size evidence missing")
    volumes = data.get("volumes", [])
    require(len(volumes) == 1 and volumes[0].get("name") == "tree.tar.zst.001"
            and volumes[0].get("artifact_id") == ARTIFACT
            and volumes[0].get("size_in_bytes") == 8423029404
            and re.fullmatch(r"[0-9a-f]{64}", volumes[0].get("sha256", ""))
            and data.get("total_size_in_bytes") == 8423029404, "single donor volume mismatch")
    return dict(manifest=manifest, volume=volumes[0], producer_log_sha256=JOB_LOG_HASH,
                producer_log_size=JOB_LOG_SIZE)


def recipes(repo, donor=None):
    require(migration.source_ready_key(repo, "macos", "x64") == KEY,
            "donor preparation key changed; migration/cold fallback forbidden")
    result = {}
    for name in dict.fromkeys(RECIPE_FILES):
        data = recipe_bytes(name, read(repo, name))
        if donor is not None:
            original = read(donor, name)
            require(original == migration._donor_git(donor, repo, "cat-file", "blob", SHA + ":" + name),
                    "dirty donor recipe: " + name)
            require(data == recipe_bytes(name, original), f"preparation/build recipe changed: {name}")
        result[name] = sha(data)
    return result


def preparation(work):
    for name in ("upstream-cache-preparation.json", "src/.chromix-restored-build-prepared.json"):
        raw = read(work, name)
        require(sha(raw) == PREPARATION_HASH, f"x64-stage8 preparation receipt mismatch: {name}")
        data = json.loads(raw)
        require((data.get("platform"), data.get("arch")) == ("macos", "x64")
                and data.get("ready_for_gn") is True
                and data.get("host") == {"platform": "macos", "arch": "x64"}
                and all(data.get(field) is True for field in
                        ("native_tools", "compilers_native", "toolchains_native"))
                and identity_hash(data.get("source_identity")) == UPSTREAM_IDENTITY,
                "prepared native mac156 Intel source identity required")
        require(all(data.get("counters", {}).get(field) == 0 for field in
                    ("first_finish", "tool_swap_invalidations", "toolchain_invalidated_outputs")),
                "full preparation/toolchain/output invalidation forbidden")


def source_state(repo, work):
    src = work / "src"
    for name in (migration.TRANSACTION, arp.IN_PROGRESS):
        require(not (src / name).exists(), f"interrupted source operation: {name}")
    migration._ready(src, KEY)
    require(migration.source_ready_key(repo, "macos", "x64") == KEY, "preparation key changed")
    require(arp.patch_selection.source_version(src) == VERSION, "source version changed")
    for name, digest in PINNED_FILES.items():
        require(sha(read(work, name)) == digest, f"x64-stage8 source/GN receipt changed: {name}")
    read(src, ".chromix-toolchain-ready")
    read(src, ".chromix-domain-substituted")
    state = {}
    for name in STATE_FILES:
        data = read(src, name)
        require(bool(data), f"missing incremental build state: {name}")
        state[name] = sha(data)
    graph = {}
    for path in sorted((src / "out/Default").rglob("*.ninja")):
        name = path.relative_to(src).as_posix()
        graph[name] = sha(read(src, name))
    state["graph"] = identity_hash(graph)
    preparation(work)
    return state


def live_source(repo, work, output):
    require(not output.exists(), "live source report must be new")
    subprocess.run([sys.executable, str(repo / "tools/verify_patch_stack.py"),
                    "--src", str(work / "src"), "--repo", str(repo),
                    "--core", str(work / "tooling/ungoogled-chromium"),
                    "--platform-tooling", str(work / "tooling/ungoogled-chromium-macos"),
                    "--platform", "macos", "--output", str(output)], check=True)
    receipt = json.loads(output.read_text())
    require(receipt.get("status") == "verified" and receipt.get("patch_count") == 226
            and receipt.get("domain_substituted") is True
            and identity_hash(receipt.get("identity")) == SOURCE_IDENTITY
            and identity_hash(receipt.get("outputs")) == SOURCE_OUTPUTS,
            "live mac156 source patch identity/output hashes differ from x64-stage8")


def admit(work, donor, repo, origin, download_report):
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", "")), "continuation run ID required")
    provenance = download_identity(origin, download_report)
    require(migration._donor_git(donor, repo, "rev-parse", "HEAD").decode().strip() == SHA,
            "exact x64-stage8 donor checkout required")
    require(migration._donor_git(donor, repo, "rev-parse", "HEAD^{tree}").decode().strip() == TREE,
            "x64-stage8 donor tree mismatch")
    require(not migration._donor_git(donor, repo, "status", "--porcelain", "--untracked-files=all"),
            "donor checkout must be clean")
    recipe = recipes(repo, donor)
    old, old_patches = arp.patch_selection.select(donor, "macos")
    current, current_patches = arp.patch_selection.select(repo, "macos")
    require(len(current_patches) == 226 and (old, old_patches) == (current, current_patches),
            "macOS recipe must retain the exact selected 226 patches")
    for name, raw in old_patches:
        require(raw == migration._donor_git(donor, repo, "cat-file", "blob", SHA + ":" + name),
                "donor patch bytes changed: " + name)
    require(migration.source_ready_key(donor, "macos", "x64") == KEY, "donor prepare key mismatch")
    state = source_state(repo, work)
    inventory = output_inventory(work / "src/out/Default")
    native_state(repo, work)
    migration.restore.verify_restored(work, "macos", "x64", repo=repo)
    migration._verify_tooling(work, repo, (work, donor, repo), "macos")
    report = work / RECEIPT
    require(not report.exists(), "donor must not contain a continuation receipt")
    live_source(repo, work, report.with_name("mac156-x64-stage8-source-admission.json"))
    require(source_state(repo, work) == state, "admission changed donor build state")
    require(output_inventory(work / "src/out/Default") == inventory, "admission modified output tree")
    result = dict(status="verified", operation="unchanged-mac156-x64-stage8-continuation", repository=REPOSITORY,
                  run_id=RUN, attempt=ATTEMPT, stage=STAGE, job_id=JOB, head_sha=SHA, tree=TREE,
                  artifact_id=ARTIFACT, artifact_digest=DIGEST, artifact_size=SIZE, version=VERSION, platform="macos", arch="x64",
                  preparation_key=KEY, source_identity=SOURCE_IDENTITY, recipes=recipe, state=state,
                  producer_log_sha256=JOB_LOG_HASH, provenance=provenance,
                  continuation_run=os.environ.get("GITHUB_RUN_ID"),
                  continuation_stages="1..8 after donor stage8 (logical 9..16)", changed_files=[])
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_bytes(arp._json(result))
    return result


def continuation(repo, work, stage, phase):
    require(phase in ("before", "after"), "invalid continuation phase")
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", "")), "continuation run ID required")
    require(1 <= stage <= 8, "continuation stage must be 1..8")
    require(os.environ.get("CHROMIX_BUILD_PROFILE") == "fast"
            and os.environ.get("CHROMIX_USE_UPSTREAM_CACHE") == "1",
            "continuation requires fast profile and mandatory restored cache")
    receipt = json.loads(read(work, RECEIPT))
    expected = dict(status="verified", operation="unchanged-mac156-x64-stage8-continuation", repository=REPOSITORY,
                    run_id=RUN, attempt=ATTEMPT, stage=STAGE, job_id=JOB, head_sha=SHA, tree=TREE,
                    artifact_id=ARTIFACT, artifact_digest=DIGEST, artifact_size=SIZE, version=VERSION, platform="macos", arch="x64",
                    preparation_key=KEY, source_identity=SOURCE_IDENTITY, producer_log_sha256=JOB_LOG_HASH,
                    continuation_stages="1..8 after donor stage8 (logical 9..16)", changed_files=[])
    require(all(receipt.get(key) == value for key, value in expected.items()),
            "missing or changed x64-stage8 continuation provenance")
    require(receipt.get("continuation_run") == os.environ.get("GITHUB_RUN_ID")
            and receipt.get("provenance", {}).get("manifest") == expected_manifest(),
            "continuation run/donor isolation mismatch")
    require(recipes(repo) == receipt.get("recipes"), "admitted build recipes changed")
    state = source_state(repo, work)
    require(state["graph"] == receipt["state"]["graph"],
            "GN graph changed; regeneration forbidden")
    if phase == "before":
        native_state(repo, work)
    if stage == 1 and phase == "before":
        require(state == receipt.get("state"), "initial Ninja state changed before continuation")
    if phase == "before" and stage > 1:
        previous = list((work / "fingerprint-diagnostics").glob(
            f"mac156-x64-stage8-continuation-s{stage - 1}-after-attempt-*.json"))
        require(len(previous) == 1, "exact previous continuation handoff required")
        handoff = json.loads(previous[0].read_bytes())
        require(handoff.get("continuation_run") == os.environ.get("GITHUB_RUN_ID")
                and handoff.get("logical_stage") == STAGE + stage - 1
                and handoff.get("state") == state, "previous continuation state mismatch")
    output = work / "fingerprint-diagnostics" / (
        f"mac156-x64-stage8-continuation-s{stage}-{phase}-attempt-{os.environ.get('GITHUB_RUN_ATTEMPT', 'local')}.json")
    live_source(repo, work, output.with_name(output.stem + "-source.json"))
    result = dict(expected, continuation_stage=stage, logical_stage=STAGE + stage, phase=phase,
                  continuation_run=os.environ.get("GITHUB_RUN_ID"),
                  continuation_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"), state=state)
    output.write_bytes(arp._json(result))
    return result


def native_state(repo, work):
    src = work / "src"
    old = json.loads(read(work, "upstream-cache-preparation.json"))
    require(prepared.host_identity() == ("macos", "x64"), "native macOS Intel required")
    require(prepared.inspect_native_tools(src, "macos", "x64")["native_tools"],
            "prepared native tools no longer executable")
    require(prepared.environments_compatible(old["environment"], prepared.environment_identity(src, "macos")),
            "SDK/toolchain environment changed; no prepare fallback")
    require(prepared.tool_fingerprint(src, "macos", "x64") == old["tool_fingerprint"],
            "compiler/toolchain bytes changed")
    generators = prepared.generator_fingerprint(src, "macos", "x64")
    generators["macos154"] = macos_restored_generators.prepare(src, host_arch="x64", repo=repo)
    require(generators == old["generator_fingerprint"], "generator preimage changed")
    macos_runtime.verify_runtime_loader(src, "x64")
    require(read(src, "out/Default/.ninja_log").startswith(b"# ninja log v7\n"), "Ninja log format changed")
    require(read(src, "out/Default/.ninja_deps").startswith(b"# ninjadeps\n\x04\x00\x00\x00"),
            "Ninja dependency format changed")


def compile_tree(repo, work, stage):
    receipt = json.loads(read(work, RECEIPT))
    require(receipt.get("continuation_run") == os.environ.get("GITHUB_RUN_ID"), "unadmitted compile")
    require(stage is not None and 1 <= stage <= 8, "compile stage required")
    before = json.loads(read(work, f"fingerprint-diagnostics/mac156-x64-stage8-continuation-s{stage}"
                            f"-before-attempt-{os.environ.get('GITHUB_RUN_ATTEMPT', 'local')}.json"))
    require(before.get("operation") == "unchanged-mac156-x64-stage8-continuation"
            and before.get("continuation_run") == os.environ.get("GITHUB_RUN_ID")
            and before.get("logical_stage") == STAGE + stage
            and before.get("state") == source_state(repo, work), "compile requires same-attempt read-only gate")
    ninja = Path(shutil.which("ninja") or "missing").resolve()
    require(restore_ninja.host_identity() == ("macos", "x64")
            and restore_ninja.binary_architectures(ninja, "macos") == {"x64"}
            and restore_ninja.probe_version(ninja, work) == "1.13.2", "exact native Ninja 1.13.2 required")
    jobs = os.environ.get("CHROMIX_JOBS", "")
    require(re.fullmatch(r"[1-9][0-9]*", jobs), "positive compile job count required")
    env = {key: value for key, value in os.environ.items() if not key.startswith("DYLD_")}
    return frozen_ninja(ninja, work, jobs, env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("metadata", "admit", "check", "compile"), required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--previous-repo", type=Path)
    parser.add_argument("--origin", type=Path)
    parser.add_argument("--download-report", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--stage", type=int)
    parser.add_argument("--phase", choices=("before", "after"))
    args = parser.parse_args(argv)
    try:
        if args.mode == "metadata":
            require(args.report is not None, "metadata report required")
            repository = os.environ.get("GITHUB_REPOSITORY", REPOSITORY)
            result = validate_metadata(snapshot.Client(repository, os.environ.get("GH_TOKEN", "")),
                                       repository, [ARTIFACT])
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_bytes(arp._json(result))
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(f"head_sha={SHA}\npattern={result['pattern']}\n")
        else:
            require(args.workdir is not None, "workdir required")
            work = args.workdir.resolve()
            repo = args.repo.resolve()
            if args.mode == "admit":
                require(all((args.previous_repo, args.origin, args.download_report)), "admission provenance required")
                admit(work, args.previous_repo.resolve(), repo, args.origin, args.download_report)
            elif args.mode == "compile":
                return compile_tree(repo, work, args.stage)
            else:
                require(args.stage is not None and args.phase is not None, "stage and phase required")
                continuation(repo, work, args.stage, args.phase)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"mac156 x64-stage8 continuation rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
