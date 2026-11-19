#!/usr/bin/env python3
"""Restore/admit only the two reviewed Linux157 stage1 pre-GN checkpoints."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

try:
    from . import linux157_source as source
    from . import linux157_toolchain as tools
    from . import validate_posix_snapshot as snapshot
except ImportError:
    import linux157_source as source
    import linux157_toolchain as tools
    import validate_posix_snapshot as snapshot

MANIFEST = "build/linux/157.0.8103.78/pre-gn-checkpoints.json"
MANIFEST_SHA256 = "a644c2bdd1ea5756320fe34e822dd9864bf57812388a5c434b64c20832da73c3"
MARKER = ".chromix-linux157-gn-migration.json"
PENDING = ".chromix-linux157-gn-migration-in-progress"
REVIEWED_CHANGED = {"tools/linux157_source.py", "tools/linux157_build.py"}
REVIEWED_ADDED = {"tools/linux157_gn.py", "tools/linux157_checkpoint.py", MANIFEST}


def load(repo: Path = source.ROOT) -> dict:
    raw = source.regular(repo, MANIFEST).read_bytes()
    if source.digest(raw) != MANIFEST_SHA256:
        raise ValueError("unreviewed Linux157 pre-GN checkpoint manifest")
    return json.loads(raw)


def expected(arch: str, repo: Path = source.ROOT) -> dict:
    m = load(repo)
    if arch not in m["donors"]:
        raise ValueError("unsupported Linux157 checkpoint architecture")
    d = m["donors"][arch]
    return {"repository": m["repository"], "platform": "linux", "workflow": "build-linux-" + arch,
            "run_id": d["run_id"], "attempt": 1, "stage": 1, "arch": arch,
            "head_sha": m["head_sha"], "job_id": d["job_id"],
            "pattern": f"chromix-linux-{arch}-tree-s1-attempt-1-part*",
            "artifacts": [{k: v for k, v in d["artifact"].items() if k != "expires_at"} | {"expired": False}]}


def validate_metadata(client, arch: str, repo: Path = source.ROOT) -> dict:
    m = load(repo)
    d = m["donors"][arch]
    report = snapshot.validate(client, m["repository"], d["run_id"], 1, 1, arch,
                               [d["artifact"]["id"]], platform="linux")
    if json.dumps(report, sort_keys=True) != json.dumps(expected(arch, repo), sort_keys=True):
        raise ValueError("Linux157 requires exact latest prepared stage1 metadata")
    artifacts = client.items(f"/actions/runs/{d['run_id']}/artifacts", "artifacts")
    artifact = next(a for a in artifacts if a.get("id") == d["artifact"]["id"])
    if (artifact.get("expires_at") != d["artifact"]["expires_at"]
            or datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)):
        raise ValueError("Linux157 exact checkpoint expired or expiry identity changed")
    run = client.get(f"/actions/runs/{d['run_id']}")
    if (run.get("run_attempt") != 1 or run.get("conclusion") != "failure"
            or run.get("head_branch") != "main" or run.get("repository", {}).get("id") != m["repository_id"]):
        raise ValueError("Linux157 donor run/attempt changed")
    job = next(j for j in client.items(f"/actions/runs/{d['run_id']}/attempts/1/jobs", "jobs")
               if j.get("id") == d["job_id"])
    steps = {s["name"]: s.get("conclusion") for s in job["steps"]}
    if (job.get("name") != d["job_name"] or job.get("conclusion") != "failure"
            or steps.get("Run stage 1") != "failure"
            or steps.get("Preserve latest failed Linux source/output checkpoint") != "success"):
        raise ValueError("Linux157 donor is not the reviewed pre-GN failure")
    return report


def verify_files(root: Path, expected_hashes: dict) -> None:
    for name, digest in expected_hashes.items():
        path = root / name
        if digest is None:
            if path.exists() or path.is_symlink():
                raise ValueError("pre-GN checkpoint unexpectedly contains: " + name)
        elif tools.file_hash(source.regular(root, name)) != digest:
            raise ValueError("changed pre-GN checkpoint input: " + name)


def checked_binary(src: Path, name: str, expected_hash: str) -> None:
    path = (src / name).resolve(strict=True)
    if not path.is_relative_to(src.resolve()) or tools.file_hash(path) != expected_hash:
        raise ValueError("changed official checkpoint tool: " + name)


def validate_recipe_delta(old: dict, new: dict) -> None:
    a, b = old["inputs"], new["inputs"]
    if (set(b) - set(a) != REVIEWED_ADDED or set(a) - set(b)
            or {name for name in a if a[name] != b[name]} != REVIEWED_CHANGED):
        raise ValueError("unreviewed Linux157 helper recipe delta")
    if {k: v for k, v in old.items() if k not in ("inputs", "sha256")} != {
            k: v for k, v in new.items() if k not in ("inputs", "sha256")}:
        raise ValueError("Linux157 source/core/platform identity changed")


def compatible_selection(old: dict, new: dict) -> None:
    def normalized(value):
        value = copy.deepcopy(value)
        metadata = value["selection"]
        metadata.pop("manifest", None)
        metadata.pop("review_manifest", None)
        downstream = metadata["downstream"]
        for key in ("recipe", "validation"):
            downstream.pop(key, None)
        if isinstance(metadata.get("additions"), dict):
            metadata["additions"].pop("manifest", None)
        return value
    if normalized(old) != normalized(new):
        raise ValueError("Linux157 checkpoint migration cannot change the 226 patches")


def current_selection(repo: Path, src: Path) -> dict:
    try:
        from . import patch_selection
    except ImportError:
        import patch_selection
    return patch_selection.select(repo, "linux", src=src)[0]


def admit(work: Path, arch: str, metadata: dict, repo: Path = source.ROOT) -> dict:
    if json.dumps(metadata, sort_keys=True) != json.dumps(expected(arch, repo), sort_keys=True):
        raise ValueError("wrong Linux157 checkpoint metadata admission")
    m = load(repo)
    donor = m["donors"][arch]
    download = json.loads(source.regular(work, "linux157-checkpoint-download.json").read_bytes())
    if (download.get("status") != "success" or download.get("publication") != "published"
            or download.get("repository") != m["repository"] or download.get("head_sha") != m["head_sha"]
            or download.get("run_id") != donor["run_id"] or len(download.get("artifacts", [])) != 1):
        raise ValueError("missing verified exact checkpoint download")
    artifact = download["artifacts"][0]
    if any(artifact.get(k) != v for k, v in metadata["artifacts"][0].items()):
        raise ValueError("checkpoint download artifact mismatch")
    src = work / "src"
    if (src / PENDING).exists() or (src / MARKER).exists():
        raise ValueError("migration already published or interrupted")
    for name in (source.IN_PROGRESS, ".chromix-domain-substitution-in-progress", ".chromix-upstream-restored.json"):
        if (src / name).exists() or (src / name).is_symlink():
            raise ValueError("partial or donor-cache source cannot be admitted")
    verify_files(work, donor["files"])
    verify_files(src, {"out/Chromix/gn": (
        "25684acf0d11a3de5365c881b051f0c111e894e5d5a74898f81c2b04db25801b"
        if arch == "arm64" else None)})
    receipt = json.loads(source.regular(src, source.RECEIPT).read_bytes())
    if receipt != donor["source_receipt"] or receipt["stage"] != "chromix-applied":
        raise ValueError("wrong pre-GN source receipt")
    if not (src / ".chromix-domain-substituted").is_file():
        raise ValueError("pre-GN donor must already be domain substituted")
    verify_files(src, receipt["substituted_outputs"])
    final = json.loads(source.regular(work, "fingerprint-diagnostics/source-final.json").read_bytes())
    if final.get("status") != "verified" or final.get("patch_count") != 226:
        raise ValueError("pre-GN source-final must verify all 226 patches")
    verify_files(src, final["outputs"])
    # Upload admission recorded the actual post-substitution script inputs.
    for name in source.load_manifest(repo)["toolchain_inputs"]:
        digest = donor["files"].get("src/" + name)
        if digest is None:
            raise ValueError("unbound checkpoint tool input: " + name)
        verify_files(src, {name: digest})
    tool_receipt = json.loads(source.regular(src, ".chromix-linux157-toolchain.json").read_bytes())
    if tool_receipt["recipe"] != receipt["recipe"] or tool_receipt["target_arch"] != arch:
        raise ValueError("pre-GN toolchain provenance mismatch")
    for name, digest in tool_receipt["binaries"].items():
        checked_binary(src, name, digest)
    source.get_platform(work / "tooling/ungoogled-chromium",
                        work / "tooling/ungoogled-chromium-portablelinux", repo)
    new_recipe = source.recipe(repo)
    validate_recipe_delta(receipt["recipe"], new_recipe)
    selection = current_selection(repo, src)
    compatible_selection(receipt["chromix_selection"], selection)
    # Preserve authenticated originals. Only provenance markers are migrated.
    backup = work / "linux157-pre-gn-origin"
    if backup.exists() or backup.is_symlink():
        raise ValueError("unexpected prior migration backup")
    backup.mkdir()
    names = (source.RECEIPT, ".chromix-linux157-toolchain.json", ".chromix-source-ready")
    original = {name: source.regular(src, name).read_bytes() for name in names}
    for name, raw in original.items():
        (backup / name).write_bytes(raw)
    (backup / "source-final.json").write_bytes(source.regular(work, "fingerprint-diagnostics/source-final.json").read_bytes())
    (backup / "args.gn").write_bytes(source.regular(src, "out/Chromix/args.gn").read_bytes())
    proof = {"schema_version": 1, "arch": arch, "metadata": metadata,
             "origin_recipe": receipt["recipe"], "current_recipe": new_recipe,
             "origin_files": {name: source.digest(raw) for name, raw in original.items()},
             "source_final_sha256": donor["files"]["fingerprint-diagnostics/source-final.json"],
             "phase": "exact-stage1-pre-gn", "runtime_verified": False}
    (src / PENDING).write_text(json.dumps(proof, sort_keys=True) + "\n")
    receipt.update(recipe=new_recipe, chromix_selection=selection)
    tool_receipt["recipe"] = new_recipe
    (src / source.RECEIPT).write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    (src / ".chromix-linux157-toolchain.json").write_text(json.dumps(tool_receipt, indent=2) + "\n")
    (src / ".chromix-source-ready").write_text(f"linux157-fresh|{arch}|{new_recipe['sha256']}\n")
    (src / MARKER).write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
    (src / PENDING).unlink()
    source.verify_receipt(src, repo)
    return proof


def verify_migrated(work: Path, arch: str, repo: Path = source.ROOT) -> dict:
    src = work / "src"
    if (src / PENDING).exists():
        raise ValueError("interrupted Linux157 checkpoint migration")
    proof = json.loads(source.regular(src, MARKER).read_bytes())
    donor = load(repo)["donors"][arch]
    if (proof.get("arch") != arch or proof.get("metadata") != expected(arch, repo)
            or proof.get("origin_recipe") != donor["source_receipt"]["recipe"]
            or proof.get("current_recipe") != source.recipe(repo)
            or proof.get("phase") != "exact-stage1-pre-gn"
            or proof.get("runtime_verified") is not False):
        raise ValueError("Linux157 migration identity mismatch")
    validate_recipe_delta(proof["origin_recipe"], proof["current_recipe"])
    if set(proof.get("origin_files", {})) != {source.RECEIPT, ".chromix-linux157-toolchain.json", ".chromix-source-ready"}:
        raise ValueError("incomplete migration origin markers")
    for name, digest in proof["origin_files"].items():
        if digest != donor["files"].get("src/" + name):
            raise ValueError("unreviewed origin marker")
        verify_files(work / "linux157-pre-gn-origin", {name: digest})
    verify_files(work / "linux157-pre-gn-origin", {
        "source-final.json": donor["files"]["fingerprint-diagnostics/source-final.json"],
        "args.gn": donor["files"]["src/out/Chromix/args.gn"]})
    return proof


def prepare_args(work: Path, arch: str, repo: Path = source.ROOT) -> None:
    donor = load(repo)["donors"][arch]
    original = source.regular(work / "linux157-pre-gn-origin", "args.gn").read_bytes()
    if source.digest(original) != donor["files"]["src/out/Chromix/args.gn"]:
        raise ValueError("changed original checkpoint GN arguments")
    revised = original
    for line in (b"rust_prebuilt_stdlib = false\n", b"use_chromium_rust_toolchain = true\n",
                 b"v8_use_metagen_instance_types=false\n"):
        if revised.count(line) != 1:
            raise ValueError("unexpected exact donor GN argument layout")
        revised = revised.replace(line, b"")
    path = source.regular(work, "src/out/Chromix/args.gn")
    current = path.read_bytes()
    if current not in (original, revised):
        raise ValueError("GN args differ beyond the three reviewed obsolete variables")
    if current != revised:
        path.write_bytes(revised)


def restore(arch: str, work: Path, repo: Path = source.ROOT) -> dict:
    try:
        from . import download_posix_snapshot as download
        from . import restore_posix_snapshot as unpack
    except ImportError:
        import download_posix_snapshot as download
        import restore_posix_snapshot as unpack
    if work.exists() or work.is_symlink():
        raise ValueError("exact checkpoint restore requires an absent work directory")
    m = load(repo)
    client = snapshot.Client(m["repository"], os.environ.get("GH_TOKEN", ""))
    metadata = validate_metadata(client, arch, repo)
    work.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="linux157-pre-gn-", dir=work.parent) as temporary:
        root = Path(temporary)
        manifest = root / "metadata.json"
        manifest.write_text(json.dumps(metadata) + "\n")
        volumes = root / "volumes"
        download.download_snapshot(manifest, volumes, root / "download.json")
        # ZIP digests above authenticate all tar bytes before extraction/publication.
        unpack.restore(volumes, work)
        (work / "linux157-checkpoint-download.json").write_bytes((root / "download.json").read_bytes())
    return admit(work, arch, metadata, repo)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("metadata", "admit", "restore"), required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--repo", type=Path, default=source.ROOT)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.phase == "metadata":
            result = validate_metadata(snapshot.Client(load(args.repo)["repository"], os.environ.get("GH_TOKEN", "")), args.arch, args.repo)
        elif args.phase == "admit" and args.workdir and args.metadata:
            result = admit(args.workdir.resolve(), args.arch, json.loads(args.metadata.read_text()), args.repo)
        elif args.phase == "restore" and args.workdir:
            result = restore(args.arch, args.workdir.absolute(), args.repo)
        else:
            parser.error("missing phase-specific paths")
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("Linux157 checkpoint: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
