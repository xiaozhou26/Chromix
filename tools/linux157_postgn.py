#!/usr/bin/env python3
"""Admit the exact latest post-GN Linux157 tree for the libclang-only repair."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

try:
    from . import linux157_source as source
    from . import linux157_checkpoint as prior
    from . import linux157_toolchain as tools
    from . import validate_posix_snapshot as snapshot
except ImportError:
    import linux157_source as source
    import linux157_checkpoint as prior
    import linux157_toolchain as tools
    import validate_posix_snapshot as snapshot

MANIFEST = "build/linux/157.0.8103.78/post-gn-checkpoints.json"
MANIFEST_SHA256 = "f3f06013790fd784fb2801b1876a8525dc5cbf5d34f67443b8da94d5175f1959"
MARKER = ".chromix-linux157-libclang-migration.json"
PENDING = ".chromix-linux157-libclang-migration-in-progress"
CHANGED = {"tools/linux157_source.py", "tools/linux157_build.py"}
ADDED = {"tools/linux157_libclang.py", "tools/linux157_postgn.py",
         "build/linux/157.0.8103.78/libclang.json", MANIFEST}


def load(repo=source.ROOT):
    raw = source.regular(repo, MANIFEST).read_bytes()
    if source.digest(raw) != MANIFEST_SHA256:
        raise ValueError("unreviewed post-GN donor manifest")
    return json.loads(raw)


def expected(arch, repo=source.ROOT):
    m = load(repo)
    if arch not in m["donors"]:
        raise ValueError("no reviewed latest post-GN donor for " + arch)
    d = m["donors"][arch]
    return {"repository": m["repository"], "platform": "linux", "workflow": "build-linux157-" + arch + "-pre-gn-resume",
            "run_id": d["run_id"], "attempt": 1, "stage": 1, "arch": arch,
            "head_sha": m["head_sha"], "job_id": d["job_id"],
            "pattern": f"chromix-linux-{arch}-tree-s1-attempt-1-part*",
            "artifacts": [{k: v for k, v in d["artifact"].items() if k != "expires_at"} | {"expired": False}]}


def validate_metadata(client, arch, repo=source.ROOT):
    want = expected(arch, repo)
    m = load(repo)
    d = m["donors"][arch]
    run = client.get(f"/actions/runs/{d['run_id']}")
    if (run.get("id") != d["run_id"] or run.get("path") != d["workflow_path"]
            or run.get("head_sha") != m["head_sha"] or run.get("head_branch") != "main"
            or run.get("event") != "workflow_dispatch" or run.get("status") != "completed"
            or run.get("run_attempt") != 1 or run.get("conclusion") != "failure"
            or run.get("repository", {}).get("id") != m["repository_id"]
            or run.get("repository", {}).get("full_name") != m["repository"]
            or run.get("head_repository", {}).get("full_name") != m["repository"]):
        raise ValueError("post-GN run identity changed")
    selected = [j for j in client.items(f"/actions/runs/{d['run_id']}/attempts/1/jobs", "jobs")
                if j.get("id") == d["job_id"]]
    if len(selected) != 1:
        raise ValueError("post-GN producer job missing")
    job = selected[0]
    steps = {s["name"]: s.get("conclusion") for s in job["steps"]}
    if (job["name"] != d["job_name"] or job.get("status") != "completed" or job["conclusion"] != "failure"
            or steps.get("Run stage 1") != "failure"
            or steps.get("Preserve latest failed Linux source/output checkpoint") != "success"
            or steps.get("Verify handoff snapshot") != "success"
            or any(steps.get(f"Upload tree part {i}") != "success" for i in range(1, 5))):
        raise ValueError("post-GN donor failure/preservation mismatch")
    artifacts = [a for a in client.items(f"/actions/runs/{d['run_id']}/artifacts", "artifacts")
                 if a.get("name", "").startswith(f"chromix-linux-{arch}-tree-s1-attempt-1-part")]
    if len(artifacts) != 1:
        raise ValueError("unexpected post-GN artifact set")
    artifact = artifacts[0]
    if (any(artifact.get(k) != v for k, v in d["artifact"].items())
            or artifact.get("expired") is not False
            or artifact.get("workflow_run", {}).get("id") != d["run_id"]
            or artifact.get("workflow_run", {}).get("head_sha") != m["head_sha"]
            or datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)):
        raise ValueError("post-GN artifact expired or identity changed")
    return want


def delta(old, new):
    a, b = old["inputs"], new["inputs"]
    if (set(b) - set(a) != ADDED or set(a) - set(b)
            or {n for n in a if a[n] != b[n]} != CHANGED
            or {k: v for k, v in old.items() if k not in ("inputs", "sha256")} !=
               {k: v for k, v in new.items() if k not in ("inputs", "sha256")}):
        raise ValueError("post-GN migration allows only the reviewed libclang helpers")


def current_selection(repo, src):
    try:
        from . import patch_selection
    except ImportError:
        import patch_selection
    return patch_selection.select(repo, "linux", src=src)[0]


def admit(work, arch, metadata, repo=source.ROOT):
    if json.dumps(metadata, sort_keys=True) != json.dumps(expected(arch, repo), sort_keys=True):
        raise ValueError("wrong post-GN donor metadata")
    m = load(repo)
    d = m["donors"][arch]
    src = work / "src"
    download = json.loads(source.regular(work, "linux157-postgn-download.json").read_bytes())
    if (download.get("status") != "success" or download.get("publication") != "published"
            or download.get("head_sha") != m["head_sha"] or download.get("run_id") != d["run_id"]
            or download.get("repository") != m["repository"] or len(download.get("artifacts", [])) != 1
            or any(download["artifacts"][0].get(k) != v for k, v in metadata["artifacts"][0].items())):
        raise ValueError("missing exact post-GN artifact download verification")
    if any((src / n).exists() or (src / n).is_symlink() for n in
           (PENDING, MARKER, source.IN_PROGRESS, ".chromix-domain-substitution-in-progress", ".chromix-upstream-restored.json")):
        raise ValueError("partial or already migrated post-GN tree")
    prior.verify_files(work, d["files"])
    receipt = json.loads(source.regular(src, source.RECEIPT).read_bytes())
    if receipt != d["source_receipt"] or receipt["target_arch"] != arch:
        raise ValueError("post-GN source receipt mismatch")
    prior.verify_files(src, receipt["substituted_outputs"])
    final = json.loads(source.regular(work, "fingerprint-diagnostics/source-final.json").read_bytes())
    if final.get("status") != "verified" or final.get("patch_count") != 226:
        raise ValueError("post-GN source-final incomplete")
    prior.verify_files(src, final["outputs"])
    tool = json.loads(source.regular(src, ".chromix-linux157-toolchain.json").read_bytes())
    if tool["recipe"] != receipt["recipe"] or tool["target_arch"] != arch:
        raise ValueError("post-GN toolchain identity mismatch")
    for name, digest in tool["binaries"].items():
        prior.checked_binary(src, name, digest)
    source.get_platform(work / "tooling/ungoogled-chromium", work / "tooling/ungoogled-chromium-portablelinux", repo)
    new = source.recipe(repo)
    delta(receipt["recipe"], new)
    selection = current_selection(repo, src)
    prior.compatible_selection(receipt["chromix_selection"], selection)
    backup = work / "linux157-postgn-origin"
    if backup.exists() or backup.is_symlink():
        raise ValueError("unexpected post-GN origin directory")
    backup.mkdir()
    names = (source.RECEIPT, ".chromix-linux157-toolchain.json", ".chromix-source-ready")
    originals = {n: source.regular(src, n).read_bytes() for n in names}
    for n, raw in originals.items():
        (backup / n).write_bytes(raw)
    for n, p in (("args.gn", src / "out/Chromix/args.gn"),
                 ("source-final.json", work / "fingerprint-diagnostics/source-final.json")):
        (backup / n).write_bytes(p.read_bytes())
    proof = {"schema_version": 1, "phase": "exact-post-GN-libclang", "arch": arch,
             "metadata": metadata, "origin_recipe": receipt["recipe"], "current_recipe": new,
             "originals": {n: source.digest(b) for n, b in originals.items()}, "runtime_verified": False}
    (src / PENDING).write_text(json.dumps(proof) + "\n")
    receipt.update(recipe=new, chromix_selection=selection)
    tool["recipe"] = new
    (src / source.RECEIPT).write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    (src / ".chromix-linux157-toolchain.json").write_text(json.dumps(tool, indent=2) + "\n")
    (src / ".chromix-source-ready").write_text(f"linux157-fresh|{arch}|{new['sha256']}\n")
    (src / MARKER).write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
    (src / PENDING).unlink()
    source.verify_receipt(src, repo)
    return proof


def verify(work, arch, repo=source.ROOT):
    src = work / "src"
    if (src / PENDING).exists() or (src / ".chromix-linux157-libclang-in-progress").exists():
        raise ValueError("interrupted post-GN migration or libclang preparation")
    proof = json.loads(source.regular(src, MARKER).read_bytes())
    donor = load(repo)["donors"].get(arch)
    if (not donor or proof.get("metadata") != expected(arch, repo) or proof.get("arch") != arch
            or proof.get("origin_recipe") != donor["source_receipt"]["recipe"]
            or proof.get("current_recipe") != source.recipe(repo)
            or proof.get("phase") != "exact-post-GN-libclang" or proof.get("runtime_verified") is not False):
        raise ValueError("post-GN migration identity mismatch")
    delta(proof["origin_recipe"], proof["current_recipe"])
    names = {source.RECEIPT, ".chromix-linux157-toolchain.json", ".chromix-source-ready"}
    if set(proof.get("originals", {})) != names:
        raise ValueError("incomplete post-GN origins")
    for name, digest in proof["originals"].items():
        if digest != donor["files"].get("src/" + name):
            raise ValueError("wrong original post-GN marker")
        prior.verify_files(work / "linux157-postgn-origin", {name: digest})
    prior.verify_files(work / "linux157-postgn-origin", {
        "args.gn": donor["files"]["src/out/Chromix/args.gn"],
        "source-final.json": donor["files"]["fingerprint-diagnostics/source-final.json"]})
    prior.verify_files(work, {"src/out/Chromix/args.gn": donor["files"]["src/out/Chromix/args.gn"]})
    package_receipt = src / ".chromix-linux157-libclang.json"
    if package_receipt.exists() or package_receipt.is_symlink():
        try:
            from . import linux157_libclang as libclang
        except ImportError:
            import linux157_libclang as libclang
        manifest = libclang.load(repo)
        data = json.loads(source.regular(src, ".chromix-linux157-libclang.json").read_bytes())
        if (data.get("package_sha256") != manifest["download"]["sha256"]
                or data.get("files") != manifest["files"] or data.get("host") != "linux-x64"
                or data.get("probe", {}).get("version") != manifest["clang_version"]
                or data.get("probe", {}).get("metagen") != "instance-types-and-layouts-passed"):
            raise ValueError("post-GN libclang receipt changed")
        libclang.verify_package(src / libclang.PACKAGE, manifest)
    return proof


def restore(work, arch, repo=source.ROOT):
    try:
        from . import download_posix_snapshot as download
        from . import restore_posix_snapshot as unpack
    except ImportError:
        import download_posix_snapshot as download
        import restore_posix_snapshot as unpack
    if work.exists() or work.is_symlink():
        raise ValueError("post-GN restore requires absent workdir")
    m = load(repo)
    metadata = validate_metadata(snapshot.Client(m["repository"], os.environ.get("GH_TOKEN", "")), arch, repo)
    work.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="linux157-postgn-", dir=work.parent) as temporary:
        root = Path(temporary)
        (root / "metadata.json").write_text(json.dumps(metadata))
        download.download_snapshot(root / "metadata.json", root / "volumes", root / "download.json")
        unpack.restore(root / "volumes", work)
        (work / "linux157-postgn-download.json").write_bytes((root / "download.json").read_bytes())
    return admit(work, arch, metadata, repo)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=("metadata", "restore", "admit"), required=True)
    p.add_argument("--arch", choices=("x64", "arm64"), required=True)
    p.add_argument("--workdir", type=Path)
    p.add_argument("--metadata", type=Path)
    p.add_argument("--repo", type=Path, default=source.ROOT)
    a = p.parse_args(argv)
    try:
        if a.phase == "metadata":
            result = validate_metadata(snapshot.Client(load(a.repo)["repository"], os.environ.get("GH_TOKEN", "")), a.arch, a.repo)
        elif a.phase == "restore" and a.workdir:
            result = restore(a.workdir.absolute(), a.arch, a.repo)
        elif a.phase == "admit" and a.workdir and a.metadata:
            result = admit(a.workdir.resolve(), a.arch, json.loads(a.metadata.read_text()), a.repo)
        else:
            p.error("missing phase paths")
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        print("Linux157 post-GN: " + str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
