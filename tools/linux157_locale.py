#!/usr/bin/env python3
"""Migrate one exact compiled Linux157 tree through the reviewed two-file locale API additions."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

try:
    from . import linux157_source as source
    from . import linux157_checkpoint as prior
    from . import linux157_libclang as libclang
    from . import linux157_toolchain as tools
    from . import validate_posix_snapshot as snapshot
    from . import apply_restored_patches as arp
    from . import patch_selection
except ImportError:
    import linux157_source as source
    import linux157_checkpoint as prior
    import linux157_libclang as libclang
    import linux157_toolchain as tools
    import validate_posix_snapshot as snapshot
    import apply_restored_patches as arp
    import patch_selection

MANIFEST = "build/linux/157.0.8103.78/locale-checkpoint.json"
MANIFEST_SHA256 = "42a6d9d6e9d40812cb4bee4077768213987586387f7f8816e08b9bd066993208"
TARGETS = ("base/i18n/icubridge/default_icu_locale.h",
           "third_party/blink/renderer/core/timezone/timezone_controller.cc")
OLD_COUNT = 226
NEW_COUNT = 228
ADDITION_COUNT = 2
MARKER = ".chromix-linux157-locale-migration.json"
PENDING = ".chromix-linux157-locale-migration-in-progress"
BACKUP = "linux157-locale-origin"
CHANGED = {"tools/linux157_source.py", "tools/linux157_build.py"}
ADDED = {"tools/linux157_locale.py", MANIFEST}


def load(repo=source.ROOT):
    raw = source.regular(repo, MANIFEST).read_bytes()
    if source.digest(raw) != MANIFEST_SHA256:
        raise ValueError("unreviewed compiled locale checkpoint manifest")
    return json.loads(raw)


def expected(arch, repo=source.ROOT):
    m = load(repo)
    if arch != m["arch"]:
        raise ValueError("locale recovery only admits the exact x64 compiled donor")
    return {"repository": m["repository"], "platform": "linux", "workflow": "build-linux157-x64-post-gn-resume",
            "run_id": m["run_id"], "attempt": m["attempt"], "stage": m["stage"], "arch": arch,
            "head_sha": m["head_sha"], "job_id": m["job_id"],
            "pattern": "chromix-linux-x64-tree-s1-attempt-1-part*",
            "artifacts": [{k: v for k, v in m["artifact"].items() if k != "expires_at"} | {"expired": False}]}


def validate_metadata(client, arch, repo=source.ROOT):
    want = expected(arch, repo)
    m = load(repo)
    run = client.get(f"/actions/runs/{m['run_id']}")
    if (run.get("id") != m["run_id"] or run.get("path") != m["workflow_path"]
            or run.get("head_sha") != m["head_sha"] or run.get("head_branch") != "main"
            or run.get("event") != "workflow_dispatch" or run.get("status") != "completed"
            or run.get("run_attempt") != 1 or run.get("conclusion") != "failure"
            or run.get("repository", {}).get("id") != m["repository_id"]
            or run.get("repository", {}).get("full_name") != m["repository"]
            or run.get("head_repository", {}).get("full_name") != m["repository"]):
        raise ValueError("compiled donor run identity mismatch")
    jobs = [j for j in client.items(f"/actions/runs/{m['run_id']}/attempts/1/jobs", "jobs") if j.get("id") == m["job_id"]]
    if len(jobs) != 1:
        raise ValueError("missing compiled donor job")
    job = jobs[0]
    steps = {s["name"]: s.get("conclusion") for s in job["steps"]}
    if (job.get("name") != m["job_name"] or job.get("conclusion") != "failure"
            or job.get("status") != "completed" or steps.get("Run stage 1") != "failure"
            or steps.get("Preserve latest failed Linux source/output checkpoint") != "success"
            or steps.get("Verify handoff snapshot") != "success"
            or any(steps.get(f"Upload tree part {i}") != "success" for i in range(1, 5))):
        raise ValueError("compiled donor did not preserve verified failed output")
    artifacts = [a for a in client.items(f"/actions/runs/{m['run_id']}/artifacts", "artifacts")
                 if a.get("name", "").startswith("chromix-linux-x64-tree-s1-attempt-1-part")]
    if len(artifacts) != 1:
        raise ValueError("unexpected compiled donor artifact set")
    artifact = artifacts[0]
    if (any(artifact.get(k) != v for k, v in m["artifact"].items())
            or artifact.get("expired") is not False
            or artifact.get("workflow_run", {}).get("id") != m["run_id"]
            or artifact.get("workflow_run", {}).get("head_sha") != m["head_sha"]
            or datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)):
        raise ValueError("compiled donor artifact changed or expired")
    return want


def recipe_delta(old, new):
    a, b = old["inputs"], new["inputs"]
    if (set(b) - set(a) != ADDED or set(a) - set(b)
            or {n for n in a if a[n] != b[n]} != CHANGED
            or {k: v for k, v in old.items() if k not in ("inputs", "sha256")} !=
               {k: v for k, v in new.items() if k not in ("inputs", "sha256")}):
        raise ValueError("locale recovery recipe delta is not reviewed")


def canonical_series(repo, identity, root_series):
    """Reproduce selector serialization, retaining root comments and line endings."""
    lines = root_series.decode("utf-8").splitlines(keepends=True)
    names = [line.split("#", 1)[0].strip() for line in lines]
    names = [name for name in names if name]
    if len(names) != 224 or len(set(names)) != len(names):
        raise ValueError("locale recovery requires the unchanged224 root series")
    entries = identity["patches"]
    additions = identity["selection"]["additions"]["patches"]
    if len(entries) != len(names) + len(additions) or entries[len(names):] != additions:
        raise ValueError("effective series additions do not match ordered patch identities")
    replacements = {"patches/" + Path(item["path"]).name: item["path"]
                    for item in identity["selection"]["overrides"]}
    mapped = [replacements.get(name, name) for name in names]
    if [entry["path"] for entry in entries[:len(names)]] != mapped:
        raise ValueError("effective root paths differ from reviewed224 overrides")
    mapping = dict(zip(names, mapped, strict=True))
    effective = "".join(line.replace(name, mapping[name], 1)
                        if (name := line.split("#", 1)[0].strip()) else line
                        for line in lines).encode("utf-8")
    if additions:
        if not effective.endswith(b"\n"):
            effective += b"\n"
        effective += "".join(entry["path"] + "\n" for entry in additions).encode("utf-8")
    return source.digest(effective)


def validate_additions(selected, raw, manifest, repo=source.ROOT):
    old = manifest["source_receipt"]["chromix_selection"]
    additions = manifest["migration"]["additions"]
    if (len(raw) != NEW_COUNT or len(selected["patches"]) != NEW_COUNT
            or selected["patches"][:-ADDITION_COUNT] != old["patches"]):
        raise ValueError("reviewed0236/0237 migration unavailable: require unchanged old226 plus exact two additions")
    if (manifest["migration"]["old_patch_count"] != OLD_COUNT
            or manifest["migration"]["new_patch_count"] != NEW_COUNT
            or tuple(entry["target"] for entry in additions) != TARGETS):
        raise ValueError("unreviewed locale migration surface")
    for offset, ((name, data), pin) in enumerate(zip(raw[-ADDITION_COUNT:], additions)):
        expected = {"path": pin["patch"], "sha256": pin["sha256"]}
        if (name != pin["patch"] or source.digest(data) != pin["sha256"]
                or selected["patches"][-ADDITION_COUNT + offset] != expected):
            raise ValueError("unreviewed locale API addition identity or order")
        _, entries = arp.transform_patch(data, set(), [])
        if len(entries) != 1 or entries[0][0] != pin["target"] or entries[0][1] != "modify":
            raise ValueError("each locale addition must modify exactly its approved source")
    old_meta, new_meta = old["selection"], selected["selection"]
    root_series = source.regular(repo, "patches/series").read_bytes()
    if (source.digest(root_series) != old_meta["base_series_sha256"]
            or new_meta["base_series_sha256"] != old_meta["base_series_sha256"]):
        raise ValueError("locale root224 series identity changed")
    if (canonical_series(repo, old, root_series) != old["series_sha256"]
            or canonical_series(repo, selected, root_series) != selected["series_sha256"]):
        raise ValueError("locale effective series digest does not match canonical ordered patches")
    for (name, data), entry in zip(raw, selected["patches"], strict=True):
        if (name != entry["path"] or source.digest(data) != entry["sha256"]
                or source.regular(repo, name).read_bytes() != data):
            raise ValueError("locale raw patch path/bytes mismatch: " + name)
    for key in ("version", "platform", "core_commit", "core_gitlink", "platform_commit", "platform_version", "overrides"):
        if old_meta.get(key) != new_meta.get(key):
            raise ValueError("unreviewed source/platform patch identity change")
    expected_additions = [{"path": pin["patch"], "sha256": pin["sha256"]} for pin in additions]
    if new_meta["additions"]["patches"] != old_meta["additions"]["patches"] + expected_additions:
        raise ValueError("previous additions changed")
    return additions


def selected_stacks(repo, core, tooling, src, manifest):
    selected, raw = patch_selection.select(repo, "linux", src=src, core=core)
    additions = validate_additions(selected, raw, manifest, repo)
    identity, stack, _ = arp._load(repo, core, tooling, "linux", src=src)
    if len(stack) != NEW_COUNT:
        raise ValueError("incomplete current locale stack")
    for item, pin in zip(stack[-ADDITION_COUNT:], additions):
        if len(item[2]) != 1 or item[2][0][0] != pin["target"] or item[2][0][1] != "modify":
            raise ValueError("domain transformation changed the approved locale surface")
    return selected, identity, stack


def snapshot_outputs(out):
    result = {}
    for directory, dirs, files in os.walk(out, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            relative = path.relative_to(out).as_posix()
            result[relative] = (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ino,
                                os.readlink(path) if stat.S_ISLNK(info.st_mode) else None)
    return result


def apply_one(stage, patch, reverse, root):
    path = root / "current.patch"
    path.write_bytes(patch[1])
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CEILING_DIRECTORIES=str(root))
    args = ["git", "-c", "core.autocrlf=false", "apply", "--no-index", "--whitespace=nowarn"]
    if reverse:
        args.append("--reverse")
    result = subprocess.run([*args, str(path)], cwd=stage, env=env, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError(f"private stack {'reverse' if reverse else 'forward'} failed at {patch[0]}: " + result.stderr.decode(errors="replace"))
    for name, action, _ in patch[2]:
        if ((action == "create" and reverse) or (action == "delete" and not reverse)) and (stage / name).exists():
            raise ValueError("patch did not remove expected file: " + name)


def private_migration(src, stack, temporary_root):
    if (len(stack) != NEW_COUNT or any(
            len(item[2]) != 1 or item[2][0][0] != target or item[2][0][1] != "modify"
            for item, target in zip(stack[-ADDITION_COUNT:], TARGETS))):
        raise ValueError("private replay requires the complete228 stack and exact ordered two-source additions")
    names = {entry[0] for _, _, entries in stack for entry in entries}
    before = arp._snapshot(src, names)
    with tempfile.TemporaryDirectory(prefix="locale-stack-", dir=temporary_root) as temporary:
        root = Path(temporary)
        stage = root / "src"
        stage.mkdir()
        for name, (raw, _) in before.items():
            if raw is not None:
                p = stage / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw.replace(b"\r\n", b"\n"))
        old = stack[:-ADDITION_COUNT]
        for reverse, sequence in ((True, reversed(old)), (False, old)):
            for item in sequence:
                apply_one(stage, item, reverse, root)
        for name, (raw, _) in before.items():
            p = stage / name
            if (p.read_bytes() if p.exists() else None) != (raw.replace(b"\r\n", b"\n") if raw is not None else None):
                raise ValueError("old226 private roundtrip changed source: " + name)
        for item in stack[-ADDITION_COUNT:]:
            apply_one(stage, item, False, root)
        expected = {name: (stage / name).read_bytes() if (stage / name).exists() else None for name in names}
        for name, raw in expected.items():
            old_raw = before[name][0]
            if name not in TARGETS and raw != (old_raw.replace(b"\r\n", b"\n") if old_raw is not None else None):
                raise ValueError("locale migration changed an unrelated source")
        for reverse, sequence in ((True, reversed(stack)), (False, stack)):
            for item in sequence:
                apply_one(stage, item, reverse, root)
        for name, raw in expected.items():
            p = stage / name
            if (p.read_bytes() if p.exists() else None) != raw:
                raise ValueError("new228 private roundtrip changed source: " + name)
        for name, (raw, info) in before.items():
            p = src / name
            if ((p.read_bytes() if p.exists() else None) != raw
                    or (info and p.stat().st_mtime_ns != info.st_mtime_ns)):
                raise ValueError("source changed concurrently during private replay")
        return before, expected


def verify_ninja(work, repo=source.ROOT):
    try:
        from . import restore_ninja
    except ImportError:
        import restore_ninja
    pin = load(repo)["ninja"]
    chosen = shutil.which("ninja")
    if not chosen or restore_ninja.host_identity() != tuple(pin["host"]):
        raise ValueError("compiled locale recovery requires native donor-compatible Ninja")
    path = Path(chosen).resolve(strict=True)
    if ("x64" not in restore_ninja.binary_architectures(path, "linux")
            or tools.file_hash(path) != pin["sha256"]
            or restore_ninja.probe_version(path, work) != pin["version"]):
        raise ValueError("Ninja differs from compiled checkpoint; preserve its logs")
    return path


def atomic_bytes(path, raw, *, mode=None):
    if path.is_symlink():
        raise ValueError("linked publication path")
    descriptor, name = tempfile.mkstemp(prefix=".locale-publish-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if mode is not None:
            os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def publish_sources(src, before, revised):
    """The pair stays unready until both replacements and all receipts are committed."""
    if not (src / PENDING).is_file() or not (src / ".chromix-layer-in-progress").is_file():
        raise ValueError("two-file publication requires both transaction blockers")
    if set(TARGETS) - before.keys() or set(TARGETS) - revised.keys():
        raise ValueError("incomplete two-file publication")
    for name in TARGETS:
        raw, info = before[name]
        path = source.regular(src, name)
        if path.read_bytes() != raw or path.stat().st_mtime_ns != info.st_mtime_ns:
            raise ValueError("locale source changed before atomic publication")
    for name in TARGETS:
        atomic_bytes(src / name, revised[name], mode=stat.S_IMODE(before[name][1].st_mode))


def admit(work, arch, metadata, repo=source.ROOT):
    m = load(repo)
    if metadata != expected(arch, repo):
        raise ValueError("locale metadata tuple mismatch")
    src = work / "src"
    for name in (PENDING, MARKER, source.IN_PROGRESS, ".chromix-layer-in-progress", ".chromix-domain-substitution-in-progress",
                 ".chromix-linux157-libclang-in-progress", ".chromix-upstream-restored.json"):
        if (src / name).exists() or (src / name).is_symlink():
            raise ValueError("partial or already migrated locale tree")
    downloaded = json.loads(source.regular(work, "linux157-locale-download.json").read_bytes())
    if (downloaded.get("status") != "success" or downloaded.get("publication") != "published"
            or downloaded.get("run_id") != m["run_id"] or downloaded.get("head_sha") != m["head_sha"]
            or downloaded.get("repository") != m["repository"] or len(downloaded.get("artifacts", [])) != 1
            or any(downloaded["artifacts"][0].get(k) != v for k, v in metadata["artifacts"][0].items())):
        raise ValueError("compiled artifact download verification missing")
    prior.verify_files(work, m["files"])
    old = json.loads(source.regular(src, source.RECEIPT).read_bytes())
    if old != m["source_receipt"] or old.get("stage") != "chromix-applied":
        raise ValueError("compiled source preparation identity changed")
    prior.verify_files(src, old["substituted_outputs"])
    prior.verify_files(src, {pin["target"]: pin["before"] for pin in m["migration"]["additions"]})
    final = json.loads(source.regular(work, "fingerprint-diagnostics/source-final.json").read_bytes())
    if final != m["source_final"] or final.get("patch_count") != 226:
        raise ValueError("compiled source-final does not match old226")
    prior.verify_files(src, final["outputs"])
    tool = json.loads(source.regular(src, ".chromix-linux157-toolchain.json").read_bytes())
    if tool.get("recipe") != old["recipe"] or tool.get("target_arch") != arch:
        raise ValueError("compiled toolchain receipt mismatch")
    for name, digest in tool["binaries"].items():
        prior.checked_binary(src, name, digest)
    libclang.verify_package(src / libclang.PACKAGE, libclang.load(repo))
    core, tooling = work / "tooling/ungoogled-chromium", work / "tooling/ungoogled-chromium-portablelinux"
    source.get_platform(core, tooling, repo)
    new_recipe = source.recipe(repo)
    recipe_delta(old["recipe"], new_recipe)
    selected, identity, stack = selected_stacks(repo, core, tooling, src, m)
    out_before = snapshot_outputs(src / "out")
    before, revised = private_migration(src, stack, work)
    changed = {name for name in revised if revised[name] != (before[name][0].replace(b"\r\n", b"\n") if before[name][0] is not None else None)}
    if changed != set(TARGETS):
        raise ValueError("locale additions must change exactly the two approved sources")
    for pin in m["migration"]["additions"]:
        name = pin["target"]
        if source.digest(before[name][0]) != pin["before"] or source.digest(revised[name]) != pin["after"]:
            raise ValueError("locale source preimage/postimage mismatch: " + name)
    if snapshot_outputs(src / "out") != out_before:
        raise ValueError("GN/PGO/Ninja output changed during migration")
    prior.verify_files(work, m["files"])
    if source.recipe(repo) != new_recipe or patch_selection.select(repo, "linux", src=src, core=core)[0] != selected:
        raise ValueError("recipe or patch review changed during private replay")
    backup = work / BACKUP
    if backup.exists() or backup.is_symlink():
        raise ValueError("existing locale origin backup")
    backup.mkdir()
    originals = {source.RECEIPT: source.regular(src, source.RECEIPT).read_bytes(),
                 ".chromix-linux157-toolchain.json": source.regular(src, ".chromix-linux157-toolchain.json").read_bytes(),
                 ".chromix-source-ready": source.regular(src, ".chromix-source-ready").read_bytes(),
                 "source-final.json": source.regular(work, "fingerprint-diagnostics/source-final.json").read_bytes(),
                 **{Path(name).name: before[name][0] for name in TARGETS}}
    for name, raw in originals.items():
        (backup / name).write_bytes(raw)
    proof = {"schema_version": 1, "phase": "exact-compiled-locale", "arch": arch, "metadata": metadata,
             "origin_recipe": old["recipe"], "current_recipe": new_recipe, "old_patch_count": 226,
             "new_patch_count": NEW_COUNT, "patches": selected["patches"][-ADDITION_COUNT:],
             "before": {pin["target"]: pin["before"] for pin in m["migration"]["additions"]},
             "after": {pin["target"]: pin["after"] for pin in m["migration"]["additions"]},
             "originals": {n: source.digest(b) for n, b in originals.items()}, "runtime_verified": False}
    # Publication is guarded until source, receipts and source-final are committed together.
    with (src / PENDING).open("xb") as stream:
        stream.write(json_bytes(proof))
        stream.flush()
        os.fsync(stream.fileno())
    with (src / ".chromix-layer-in-progress").open("xb") as stream:
        stream.write(json_bytes(proof))
        stream.flush()
        os.fsync(stream.fileno())
    publish_sources(src, before, revised)
    receipt = copy.deepcopy(old)
    receipt.update(recipe=new_recipe, chromix_selection=selected)
    for name in TARGETS:
        receipt["prepared_outputs"][name] = source.digest(revised[name])
        receipt["substituted_outputs"][name] = source.digest(revised[name])
    tool["recipe"] = new_recipe
    new_final = {"schema_version": 1, "status": "verified", "method": "reverse-forward-in-scratch",
                 "qualification": "complete old226 and new228 private roundtrips; exact two approved sources published",
                 "domain_substituted": True, "patch_count": NEW_COUNT, "identity": identity,
                 "outputs": {n: source.digest(b) if b is not None else None for n, b in revised.items()}}
    atomic_bytes(src / source.RECEIPT, json_bytes(receipt))
    atomic_bytes(src / ".chromix-linux157-toolchain.json", json_bytes(tool))
    atomic_bytes(work / "fingerprint-diagnostics/source-final.json", json_bytes(new_final))
    atomic_bytes(src / ".chromix-source-ready", f"linux157-fresh|{arch}|{new_recipe['sha256']}\n".encode())
    if snapshot_outputs(src / "out") != out_before:
        raise ValueError("compiled outputs changed during publication")
    prior.verify_files(src, new_final["outputs"])
    if source.recipe(repo) != new_recipe or patch_selection.select(repo, "linux", src=src, core=core)[0] != selected:
        raise ValueError("review changed during locale publication")
    atomic_bytes(src / MARKER, json_bytes(proof))
    (src / ".chromix-layer-in-progress").unlink()
    (src / PENDING).unlink()
    source.verify_receipt(src, repo)
    return proof


def verify(work, arch, repo=source.ROOT):
    src = work / "src"
    if any((src / name).exists() or (src / name).is_symlink() for name in (
            PENDING, ".chromix-layer-in-progress", source.IN_PROGRESS,
            ".chromix-linux157-libclang-in-progress", ".chromix-domain-substitution-in-progress")):
        raise ValueError("incomplete locale migration")
    m = load(repo)
    proof = json.loads(source.regular(src, MARKER).read_bytes())
    selected, raw = patch_selection.select(repo, "linux", src=src)
    additions = validate_additions(selected, raw, m, repo)
    if (arch != "x64" or proof.get("metadata") != expected(arch, repo)
            or proof.get("origin_recipe") != m["source_receipt"]["recipe"]
            or proof.get("current_recipe") != source.recipe(repo) or proof.get("phase") != "exact-compiled-locale"
            or proof.get("new_patch_count") != NEW_COUNT or proof.get("patches") != selected["patches"][-ADDITION_COUNT:]
            or proof.get("runtime_verified") is not False):
        raise ValueError("locale migration identity mismatch")
    recipe_delta(proof["origin_recipe"], proof["current_recipe"])
    expected_originals = {source.RECEIPT: m["files"]["src/" + source.RECEIPT],
                          ".chromix-linux157-toolchain.json": m["files"]["src/.chromix-linux157-toolchain.json"],
                          ".chromix-source-ready": m["files"]["src/.chromix-source-ready"],
                          "source-final.json": m["files"]["fingerprint-diagnostics/source-final.json"],
                          **{Path(pin["target"]).name: pin["before"] for pin in additions}}
    if proof.get("originals") != expected_originals:
        raise ValueError("locale origin backups do not match exact compiled donor")
    prior.verify_files(work / BACKUP, expected_originals)
    expected_before = {pin["target"]: pin["before"] for pin in additions}
    expected_after = {pin["target"]: pin["after"] for pin in additions}
    if proof.get("before") != expected_before or proof.get("after") != expected_after:
        raise ValueError("locale source preimage/postimage identity mismatch")
    receipt = json.loads(source.regular(src, source.RECEIPT).read_bytes())
    if receipt.get("chromix_selection") != selected or receipt.get("recipe") != proof["current_recipe"]:
        raise ValueError("locale source receipt is not bound to the current228 review")
    for field in ("prepared_outputs", "substituted_outputs"):
        outputs = dict(m["source_receipt"][field])
        outputs.update(expected_after)
        if receipt.get(field) != outputs:
            raise ValueError("locale receipt changed beyond the exact two-source delta")
    prior.verify_files(src, expected_after)
    prior.verify_files(work, {"src/out/Chromix/args.gn": m["files"]["src/out/Chromix/args.gn"]})
    libclang.verify_package(src / libclang.PACKAGE, libclang.load(repo))
    return proof


def restore(work, arch, repo=source.ROOT):
    if work.exists() or work.is_symlink():
        raise ValueError("compiled restore requires absent workdir")
    m = load(repo)
    selected, patches = patch_selection.select(repo, "linux")
    validate_additions(selected, patches, m, repo)
    metadata = validate_metadata(snapshot.Client(m["repository"], os.environ.get("GH_TOKEN", "")), arch, repo)
    work.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="linux157-locale-", dir=work.parent) as temp:
        root = Path(temp)
        (root / "metadata.json").write_bytes(json_bytes(metadata))
        subprocess.run([sys.executable, str(repo / "tools/download_posix_snapshot.py"),
                        "--manifest", str(root / "metadata.json"), "--destination", str(root / "volumes"),
                        "--report", str(root / "download.json")], check=True)
        subprocess.run([sys.executable, str(repo / "tools/restore_posix_snapshot.py"),
                        str(root / "volumes"), str(work)], check=True)
        (work / "linux157-locale-download.json").write_bytes((root / "download.json").read_bytes())
    return admit(work, arch, metadata, repo)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("metadata", "restore", "admit", "verify"), required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--repo", type=Path, default=source.ROOT)
    args = parser.parse_args(argv)
    try:
        if args.phase == "metadata":
            result = validate_metadata(snapshot.Client(load(args.repo)["repository"], os.environ.get("GH_TOKEN", "")), args.arch, args.repo)
        elif args.phase == "restore" and args.workdir:
            result = restore(args.workdir.absolute(), args.arch, args.repo)
        elif args.phase == "admit" and args.workdir and args.metadata:
            result = admit(args.workdir.resolve(), args.arch, json.loads(args.metadata.read_text()), args.repo)
        elif args.phase == "verify" and args.workdir:
            result = verify(args.workdir.resolve(), args.arch, args.repo)
        else:
            parser.error("missing phase-specific arguments")
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("Linux157 locale recovery: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
