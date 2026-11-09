#!/usr/bin/env python3
"""Admit the exact macOS 156 ARM64 stage8 donor without changing its build identity."""
from __future__ import annotations

import argparse
import ast
import re
import shutil
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from contextlib import contextmanager
import stat
import ctypes

try:
    from . import apply_restored_patches as arp
    from . import migrate_restored_snapshot as migration
    from . import validate_posix_snapshot as snapshot
    from . import prepare_restored_build as prepared
    from . import restore_ninja, macos_runtime, macos_restored_generators
except ImportError:
    import apply_restored_patches as arp
    import migrate_restored_snapshot as migration
    import validate_posix_snapshot as snapshot
    import prepare_restored_build as prepared
    import restore_ninja, macos_runtime, macos_restored_generators

REPOSITORY = "xiaozhou26/Chromix"
RUN = 42455180503
JOB = 129075816793
STAGE = 8
ATTEMPT = 1
SHA = "2e2a85fdeb42f37a7528a7d1ced247deab337452"
TREE = "45453c3968522416c696ea431f93db835890d7d2"
BRANCH = "main"
VERSION = "156.0.8078.79"
ARTIFACT = 13675184448
SIZE = 9643478210
DIGEST = "sha256:b943ead7eaf8ba1f3b458763fdd42c381f5b9e440ad352448283971181d6da56"
KEY = ("macos|arm64|156.0.8078.79|28a211ab4ab6b83d919966a4f58a6df23cf4fb29|"
       "31f01756a43d55b2c4211e73ca329bfab4ebfcb2|"
       "0c828be269b7a653ffcec6ff9392346a72f6f96eb9716792c270bd9240b17914")
SOURCE_IDENTITY = "c92c9cd3507915524acee33a5a8feaae7987b391c5e183a57e46a21ae56277b0"
SOURCE_OUTPUTS = "3bfbbf5889a11bd7744c712e5c16a0f34d5c196c0ec154c4600e794e7485722e"
UPSTREAM_IDENTITY = "8b35f6dbc681cc52af41e862c1cf8b06339ad86e4e76aa9c65d83a7c941330c0"
RECEIPT = "fingerprint-diagnostics/mac156-arm64-stage8-resume.json"
PINNED_FILES = {
    "src/out/Default/args.gn": "aca4229e636c0cfff25e7fe0e8bfae588ce82fef5a0a8a7cff4b8333aa59e750",
    "src/.chromix-upstream-restored.json": "0aecab3cfb0fa31b836fef917bc4939ef15911667e21b364f930f81aa103a6e3",
    "src/.chromix-restored-patches.json": "ebc244dc51b54f30d55f842536b4ae243fc0c89697e44c72eb2e6241d389c5e5",
    "upstream-cache-restore.json": "dc28f449f55ecb8e4f4814d1a7b65d48bcbddf7f89caac3d0737c1914feb06ce",
    "fingerprint-diagnostics/source-final.json": "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
}
PREPARATION_HASH = "636f4b639fac033869f3ea92588eff7138505ea21052ac8125359986cb332bf6"
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


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def identity_hash(value):
    return sha(arp._json(value))


def read(root, name):
    return arp.patch_selection.read(root, name)


JOB_LOG_HASH = "482d3374465856ea5db15ecdd1506fd4eb7e8259203a6dc0d1bb96157e36a29d"
JOB_LOG_SIZE = 635568


def producer_log():
    result = subprocess.run(
        ["gh", "api", "--allow-escape-sequences", f"repos/{REPOSITORY}/actions/jobs/{JOB}/logs"],
        check=True, capture_output=True, timeout=120)
    return result.stdout


def validate_metadata(client, repository, artifact_ids, *, log_loader=producer_log):
    require(repository == REPOSITORY and artifact_ids == [ARTIFACT],
            "exact mac156 ARM64 stage8 repository/artifact required")
    run = client.get(f"/actions/runs/{RUN}")
    require(type(run.get("run_attempt")) is int, "integer producer attempt required")
    require(run.get("id") == RUN and run.get("head_sha") == SHA
            and run.get("path") == ".github/workflows/build-macos-arm64.yml"
            and run.get("head_branch") == BRANCH and run.get("event") == "workflow_dispatch"
            and run.get("run_attempt") == ATTEMPT and run.get("status") == "completed"
            and run.get("conclusion") == "failure"
            and run.get("repository", {}).get("full_name") == REPOSITORY
            and run.get("head_repository", {}).get("full_name") == REPOSITORY,
            "exact failed manual ARM64 attempt1 required")
    job = client.get(f"/actions/jobs/{JOB}")
    require(type(job.get("run_attempt")) is int, "integer job attempt required")
    require(job.get("id") == JOB and job.get("run_id") == RUN
            and job.get("head_sha") == SHA and job.get("run_attempt") == ATTEMPT
            and job.get("head_branch") == BRANCH and job.get("status") == "completed"
            and job.get("conclusion") == "failure" and job.get("labels") == ["macos-26"]
            and job.get("name") == ("macOS Apple Silicon (8-stage snapshot/resume) / "
                                    "macos-arm64 stage 8 (resume compile)"),
            "recorded native ARM64 stage8 producer job mismatch")
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
                     b"Artifact chromix-mac-arm64-tree-s8-attempt-1-part1.zip successfully finalized. Artifact ID 13675184448",
                     b"Final size is 9643478210 bytes. Artifact ID is 13675184448"):
        require(evidence in raw, "producer log lacks exact exhaustion/finalization evidence")
    require(b"packed 9643477952 bytes as 1 volumes (streaming)" in raw,
            "exact single packed volume required")
    for part in range(2, 5):
        require(f".snapshot-stage-8/p{part}/. No artifacts will be uploaded.".encode() in raw,
                "empty upload evidence missing")
    prefix = "chromix-mac-arm64-tree-s8-attempt-1-part"
    artifacts = [item for item in client.items(f"/actions/runs/{RUN}/artifacts", "artifacts")
                 if item.get("name", "").startswith(prefix)]
    require(len(artifacts) == 1, "complete exact ARM64 stage8 artifact set required")
    artifact = artifacts[0]
    require(artifact.get("expired") is False, "artifact must be unexpired")
    require((artifact.get("id"), artifact.get("name"), artifact.get("size_in_bytes"),
             artifact.get("digest"), artifact.get("expired")) ==
            (ARTIFACT, prefix + "1", SIZE, DIGEST, False),
            "ARM64 stage8 artifact ID/name/size/SHA-256/expiry mismatch")
    origin = artifact.get("workflow_run", {})
    require(origin.get("id") == RUN and origin.get("head_sha") == SHA
            and origin.get("head_branch") == BRANCH, "ARM64 artifact origin mismatch")
    return expected_manifest()


def expected_manifest():
    return dict(repository=REPOSITORY, platform="macos", arch="arm64", workflow="build-macos-arm64",
                run_id=RUN, attempt=ATTEMPT, stage=STAGE, head_sha=SHA, job_id=JOB,
                pattern="chromix-mac-arm64-tree-s8-attempt-1-part*",
                artifacts=[dict(id=ARTIFACT, name="chromix-mac-arm64-tree-s8-attempt-1-part1",
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
            and volumes[0].get("size_in_bytes") == 9643477952
            and re.fullmatch(r"[0-9a-f]{64}", volumes[0].get("sha256", ""))
            and data.get("total_size_in_bytes") == 9643477952, "single donor volume mismatch")
    return dict(manifest=manifest, volume=volumes[0], producer_log_sha256=JOB_LOG_HASH,
                producer_log_size=JOB_LOG_SIZE)


def recipe_bytes(name, data):
    if name != "tools/prepare_restored_build.py":
        return data
    # Only the reviewed Linux TypeScript function differs; no macOS code is omitted.
    require(sha(data) in (
        "7885aa7b0edaaae7019ef1b6938bb37624e672530bfd0787d411a39ad66ed42a",
        "f5e4121b12ba87d5b67a11c0eddcbad216416d3ba7cc19613eb1cfcd3ad8ba11"),
        "unreviewed shared preparation helper")
    tree = ast.parse(data)
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.FunctionDef) and node.name == "prepare_linux_typescript")]
    return ast.dump(tree, include_attributes=False).encode()


def recipes(repo, donor=None):
    require(migration.source_ready_key(repo, "macos", "arm64") == KEY,
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
        require(sha(raw) == PREPARATION_HASH, f"arm64-stage8 preparation receipt mismatch: {name}")
        data = json.loads(raw)
        require((data.get("platform"), data.get("arch")) == ("macos", "arm64")
                and data.get("ready_for_gn") is True
                and data.get("host") == {"platform": "macos", "arch": "arm64"}
                and all(data.get(field) is True for field in
                        ("native_tools", "compilers_native", "toolchains_native"))
                and identity_hash(data.get("source_identity")) == UPSTREAM_IDENTITY,
                "prepared native mac156 ARM64 source identity required")
        require(all(data.get("counters", {}).get(field) == 0 for field in
                    ("first_finish", "tool_swap_invalidations", "toolchain_invalidated_outputs")),
                "full preparation/toolchain/output invalidation forbidden")


def source_state(repo, work):
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
            "live mac156 source patch identity/output hashes differ from arm64-stage8")


def admit(work, donor, repo, origin, download_report):
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", "")), "continuation run ID required")
    provenance = download_identity(origin, download_report)
    require(migration._donor_git(donor, repo, "rev-parse", "HEAD").decode().strip() == SHA,
            "exact arm64-stage8 donor checkout required")
    require(migration._donor_git(donor, repo, "rev-parse", "HEAD^{tree}").decode().strip() == TREE,
            "arm64-stage8 donor tree mismatch")
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
    require(migration.source_ready_key(donor, "macos", "arm64") == KEY, "donor prepare key mismatch")
    state = source_state(repo, work)
    inventory = output_inventory(work / "src/out/Default")
    native_state(repo, work)
    migration.restore.verify_restored(work, "macos", "arm64", repo=repo)
    migration._verify_tooling(work, repo, (work, donor, repo), "macos")
    report = work / RECEIPT
    require(not report.exists(), "donor must not contain a continuation receipt")
    live_source(repo, work, report.with_name("mac156-arm64-stage8-source-admission.json"))
    require(source_state(repo, work) == state, "admission changed donor build state")
    require(output_inventory(work / "src/out/Default") == inventory, "admission modified output tree")
    result = dict(status="verified", operation="unchanged-mac156-arm64-stage8-continuation", repository=REPOSITORY,
                  run_id=RUN, attempt=ATTEMPT, stage=STAGE, job_id=JOB, head_sha=SHA, tree=TREE,
                  artifact_id=ARTIFACT, artifact_digest=DIGEST, artifact_size=SIZE, version=VERSION, platform="macos", arch="arm64",
                  preparation_key=KEY, source_identity=SOURCE_IDENTITY, recipes=recipe, state=state,
                  producer_log_sha256=JOB_LOG_HASH, provenance=provenance,
                  continuation_run=os.environ.get("GITHUB_RUN_ID"),
                  continuation_stages="1..8 after donor stage8 (logical 9..16)", changed_files=[])
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_bytes(arp._json(result))
    return result


def continuation_identity():
    return dict(status="verified", operation="unchanged-mac156-arm64-stage8-continuation", repository=REPOSITORY,
                run_id=RUN, attempt=ATTEMPT, stage=STAGE, job_id=JOB, head_sha=SHA, tree=TREE,
                artifact_id=ARTIFACT, artifact_digest=DIGEST, artifact_size=SIZE, version=VERSION, platform="macos", arch="arm64",
                preparation_key=KEY, source_identity=SOURCE_IDENTITY, producer_log_sha256=JOB_LOG_HASH,
                continuation_stages="1..8 after donor stage8 (logical 9..16)", changed_files=[])


def previous_handoff(work, prefix, stage, base, expected, state):
    directory = work / "fingerprint-diagnostics"
    pattern = re.compile(re.escape(f"{prefix}-s{stage - 1}-after-attempt-") + r"([1-9][0-9]*)\.json")
    # Source attestations share the prefix, but are not handoff receipts.
    previous = [(path, pattern.fullmatch(path.name)) for path in directory.iterdir()]
    previous = [(path, match) for path, match in previous if match]
    require(len(previous) == 1, "exact previous continuation handoff required")
    path, match = previous[0]
    attempt = os.environ.get("CHROMIX_PREVIOUS_ATTEMPT", "")
    consumer = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    require(re.fullmatch(r"[1-9][0-9]*", attempt) and re.fullmatch(r"[1-9][0-9]*", consumer)
            and match[1] == attempt and int(attempt) <= int(consumer),
            "exact previous producer attempt required")
    handoff = json.loads(read(directory, path.name))
    identity = dict(expected, continuation_run=os.environ.get("GITHUB_RUN_ID"),
                    continuation_attempt=attempt, continuation_stage=stage - 1,
                    logical_stage=base + stage - 1, phase="after", state=state)
    require(all(handoff.get(key) == value for key, value in identity.items()),
            "previous continuation state/provenance mismatch")
    return handoff


def continuation(repo, work, stage, phase, *, receipt_name=RECEIPT,
                 prefix="mac156-arm64-stage8-continuation", expected=None, manifest=None, base=STAGE):
    require(phase in ("before", "after"), "invalid continuation phase")
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", "")), "continuation run ID required")
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ATTEMPT", "")), "continuation attempt required")
    require(1 <= stage <= 8, "continuation stage must be 1..8")
    require(os.environ.get("CHROMIX_BUILD_PROFILE") == "fast"
            and os.environ.get("CHROMIX_USE_UPSTREAM_CACHE") == "1",
            "continuation requires fast profile and mandatory restored cache")
    receipt = json.loads(read(work, receipt_name))
    expected = continuation_identity() if expected is None else expected
    manifest = expected_manifest() if manifest is None else manifest
    require(all(receipt.get(key) == value for key, value in expected.items()),
            "missing or changed arm64-stage8 continuation provenance")
    require(receipt.get("continuation_run") == os.environ.get("GITHUB_RUN_ID")
            and receipt.get("provenance", {}).get("manifest") == manifest,
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
        previous_handoff(work, prefix, stage, base, expected, state)
    output = work / "fingerprint-diagnostics" / (
        f"{prefix}-s{stage}-{phase}-attempt-{os.environ['GITHUB_RUN_ATTEMPT']}.json")
    live_source(repo, work, output.with_name(output.stem + "-source.json"))
    result = dict(expected, continuation_stage=stage, logical_stage=base + stage, phase=phase,
                  continuation_run=os.environ.get("GITHUB_RUN_ID"),
                  continuation_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"), state=state)
    output.write_bytes(arp._json(result))
    return result


def output_inventory(out):
    result = {}
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(out, followlinks=False, onerror=walk_error):
        for name in sorted([*dirs, *files]):
            path = Path(directory) / name
            info = path.lstat()
            result[path.relative_to(out).as_posix()] = (
                info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                info.st_ino, os.readlink(path) if path.is_symlink() else None)
    return result


def native_state(repo, work):
    src = work / "src"
    old = json.loads(read(work, "upstream-cache-preparation.json"))
    require(prepared.host_identity() == ("macos", "arm64"), "native macOS ARM64 required")
    require(prepared.inspect_native_tools(src, "macos", "arm64")["native_tools"],
            "prepared native tools no longer executable")
    require(prepared.environments_compatible(old["environment"], prepared.environment_identity(src, "macos")),
            "SDK/toolchain environment changed; no prepare fallback")
    require(prepared.tool_fingerprint(src, "macos", "arm64") == old["tool_fingerprint"],
            "compiler/toolchain bytes changed")
    generators = prepared.generator_fingerprint(src, "macos", "arm64")
    generators["macos154"] = macos_restored_generators.prepare(src, host_arch="arm64", repo=repo)
    require(generators == old["generator_fingerprint"], "generator preimage changed")
    macos_runtime.verify_runtime_loader(src, "arm64")
    require(read(src, "out/Default/.ninja_log").startswith(b"# ninja log v7\n"), "Ninja log format changed")
    require(read(src, "out/Default/.ninja_deps").startswith(b"# ninjadeps\n\x04\x00\x00\x00"),
            "Ninja dependency format changed")


def regeneration_proof(out, graphs, wrapper, inspect):
    """Trace protected files through phony inputs to executable producer edges."""
    out = out.resolve()
    protected = {path.resolve() for path in graphs}
    protected.update((out / name).resolve() for name in ("args.gn", "build.ninja.stamp", "build.ninja.d"))
    targets = {}
    for line in inspect(["-t", "targets", "all"]).splitlines():
        name, separator, rule = line.rpartition(": ")
        require(separator and name and rule and name not in targets, "invalid Ninja target inventory")
        require((out / name).resolve() != wrapper, "private Ninja entry must not have a generating edge")
        targets[name] = rule
    require(targets, "empty Ninja target inventory")
    roots = sorted(name for name in targets if (out / name).resolve() in protected)
    pending, visited, producers = list(roots), {}, {}
    while pending:
        batch = sorted(set(pending) - visited.keys())
        pending = []
        if not batch:
            break
        # -n prevents RUN_AFTER_LOGS query from opening/recompacting the donor logs for writing.
        text = inspect(["-n", "-t", "query", *batch])
        blocks, current, section = {}, None, None
        for line in text.splitlines():
            if not line.startswith(" "):
                require(line.endswith(":") and line[:-1] in batch and line[:-1] not in blocks,
                        "unknown Ninja query node")
                current = blocks[line[:-1]] = {"rule": None, "inputs": []}
                section = None
            elif line.startswith("  input: "):
                require(current is not None and current["rule"] is None, "duplicate Ninja input edge")
                current["rule"] = line.removeprefix("  input: ")
                section = "inputs"
            elif line in ("  validations:", "  outputs:", "  validation for:"):
                require(current is not None, "Ninja query section without node")
                section = "inputs" if line == "  validations:" else "outputs"
            elif line.startswith("    "):
                require(current is not None and section is not None, "unknown Ninja query dependency")
                if section == "inputs":
                    name = line[4:]
                    for prefix in ("|| ", "| "):
                        if name.startswith(prefix):
                            name = name[len(prefix):]
                            break
                    require(name, "empty Ninja dependency")
                    current["inputs"].append(name)
            else:
                raise ValueError("unknown Ninja query format")
        require(set(blocks) == set(batch), "incomplete Ninja producer graph query")
        for name, node in blocks.items():
            require(node["rule"] == targets.get(name), "Ninja producer rule disagrees with inventory")
            visited[name] = node
            if node["rule"] not in (None, "phony"):
                producers[name] = node["rule"]
            pending.extend(node["inputs"])
        require(len(visited) <= 10000, "protected Ninja producer graph is unexpectedly large")
    forbidden = set()
    for name in sorted(producers):
        lines = inspect(["-t", "commands", "-s", name]).splitlines()
        require(len(lines) == 1 and lines[0].strip(), "executable Ninja producer has no identifiable command")
        forbidden.add(lines[0])
    produced_paths = {(out / name).resolve() for name in roots}
    proof = {"protected": sorted(str(path.relative_to(out)) for path in protected),
             "roots": roots, "nodes": visited, "producers": producers,
             "commands": sorted(forbidden),
             "unproduced": sorted(str(path.relative_to(out)) for path in protected
                                   if path not in produced_paths)}
    # Empty commands are justified only by a complete leaf/phony producer closure.
    require(forbidden or all(node["rule"] in (None, "phony") for node in visited.values()),
            "unproved empty regeneration command set")
    return forbidden, proof


def inventory_difference(before, after):
    fields = ("mode", "size", "mtime_ns", "ctime_ns", "inode", "symlink")
    return {name: {"before": before.get(name), "after": after.get(name),
                   "fields": [field for i, field in enumerate(fields)
                              if name not in before or name not in after or before[name][i] != after[name][i]]}
            for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)}


@contextmanager
def preflight_source(work, diagnostics):
    src = (work / "src").resolve()
    require(sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
            "isolated mac156 preflight requires macOS sandbox-exec")
    before = output_inventory(src)
    # A sibling preserves every ../ input, including paths climbing to the volume root.
    with tempfile.TemporaryDirectory(prefix=".mac156-preflight-", dir=work) as temporary:
        scratch = Path(temporary).resolve()
        libc = ctypes.CDLL(None, use_errno=True)
        clonefile = libc.clonefile
        clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
        clonefile.restype = ctypes.c_int

        def clone_file(source, destination):
            require(stat.S_ISREG(os.lstat(source).st_mode), "preflight clone requires regular files")
            if clonefile(os.fsencode(source), os.fsencode(destination), 0):
                raise OSError(ctypes.get_errno(), "preflight clonefile failed", source)
            shutil.copystat(source, destination, follow_symlinks=False)
            return destination

        shutil.copytree(src, scratch, symlinks=True, dirs_exist_ok=True, copy_function=clone_file)
        copied = output_inventory(scratch)
        require(set(before) == set(copied), "preflight clone inventory differs from complete source")
        for name, entry in before.items():
            clone = copied[name]
            require(entry[:3] == clone[:3] and entry[5] == clone[5],
                    "preflight clone changed size/mode/mtime/link: " + name)
            if stat.S_ISREG(entry[0]):
                require(entry[4] != clone[4], "preflight clone must not hardlink original files: " + name)
        # APFS clonefile creates independent copy-on-write files; no hardlinks or sparse placeholders.
        policy = diagnostics / "preflight.sb"
        policy.write_text('(version 1)\n(allow default)\n(deny file-write*)\n'
                          '(allow file-write* (subpath ' + json.dumps(str(scratch)) + '))\n')
        (diagnostics / "clone.json").write_bytes(arp._json({
            "method": "clonefile regular files; copied directories/symlinks", "source": str(src), "scratch": str(scratch),
            "entries": len(before), "relative_depth_preserved": True}))
        try:
            yield scratch, ["/usr/bin/sandbox-exec", "-f", str(policy)]
        finally:
            scratch_diff = inventory_difference(copied, output_inventory(scratch))
            source_diff = inventory_difference(before, output_inventory(src))
            (diagnostics / "preflight-state-difference.json").write_bytes(arp._json({
                "original_changed": source_diff, "scratch_changed": scratch_diff}))
            require(not source_diff, "Ninja preflight modified original source/output; see state-difference report")


def frozen_ninja(ninja, work, jobs, env):
    """Load the original graph through a non-generated entry, without touching out."""
    out = work / "src/out/Default"
    graphs = sorted(out.rglob("*.ninja"))
    for path in graphs:
        raw = read(out, path.relative_to(out).as_posix())
        # A dry run cannot attest to dependencies introduced by future commands.
        require(not re.search(rb"(?m)^\s*dyndep\s*=", raw),
                "dynamic Ninja dependencies cannot use frozen-graph continuation")
    inventory = output_inventory(out)
    diagnostics = Path(tempfile.mkdtemp(prefix="mac156-ninja-probes-", dir=work / "fingerprint-diagnostics"))
    with preflight_source(work, diagnostics) as (scratch, sandbox), \
            tempfile.TemporaryDirectory(prefix="entry-", dir=diagnostics) as temporary:
        wrapper = Path(temporary) / "entry.ninja"
        wrapper.write_text("include build.ninja\n")
        command = [*sandbox, str(ninja), "-C", str(scratch / "out/Default"), "-f", str(wrapper)]

        probe_index = 0

        def inspect(arguments, probe_env=env):
            nonlocal probe_index
            probe_index += 1
            prefix = diagnostics / f"probe-{probe_index:03d}"
            result = subprocess.run([*command, *arguments], cwd=scratch, env=probe_env,
                                    capture_output=True, text=True, check=False, timeout=180)
            prefix.with_suffix(".stdout").write_text(result.stdout)
            prefix.with_suffix(".stderr").write_text(result.stderr)
            prefix.with_suffix(".json").write_bytes(arp._json({"arguments": arguments, "returncode": result.returncode}))
            require(result.returncode == 0, f"Ninja probe failed; see {prefix}")
            require(not result.stderr, f"Ninja graph inspection produced warnings; see {prefix}")
            return result.stdout

        forbidden, proof = regeneration_proof(scratch / "out/Default",
                                               sorted((scratch / "out/Default").rglob("*.ninja")), wrapper, inspect)
        (diagnostics / "regeneration-proof.json").write_bytes(arp._json(proof))
        # Unlike -t commands, the dry-run plan includes validation and log dependencies.
        plan_env = dict(env, NINJA_STATUS="CHROMIX_FROZEN_PLAN ")
        plan = inspect(["-n", "-v", "-j", jobs, "chrome"], plan_env)
        planned = []
        for line in plan.splitlines():
            if line.startswith("CHROMIX_FROZEN_PLAN "):
                planned.append(line.removeprefix("CHROMIX_FROZEN_PLAN "))
            else:
                require(line.startswith("ninja: Entering directory ") or line == "ninja: no work to do.",
                        "unrecognized or multiline frozen Ninja command")
        require(not forbidden.intersection(planned), "GN/manifest regeneration in compile plan is forbidden")
    difference = inventory_difference(inventory, output_inventory(out))
    (diagnostics / "original-out-difference.json").write_bytes(arp._json(difference))
    require(not difference, "Ninja preflight modified existing output state")
    with tempfile.TemporaryDirectory(prefix="compile-entry-", dir=diagnostics) as temporary:
        wrapper = Path(temporary) / "entry.ninja"
        wrapper.write_text("include build.ninja\n")
        # Only the approved original graph is compiled; scratch objects/logs are never published.
        return subprocess.run([str(ninja), "-C", str(out), "-f", str(wrapper), "-j", jobs, "chrome"],
                              cwd=work / "src", env=env, check=False).returncode


def compile_tree(repo, work, stage, *, receipt_name=RECEIPT,
                 prefix="mac156-arm64-stage8-continuation", base=STAGE, expected=None):
    expected = continuation_identity() if expected is None else expected
    receipt = json.loads(read(work, receipt_name))
    require(all(receipt.get(key) == value for key, value in expected.items())
            and receipt.get("continuation_run") == os.environ.get("GITHUB_RUN_ID"), "unadmitted compile")
    require(stage is not None and 1 <= stage <= 8, "compile stage required")
    before = json.loads(read(work, f"fingerprint-diagnostics/{prefix}-s{stage}"
                            f"-before-attempt-{os.environ.get('GITHUB_RUN_ATTEMPT', 'local')}.json"))
    require(all(before.get(key) == value for key, value in expected.items())
            and before.get("continuation_run") == os.environ.get("GITHUB_RUN_ID")
            and before.get("continuation_stage") == stage and before.get("phase") == "before"
            and before.get("logical_stage") == base + stage
            and before.get("continuation_attempt") == os.environ.get("GITHUB_RUN_ATTEMPT")
            and before.get("state") == source_state(repo, work), "compile requires same-attempt read-only gate")
    ninja = Path(shutil.which("ninja") or "missing").resolve()
    require(restore_ninja.host_identity() == ("macos", "arm64")
            and restore_ninja.binary_architectures(ninja, "macos") == {"arm64"}
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
        print(f"mac156 arm64-stage8 continuation rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
