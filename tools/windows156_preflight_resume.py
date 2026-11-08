#!/usr/bin/env python3
"""Resume only the two reviewed Windows156 precompile checkpoints, without migration."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

import apply_restored_patches as patches
import merge_gn_args
import migrate_windows_snapshot as files
import patch_selection
import verify_patch_stack
import windows156_fresh as fresh
from verify_windows_snapshot_source import _inventory

REPOSITORY = "xiaozhou26/Chromix"
DONOR_SHA = "2e2a85fdeb42f37a7528a7d1ced247deab337452"
MAIN_BASE = "3dfe10e164966f9d8f99099849c4f2aa1ed9138c"
OLD_HELPER_SHA256 = "2e04eae3fcdc88c37e7e8a7f1471e7576a2d5059ba0f2f13333b83fc91425c26"
# The integration commit must explicitly approve the reviewed bindings fix.
REVIEWED_HELPER_SHA256 = frozenset()
HELPER = "tools/install_windows_metagen_bindings.py"
SOURCE_RECEIPT_SHA256 = "9354e1cb764d8d2c3cfd54a7e11dca60814b16d22212d2d97d4415cce6550a5c"
DONORS = {
    "x64": {
        "run_id": 42455183038, "job_id": 128652555912, "workflow_id": 410936225,
        "source_receipt": "source-6c3d964f020842969b2ad74da074bec6.json",
        "parts": [
            (13607993810, 9663676664, "c77c770b4e403a369163da6bf611394063c929c4a8dd81381774f0de94284d62"),
            (13608606503, 255760787, "e225e980d6a98d105aeaadcd6ab61275219190bb2958bebb969b73f689317ea2"),
        ],
    },
    "arm64": {
        "run_id": 42455185910, "job_id": 128652565583, "workflow_id": 410936224,
        "source_receipt": "source-abebee2bd79a4340b79eb9620a0a9381.json",
        "parts": [
            (13609301354, 9663676664, "f41785b6232a502a903bf273cdb841a3eedf9841eac25a425eb0c15d0e875084"),
            (13608573911, 261130376, "877091234f7c3d40aa5aaf7ea56bc4ac989ddc436516bc15cdf5f5e6e4788bf2"),
        ],
    },
}
RECOVERY_FILES = frozenset({
    "tools/windows156_preflight_resume.py",
    "tools/tests/test_windows156_preflight_resume.py",
    "tools/gen_windows_arm64_workflow.py",
    "tools/tests/test_windows156_fresh.py",
    "tools/tests/test_windows_metagen_bindings.py",
    "tools/tests/fixtures/windows156_metagen.json.gz",
    "tools/tests/fixtures/windows156_preflight_source.json.gz",
    HELPER,
    *[f".github/workflows/build-win-{arch}-156-{mode}.yml"
      for arch in DONORS for mode in ("fresh", "preflight-resume")],
})


def manifest(arch: str) -> dict:
    donor = DONORS[arch]
    return dict(repository=REPOSITORY, head_sha=DONOR_SHA, run_id=donor["run_id"],
                stage=1, attempt=1, job_id=donor["job_id"], arch=arch,
                workflow=f"build-win-{arch}-156-fresh", artifacts=[
                    dict(id=artifact, name=f"win-{arch}-156-tree-s1-attempt-1-part{index}",
                         size_in_bytes=size, digest="sha256:" + digest, expired=False)
                    for index, (artifact, size, digest) in enumerate(donor["parts"], 1)])


def metadata(client, arch: str) -> dict:
    expected = manifest(arch)
    donor = DONORS[arch]
    run = client.get(f'/actions/runs/{donor["run_id"]}')
    required = dict(id=donor["run_id"], head_sha=DONOR_SHA, head_branch="main", run_attempt=1,
                    status="completed", conclusion="failure", event="workflow_dispatch",
                    workflow_id=donor["workflow_id"], path=f'.github/workflows/{expected["workflow"]}.yml')
    if (any(run.get(key) != value for key, value in required.items())
            or any(run.get(key, {}).get("full_name") != REPOSITORY
                   or run.get(key, {}).get("id") != 1342691290 for key in ("repository", "head_repository"))):
        raise ValueError("precompile donor run identity mismatch")
    jobs = client.items(f'/actions/runs/{donor["run_id"]}/attempts/1/jobs', "jobs")
    jobs = [job for job in jobs if job.get("id") == donor["job_id"]
            or job.get("name") == f"Windows156 {arch} stage 1"]
    required = dict(id=donor["job_id"], name=f"Windows156 {arch} stage 1", run_id=donor["run_id"],
                    head_sha=DONOR_SHA, run_attempt=1, status="completed", conclusion="failure")
    if len(jobs) != 1 or any(jobs[0].get(key) != value for key, value in required.items()):
        raise ValueError("precompile donor job identity mismatch")
    steps = jobs[0].get("steps", [])
    required_steps = {"Run stage 1": "failure", "Restore latest valid preserved snapshot": "skipped",
                      "Upload final bundle": "skipped", "Ensure build tree snapshot": "success",
                      "Upload fingerprint diagnostics": "success",
                      **{f"Upload tree part {index}": "success" for index in range(1, 5)}}
    for name, conclusion in required_steps.items():
        selected = [step for step in steps if step.get("name") == name]
        if len(selected) != 1 or selected[0].get("conclusion") != conclusion:
            raise ValueError("precompile donor step mismatch: " + name)
    artifacts = client.items(f'/actions/runs/{donor["run_id"]}/artifacts', "artifacts")
    trees = [item for item in artifacts if "tree-" in item.get("name", "")]
    if len(trees) != len(expected["artifacts"]):
        raise ValueError("donor has a different/latest checkpoint set")
    for pinned in expected["artifacts"]:
        selected = [item for item in trees if item.get("id") == pinned["id"]]
        if (len(selected) != 1 or any(selected[0].get(key) != value for key, value in pinned.items())
                or selected[0].get("workflow_run", {}).get("id") != donor["run_id"]
                or selected[0].get("workflow_run", {}).get("head_sha") != DONOR_SHA):
            raise ValueError("precompile donor artifact identity mismatch")
    return expected


def checked_download(origin: dict, download: dict, arch: str) -> None:
    expected = manifest(arch)
    if origin != expected:
        raise ValueError("origin is not the fixed precompile donor")
    required = dict(status="success", phase="complete", publication="published",
                    repository=REPOSITORY, head_sha=DONOR_SHA, run_id=expected["run_id"])
    if any(download.get(key) != value for key, value in required.items()):
        raise ValueError("snapshot download was not authenticated and published")
    records = download.get("artifacts", [])
    if [{key: item.get(key) for key in expected["artifacts"][0]} for item in records] != expected["artifacts"]:
        raise ValueError("download artifact set differs from fixed donor")
    volumes = download.get("volumes", [])
    if (not volumes or [item.get("name") for item in volumes]
            != [f"tree.7z.{index:03d}" for index in range(1, len(volumes) + 1)]
            or {item.get("artifact_id") for item in volumes} != {item["id"] for item in records}
            or any(type(item.get("size_in_bytes")) is not int or item["size_in_bytes"] <= 0
                   or not re.fullmatch("[0-9a-f]{64}", item.get("sha256", "")) for item in volumes)
            or download.get("total_size_in_bytes") != sum(item["size_in_bytes"] for item in volumes)):
        raise ValueError("download volume receipt is incomplete")


def git_tree(git: str, repo: Path, sha: str) -> dict:
    result = {}
    for record in files._git(git, repo, "ls-tree", "-rz", "--full-tree", sha).split(b"\0"):
        if record:
            entry, name = record.split(b"\t", 1)
            result[name.decode()] = tuple(entry.decode().split())
    return result


def checkout_identity(repo: Path, target_sha: str) -> tuple[str, dict]:
    git = files._host_git((repo,))
    head, tree = files._tree(git, repo, target_sha)
    files._git(git, repo, "merge-base", "--is-ancestor", MAIN_BASE, head)
    base = git_tree(git, repo, MAIN_BASE)
    old = git_tree(git, repo, DONOR_SHA)
    if not base or not old:
        raise ValueError("fixed main/donor Git trees are missing")
    changed = {name for name in set(base) | set(tree) if base.get(name) != tree.get(name)}
    if changed - RECOVERY_FILES:
        raise ValueError("main inputs changed outside reviewed recovery helpers: " + str(sorted(changed - RECOVERY_FILES)))
    if _inventory(repo) != set(tree):
        raise ValueError("recovery checkout has extra or missing files")
    files._tracked(repo, tree)
    old_helper = files._git(git, repo, "cat-file", "blob", f"{DONOR_SHA}:{HELPER}")
    new_helper = files._git(git, repo, "cat-file", "blob", f"{head}:{HELPER}")
    if (hashlib.sha256(old_helper).hexdigest() != OLD_HELPER_SHA256
            or hashlib.sha256(new_helper).hexdigest() not in REVIEWED_HELPER_SHA256):
        raise ValueError("bindings helper fix is not explicitly hash-reviewed")
    return git, tree


def old_checkout(git: str, repo: Path, destination: Path) -> None:
    # Git blobs are verification data; no donor code is imported or executed.
    old = git_tree(git, repo, DONOR_SHA)
    current = git_tree(git, repo, "HEAD")
    archive = files._git(git, repo, "archive", "--format=zip", DONOR_SHA)
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        for item in source.infolist():
            if item.is_dir():
                continue
            relative = patches._relative(item.filename)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            # Preserve Windows checkout EOL/modes only for identical authenticated blobs.
            data = files._file(repo, relative).read_bytes() if old[relative] == current.get(relative) else source.read(item)
            target.write_bytes(data)
            if os.name != "nt":
                target.chmod(int(old[relative][0], 8) & 0o777)


def precompile_logs(out: Path) -> None:
    # Only these fixed post-GN/pre-Ninja donors may have no Chromium Ninja logs.
    for name, header in ((".ninja_log", b"# ninja log v5\n"),
                         (".ninja_deps", b"# ninjadeps\n\x04\x00\x00\x00")):
        path = files._file(out, name, missing=True)
        if path.exists() and path.read_bytes().replace(b"\r\n", b"\n") != header:
            raise ValueError("invalid or compiled Ninja state in precompile donor: " + name)
    for path in out.rglob("*"):
        if path.is_dir():
            files._safe(path, directory=True)
        else:
            files._safe(path)
            if path.suffix.lower() in (".obj", ".o", ".dll", ".exe", ".lib", ".pdb", ".tlb") and path.name != "gn.exe":
                raise ValueError("compiled output contradicts precompile donor: " + str(path))


def gn_contract(repo: Path, work: Path, arch: str, core: Path, tooling: Path) -> None:
    src = work / "src"
    overlay = ('# Windows156 fresh native build; never disable PGO for tarball sources.\n'
               'chrome_pgo_phase = 2\n'
               f'v8_metagen_libclang_so = {json.dumps((src / "third_party/llvm-build/Release+Asserts/bin/libclang.dll").as_posix())}\n'
               f'v8_metagen_libclang_bindings_dir = {json.dumps((src / "third_party/llvm-build/Release+Asserts/bindings/python").as_posix())}\n')
    if files._file(work, "windows156-native.gn").read_text() != overlay:
        raise ValueError("preserved native PGO2 overlay changed")
    inputs = [core / "flags.gn", tooling / "flags.windows.gn", repo / "build/args.windows.gn"]
    if arch == "arm64":
        inputs.append(repo / "build/args.windows.arm64.gn")
    inputs.append(work / "windows156-native.gn")
    order, merged = [], {}
    for path in inputs:
        keys, values = merge_gn_args.parse(path)
        for key in keys:
            if key not in merged:
                order.append(key)
            merged[key] = values[key]
    expected = "\n".join([merge_gn_args.GENERATED_HEADER, *[merged[key] for key in order]]) + "\n"
    if files._file(src, "out/Chromix/args.gn").read_text() != expected:
        raise ValueError("preserved GN arguments differ from exact donor native inputs")


def source_receipt(work: Path, arch: str, proof: dict) -> None:
    receipt = files._file(work, "fingerprint-diagnostics/" + DONORS[arch]["source_receipt"])
    if fresh.file_digest(receipt) != SOURCE_RECEIPT_SHA256:
        raise ValueError("complete original source receipt is missing or changed")
    if json.loads(receipt.read_text()) != proof:
        raise ValueError("full source proof differs from original donor receipt")


def verify_source(repo: Path, previous: Path, work: Path, arch: str) -> dict:
    fresh.initialize(repo, work, arch, resume=True)
    src = files._safe(work / "src", directory=True)
    for root in (work, src):
        for path in root.iterdir():
            if path.name.startswith(".chromix") and ("in-progress" in path.name or "restore" in path.name):
                raise ValueError("interrupted or upstream source state is forbidden")
    pins = fresh.checked_pins(repo)
    if fresh.checked_pins(previous) != pins:
        raise ValueError("old/new source pins changed")
    key = patch_selection.preparation_key(repo, "windows", "windows")
    if patch_selection.preparation_key(previous, "windows", "windows") != key:
        raise ValueError("old/new source preparation key changed")
    markers = {
        ".chromix-source-unpacked": fresh.VERSION, ".chromix-ungoogled-core": fresh.CORE,
        ".chromix-ungoogled-windows": fresh.WINDOWS, ".chromix-binaries-pruned": fresh.CORE,
        ".chromix-domain-substituted": fresh.CORE, ".chromix-patches": key,
        ".chromix-patch-selection": key,
        ".chromix-source-ready": "|".join((fresh.VERSION, fresh.CORE, fresh.WINDOWS, key)),
    }
    for name, expected in markers.items():
        if files._file(src, name).read_text().strip() != expected:
            raise ValueError("source marker mismatch: " + name)
    arch_marker = files._file(work, ".chromix-target-arch", missing=arch == "x64")
    if arch_marker.exists() and arch_marker.read_text().strip() != arch:
        raise ValueError("source target marker mismatch")
    core = files._safe(work / "tooling/ungoogled-chromium", directory=True)
    tooling = files._safe(work / "tooling/ungoogled-chromium-windows", directory=True)
    git = files._host_git((repo, previous, work))
    tool_states = []
    for root, pin, submodule in ((core, fresh.CORE, None), (tooling, fresh.WINDOWS, fresh.CORE)):
        head, tree = files._tree(git, root, pin)
        tool_states.append((root, head, tree, files._tracked(root, tree, core_pin=submodule)))
    old_identity, _, old_lite = patches._load(previous, core, tooling, "windows", src=src)
    new_identity, _, new_lite = patches._load(repo, core, tooling, "windows", src=src)
    if old_identity != new_identity or old_lite != new_lite:
        raise ValueError("old/new patch, lite or tooling source identity changed")
    for name, (data, _) in new_lite.items():
        if files._file(src, name).read_bytes() != data:
            raise ValueError("preserved lite source changed: " + name)
    gn_contract(repo, work, arch, core, tooling)
    for name in ("out/Chromix/build.ninja", "out/Chromix/gn.exe"):
        if files._file(src, name).stat().st_size == 0:
            raise ValueError("precompile GN output is empty")
    precompile_logs(src / "out/Chromix")
    pgo = "chrome/build/pgo_profiles/" + fresh.PROFILES[arch]
    files._file(src, pgo)
    files._file(src, pgo + ".chromix-sha256")
    protected = files._capture(src, {*markers, "out/Chromix/args.gn", "out/Chromix/build.ninja", pgo,
                                      pgo + ".chromix-sha256", "build/toolchain/win/midl.py"})
    import repair_windows_midl
    if fresh.file_digest(files._file(src, "build/toolchain/win/midl.py")) != repair_windows_midl.REPAIRED_SHA256:
        raise ValueError("preserved MIDL source differs from reviewed repair")
    import install_windows_metagen_bindings as bindings
    bindings.source_contract(src, src / "out/Chromix", fresh.VERSION)
    bindings.validate_sources(src, fresh.VERSION)
    proof = verify_patch_stack.verify(src, repo, core=core, tooling=tooling, platform="windows")
    source_receipt(work, arch, proof)
    fresh.probe(src, arch)
    files._unchanged(src, protected)
    for root, head, tree, state in tool_states:
        if files._tree(git, root, head) != (head, tree):
            raise ValueError("tooling Git identity changed concurrently")
        files._unchanged(root, state)
    return dict(schema_version=1, status="verified", phase="post-gn-precompile", arch=arch,
                previous_sha=DONOR_SHA, preparation_key=key, source=proof, changed_source_files=[])


def consumer(arch: str) -> str:
    sha = os.environ.get("GITHUB_SHA", "")
    if (os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_JOB") != "build-1" or not re.fullmatch("[0-9a-f]{40}", sha)
            or sha == DONOR_SHA or os.environ.get("GITHUB_RUN_ID") in {str(d["run_id"]) for d in DONORS.values()}
            or any(not re.fullmatch("[1-9][0-9]*", os.environ.get(key, ""))
                   for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))
            or os.environ.get("GITHUB_WORKFLOW_REF")
            != f"{REPOSITORY}/.github/workflows/build-win-{arch}-156-preflight-resume.yml@refs/heads/main"):
        raise ValueError("recovery requires a new main-only fixed-workflow consumer")
    return sha


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("metadata", "verify"), required=True)
    parser.add_argument("--arch", choices=tuple(DONORS), required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, default=Path("C:/c/chromix"))
    parser.add_argument("--origin", type=Path)
    parser.add_argument("--download", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        sha = consumer(args.arch)
        repo = files._safe(args.repo.resolve(), directory=True)
        git, tree = checkout_identity(repo, sha)
        from validate_windows_snapshot import Client
        origin = metadata(Client(REPOSITORY, os.environ.get("GH_TOKEN", "")), args.arch)
        if args.phase == "metadata":
            result = origin
        else:
            if not args.origin or not args.download:
                raise ValueError("verify requires original metadata and successful download receipts")
            checked_download(json.loads(args.origin.read_text()), json.loads(args.download.read_text()), args.arch)
            work = files._safe(args.workdir.resolve(), directory=True)
            if work == repo or work.is_relative_to(repo) or repo.is_relative_to(work):
                raise ValueError("source and checkout must be separate")
            with tempfile.TemporaryDirectory(prefix="windows156-donor-inputs-") as temporary:
                previous = Path(temporary).resolve()
                old_checkout(git, repo, previous)
                result = verify_source(repo, previous, work, args.arch)
            if files._tree(git, repo, sha)[1] != tree:
                raise ValueError("consumer checkout changed concurrently")
            files._tracked(repo, tree)
            result.update(target_sha=sha, origin=origin, consumer={key: os.environ[key] for key in
                          ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "GITHUB_SHA")})
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, indent=2)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Windows156 fixed preflight resume failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
