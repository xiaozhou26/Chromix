#!/usr/bin/env python3
"""Migrate one reviewed PVT link dependency in the exact x64 stage2 compiled tree."""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
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

MANIFEST = "build/linux/157.0.8103.78/x64-pvt-link-checkpoint.json"
MANIFEST_SHA256 = "bcc6f630679fbcea57d5e2803b034d5208d03289d2776b7c0b4d5ffd6ac101ba"
OLD_COUNT = 232
NEW_COUNT = 233
ADDITION_COUNT = 1
MARKER = ".chromix-linux157-pvt-link-migration.json"
PENDING = ".chromix-linux157-pvt-link-migration-in-progress"
BACKUP = "linux157-pvt-link-origin"
TARGET = "chrome/browser/browsing_data/chrome_browsing_data_remover_delegate_factory.cc"


def load(repo=source.ROOT, arch="x64"):
    if arch != "x64":
        raise ValueError("PVT link donor is x64 only")
    raw = source.regular(repo, MANIFEST).read_bytes()
    if source.digest(raw) != MANIFEST_SHA256:
        raise ValueError("unreviewed compiled PVT link checkpoint manifest")
    return json.loads(raw)


def review(manifest):
    migration = manifest["migration"]
    if migration.get("status") != "verified":
        raise ValueError("PVT link patch scope awaits strict parent review")
    additions, targets = migration["additions"], migration["sources"]
    if (migration["old_patch_count"] != OLD_COUNT or len(additions) != ADDITION_COUNT
            or set(targets) != {TARGET} or migration["new_patch_count"] != NEW_COUNT):
        raise ValueError("invalid reviewed PVT link patch count/surface")
    for name, pin in targets.items():
        arp._relative(name)
        if (not name or name.startswith(("/", "out/", ".")) or ".." in Path(name).parts
                or "\\" in name or ":" in name or name.endswith((".gn", ".gni"))
                or set(pin) != {"before", "after"}
                or any(not re.fullmatch(r"[0-9a-f]{64}", h) for h in pin.values())
                or pin["before"] == pin["after"]):
            raise ValueError("invalid reviewed PVT link source: " + name)
    paths = [entry["patch"] for entry in additions]
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate PVT link addition")
    for entry in additions:
        arp._relative(entry["patch"])
        if (not entry["patch"].startswith("patches/chromium157-additions/0242-")
                or ".." in Path(entry["patch"]).parts
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                or len(entry["targets"]) != 1
                or set(entry["targets"]) - targets.keys()):
            raise ValueError("invalid reviewed PVT link patch")
    if set().union(*(set(p["targets"]) for p in additions)) != set(targets):
        raise ValueError("reviewed PVT link source coverage mismatch")
    return migration


def expected(arch, repo=source.ROOT):
    m = load(repo, arch)
    if arch != m["arch"]:
        raise ValueError("storage recovery architecture does not match its fixed donor")
    return {"repository": m["repository"], "platform": "linux", "workflow": Path(m["workflow_path"]).stem,
            "run_id": m["run_id"], "attempt": m["attempt"], "stage": m["stage"], "arch": arch,
            "head_sha": m["head_sha"], "job_id": m["job_id"],
            "pattern": f"chromix-linux-{arch}-tree-s{m['stage']}-attempt-{m['attempt']}-part*",
            "artifacts": [{k: v for k, v in m["artifact"].items() if k != "expires_at"} | {"expired": False}]}


def validate_metadata(client, arch, repo=source.ROOT):
    want = expected(arch, repo)
    m = load(repo, arch)
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
    if (job.get("run_id") != m["run_id"] or job.get("run_attempt") != m["attempt"]
            or job.get("head_sha") != m["head_sha"]
            or job.get("name") != m["job_name"] or job.get("conclusion") != "failure"
            or job.get("status") != "completed" or steps.get(f"Run stage {m['stage']}") != "failure"
            or steps.get("Preserve latest failed Linux source/output checkpoint") != "success"
            or steps.get("Verify handoff snapshot") != "success"
            or any(steps.get(f"Upload tree part {i}") != "success" for i in range(1, 5))):
        raise ValueError("compiled donor did not preserve verified failed output")
    artifacts = [a for a in client.items(f"/actions/runs/{m['run_id']}/artifacts", "artifacts")
                 if a.get("name", "").startswith(f"chromix-linux-{arch}-tree-s{m['stage']}-attempt-{m['attempt']}-part")]
    if len(artifacts) != 1:
        raise ValueError("unexpected compiled donor artifact set")
    artifact = artifacts[0]
    if (any(artifact.get(k) != v for k, v in m["artifact"].items())
            or artifact.get("expired") is not False
            or artifact.get("workflow_run", {}).get("id") != m["run_id"]
            or artifact.get("workflow_run", {}).get("head_sha") != m["head_sha"]
            or artifact.get("workflow_run", {}).get("repository_id") != m["repository_id"]
            or artifact.get("workflow_run", {}).get("head_repository_id") != m["repository_id"]
            or artifact.get("workflow_run", {}).get("head_branch") != "main"
            or datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)):
        raise ValueError("compiled donor artifact changed or expired")
    return want


def recipe_delta(old, new, repo=source.ROOT, arch="x64"):
    manifest = load(repo, arch)
    if old != manifest["source_receipt"]["recipe"] or new != manifest["target_recipe"] or old != new:
        raise ValueError("PVT link recovery must retain the exact compiled recipe")


def canonical_series(repo, identity, root_series):
    """Reproduce selector serialization, retaining root comments and line endings."""
    lines = root_series.decode("utf-8").splitlines(keepends=True)
    names = [line.split("#", 1)[0].strip() for line in lines]
    names = [name for name in names if name]
    if len(names) != 224 or len(set(names)) != len(names):
        raise ValueError("storage recovery requires the unchanged224 root series")
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
    migration = review(manifest)
    old = manifest["source_receipt"]["chromix_selection"]
    additions = migration["additions"]
    if (len(raw) != migration["new_patch_count"] or len(selected["patches"]) != len(raw)
            or selected["patches"][:OLD_COUNT] != old["patches"]):
        raise ValueError("PVT link recovery requires unchanged full old232 plus reviewed additions")
    for (name, data), pin in zip(raw[OLD_COUNT:], additions, strict=True):
        if name != pin["patch"] or source.digest(data) != pin["sha256"]:
            raise ValueError("unreviewed PVT link addition identity/order")
        _, entries = arp.transform_patch(data, set(), [])
        validate_surface(entries, pin)
    old_meta, new_meta = old["selection"], selected["selection"]
    root_series = source.regular(repo, "patches/series").read_bytes()
    if (source.digest(root_series) != old_meta["base_series_sha256"]
            or new_meta["base_series_sha256"] != old_meta["base_series_sha256"]
            or canonical_series(repo, old, root_series) != old["series_sha256"]
            or canonical_series(repo, selected, root_series) != selected["series_sha256"]):
        raise ValueError("PVT link root/effective series identity changed")
    for (name, data), entry in zip(raw, selected["patches"], strict=True):
        if ({"path": name, "sha256": source.digest(data)} != entry
                or source.regular(repo, name).read_bytes() != data):
            raise ValueError("PVT link raw patch path/bytes mismatch: " + name)
    for key in ("version", "platform", "core_commit", "core_gitlink", "platform_commit", "platform_version", "overrides"):
        if old_meta.get(key) != new_meta.get(key):
            raise ValueError("unreviewed source/platform patch identity change")
    expected_additions = [{"path": p["patch"], "sha256": p["sha256"]} for p in additions]
    if new_meta["additions"]["patches"] != old_meta["additions"]["patches"] + expected_additions:
        raise ValueError("previous additions changed")
    return additions


def validate_surface(entries, pin):
    if (len(entries) != 1 or [e[0] for e in entries] != pin["targets"]
            or entries[0][1] != "modify"):
        raise ValueError("PVT link addition differs from reviewed source surface")


def selected_stacks(repo, core, tooling, src, manifest):
    selected, raw = patch_selection.select(repo, "linux", src=src, core=core)
    additions = validate_additions(selected, raw, manifest, repo)
    identity, stack, _ = arp._load(repo, core, tooling, "linux", src=src)
    if len(stack) != manifest["migration"]["new_patch_count"]:
        raise ValueError("incomplete current PVT link stack")
    for item, pin in zip(stack[OLD_COUNT:], additions, strict=True):
        validate_surface(item[2], pin)
    return selected, identity, stack


def snapshot_outputs(out):
    if out.is_symlink() or not out.is_dir():
        raise ValueError("missing or linked donor output")
    result = {}
    for directory, dirs, files in os.walk(out, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            info = path.lstat()
            relative = path.relative_to(out).as_posix()
            if stat.S_ISREG(info.st_mode):
                content = tools.file_hash(path)
            elif stat.S_ISLNK(info.st_mode):
                content = os.readlink(path)
            elif stat.S_ISDIR(info.st_mode):
                content = None
            else:
                raise ValueError("special donor output: " + relative)
            after = path.lstat()
            if (info.st_size, info.st_mtime_ns, info.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError("output changed while hashing: " + relative)
            result[relative] = {"mode": info.st_mode, "size": info.st_size if not stat.S_ISDIR(info.st_mode) else 0,
                                "mtime_ns": info.st_mtime_ns, "inode": info.st_ino, "content": content}
    return result


def output_identity(outputs):
    portable = {n: {k: v for k, v in item.items() if k not in ("mtime_ns", "inode")} for n, item in outputs.items()}
    return {"entries": len(portable), "sha256": source.digest(json_bytes(portable))}


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


def private_migration(src, stack, temporary_root, manifest):
    migration = review(manifest)
    targets = migration["sources"]
    if len(stack) != migration["new_patch_count"]:
        raise ValueError("private replay requires complete reviewed PVT link stack")
    for item, pin in zip(stack[OLD_COUNT:], migration["additions"], strict=True):
        validate_surface(item[2], pin)
    names = {entry[0] for _, _, entries in stack for entry in entries}
    if any(name.startswith("out/") for name in names):
        raise ValueError("patch stack cannot touch generated output")
    for name in targets:
        source.regular(src, name)
    before = arp._snapshot(src, names)
    with tempfile.TemporaryDirectory(prefix="storage-stack-", dir=temporary_root) as temporary:
        root = Path(temporary)
        stage = root / "src"
        stage.mkdir()
        for name, (raw, _) in before.items():
            if raw is not None:
                p = stage / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw.replace(b"\r\n", b"\n"))
        old = stack[:OLD_COUNT]
        for reverse, sequence in ((True, reversed(old)), (False, old)):
            for item in sequence:
                apply_one(stage, item, reverse, root)
        for name, (raw, _) in before.items():
            p = stage / name
            if (p.read_bytes() if p.exists() else None) != (raw.replace(b"\r\n", b"\n") if raw is not None else None):
                raise ValueError("old232 private roundtrip changed source: " + name)
        for item in stack[OLD_COUNT:]:
            apply_one(stage, item, False, root)
        expected = {name: (stage / name).read_bytes() if (stage / name).exists() else None for name in names}
        for name, raw in expected.items():
            old_raw = before[name][0]
            if name not in targets and raw != (old_raw.replace(b"\r\n", b"\n") if old_raw is not None else None):
                raise ValueError("storage migration changed an unrelated source")
        for reverse, sequence in ((True, reversed(stack)), (False, stack)):
            for item in sequence:
                apply_one(stage, item, reverse, root)
        for name, raw in expected.items():
            p = stage / name
            if (p.read_bytes() if p.exists() else None) != raw:
                raise ValueError("new reviewed stack private roundtrip changed source: " + name)
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
    receipt = json.loads(source.regular(work / "src", source.RECEIPT).read_bytes())
    pin = load(repo, receipt["target_arch"])["ninja"]
    chosen = shutil.which("ninja")
    if not chosen or restore_ninja.host_identity() != tuple(pin["host"]):
        raise ValueError("compiled storage recovery requires native donor-compatible Ninja")
    path = Path(chosen).resolve(strict=True)
    if ("x64" not in restore_ninja.binary_architectures(path, "linux")
            or tools.file_hash(path) != pin["sha256"]
            or restore_ninja.probe_version(path, work) != pin["version"]):
        raise ValueError("Ninja differs from compiled checkpoint; preserve its logs")
    return path


def atomic_bytes(path, raw, *, mode=None):
    if path.is_symlink():
        raise ValueError("linked publication path")
    descriptor, name = tempfile.mkstemp(prefix=".storage-publish-", dir=path.parent)
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


def publish_sources(src, before, revised, targets):
    """The source stays unready until the replacement and all receipts are committed."""
    if not (src / PENDING).is_file() or not (src / ".chromix-layer-in-progress").is_file():
        raise ValueError("source-only publication requires both transaction blockers")
    if set(targets) - before.keys() or set(targets) - revised.keys():
        raise ValueError("incomplete source-only publication")
    for name in targets:
        raw, info = before[name]
        path = source.regular(src, name)
        if path.read_bytes() != raw or path.stat().st_mtime_ns != info.st_mtime_ns:
            raise ValueError("storage source changed before atomic publication")
    for name in targets:
        atomic_bytes(src / name, revised[name], mode=stat.S_IMODE(before[name][1].st_mode))


def verify_storage_chain(work, manifest, repo=source.ROOT):
    proof = json.loads(source.regular(work / "src", ".chromix-linux157-storage-migration.json").read_bytes())
    if proof != manifest["storage_proof"]:
        raise ValueError("compiled source232 Storage proof changed")
    required = {"src/.chromix-linux157-storage-migration.json",
                "linux157-storage-origin/" + source.RECEIPT,
                "linux157-storage-origin/source-final.json",
                "linux157-storage-origin/output-hashes.json"}
    if not required.issubset(manifest["storage_chain"]):
        raise ValueError("incomplete compiled Storage chain manifest")
    prior.verify_files(work, manifest["storage_chain"])
    old = json.loads(source.regular(work / "linux157-storage-origin", source.RECEIPT).read_bytes())
    current = manifest["source_receipt"]
    if (proof.get("old_patch_count") != 229 or proof.get("new_patch_count") != 232
            or proof.get("origin_recipe") != old["recipe"]
            or proof.get("current_recipe") != current["recipe"]
            or old["chromix_selection"]["patches"] != current["chromix_selection"]["patches"][:229]
            or proof.get("patches") != current["chromix_selection"]["patches"][229:]
            or proof.get("runtime_verified") is not False):
        raise ValueError("compiled source232 nested Storage transaction identity mismatch")
    prior.verify_files(work / "linux157-storage-origin", proof["originals"])
    prior.verify_files(work / "src", proof["after"])
    origin_final = json.loads(source.regular(work / "linux157-storage-origin", "source-final.json").read_bytes())
    if (origin_final.get("status") != "verified" or origin_final.get("patch_count") != 229
            or origin_final["identity"]["series"]["patches"] != old["chromix_selection"]["patches"]
            or manifest["source_final"].get("patch_count") != 232):
        raise ValueError("compiled Storage nested source-final chain mismatch")
    outputs = json.loads(source.regular(work / "linux157-storage-origin", "output-hashes.json").read_bytes())
    if output_identity(outputs) != proof["output_tree"]:
        raise ValueError("compiled Storage origin output proof changed")
    for field in ("prepared_outputs", "substituted_outputs"):
        expected = dict(old[field])
        expected.update(proof["after"])
        if expected != current[field]:
            raise ValueError("compiled source232 differs from exact nested Storage delta")


def admit(work, arch, metadata, repo=source.ROOT):
    m = load(repo, arch)
    if metadata != expected(arch, repo):
        raise ValueError("storage metadata tuple mismatch")
    src = work / "src"
    for name in (PENDING, MARKER, source.IN_PROGRESS, ".chromix-layer-in-progress", ".chromix-domain-substitution-in-progress",
                 ".chromix-linux157-libclang-in-progress", ".chromix-upstream-restored.json"):
        if (src / name).exists() or (src / name).is_symlink():
            raise ValueError("partial or already migrated storage tree")
    migration = review(m)
    targets = migration["sources"]
    downloaded = json.loads(source.regular(work, "linux157-pvt-link-download.json").read_bytes())
    if (downloaded.get("status") != "success" or downloaded.get("publication") != "published"
            or downloaded.get("run_id") != m["run_id"] or downloaded.get("head_sha") != m["head_sha"]
            or downloaded.get("repository") != m["repository"] or len(downloaded.get("artifacts", [])) != 1
            or any(downloaded["artifacts"][0].get(k) != v for k, v in metadata["artifacts"][0].items())):
        raise ValueError("compiled artifact download verification missing")
    prior.verify_files(work, m["files"])
    verify_storage_chain(work, m, repo)
    old = json.loads(source.regular(src, source.RECEIPT).read_bytes())
    if (old != m["source_receipt"] or old.get("stage") != "chromix-applied"
            or old.get("target_arch") != arch or old.get("upstream_cache") is not False
            or patch_selection.source_version(src) != source.VERSION):
        raise ValueError("compiled source preparation identity changed")
    prior.verify_files(src, old["substituted_outputs"])
    prior.verify_files(src, {name: pin["before"] for name, pin in targets.items()})
    final = json.loads(source.regular(work, "fingerprint-diagnostics/source-final.json").read_bytes())
    if final != m["source_final"] or final.get("patch_count") != OLD_COUNT:
        raise ValueError("compiled source-final does not match old232")
    prior.verify_files(src, final["outputs"])
    try:
        from .import_upstream_cache import validate_gn_target
        from .restore_upstream_cache import gn_assignments_last_wins
    except ImportError:
        from import_upstream_cache import validate_gn_target
        from restore_upstream_cache import gn_assignments_last_wins
    gn = gn_assignments_last_wins(source.regular(src, "out/Chromix/args.gn"))
    validate_gn_target(gn, "linux", arch)
    if gn != m["gn_args"]:
        raise ValueError("compiled donor GN/PGO profile changed")
    tool = json.loads(source.regular(src, ".chromix-linux157-toolchain.json").read_bytes())
    if tool.get("recipe") != old["recipe"] or tool.get("target_arch") != arch:
        raise ValueError("compiled toolchain receipt mismatch")
    for name, digest in tool["binaries"].items():
        prior.checked_binary(src, name, digest)
    libclang.verify_package(src / libclang.PACKAGE, libclang.load(repo))
    core, tooling = work / "tooling/ungoogled-chromium", work / "tooling/ungoogled-chromium-portablelinux"
    source.get_platform(core, tooling, repo)
    new_recipe = source.recipe(repo)
    recipe_delta(old["recipe"], new_recipe, repo, arch)
    selected, identity, stack = selected_stacks(repo, core, tooling, src, m)
    out_before = snapshot_outputs(src / "out")
    if output_identity(out_before) != m["output_tree"]:
        raise ValueError("compiled donor per-output identity mismatch")
    link_cache = {name: value for name, value in out_before.items()
                  if name.startswith("Chromix/thinlto-cache/")}
    if output_identity(link_cache) != m["link_cache"]["identity"]:
        raise ValueError("compiled ThinLTO cache identity mismatch")
    before, revised = private_migration(src, stack, work, m)
    changed = {name for name in revised if revised[name] != (before[name][0].replace(b"\r\n", b"\n") if before[name][0] is not None else None)}
    if changed != set(targets):
        raise ValueError("storage additions must change exactly the approved sources")
    for name, pin in targets.items():
        if source.digest(before[name][0]) != pin["before"] or source.digest(revised[name]) != pin["after"]:
            raise ValueError("storage source preimage/postimage mismatch: " + name)
    if snapshot_outputs(src / "out") != out_before:
        raise ValueError("GN/PGO/Ninja output changed during migration")
    prior.verify_files(work, m["files"])
    if source.recipe(repo) != new_recipe or patch_selection.select(repo, "linux", src=src, core=core)[0] != selected:
        raise ValueError("recipe or patch review changed during private replay")
    backup = work / BACKUP
    if backup.exists() or backup.is_symlink():
        raise ValueError("existing storage origin backup")
    backup.mkdir()
    (backup / "output-hashes.json").write_bytes(json_bytes(out_before))
    originals = {source.RECEIPT: source.regular(src, source.RECEIPT).read_bytes(),
                 ".chromix-linux157-toolchain.json": source.regular(src, ".chromix-linux157-toolchain.json").read_bytes(),
                 ".chromix-source-ready": source.regular(src, ".chromix-source-ready").read_bytes(),
                 "source-final.json": source.regular(work, "fingerprint-diagnostics/source-final.json").read_bytes(),
                 **{"sources/" + name: before[name][0] for name in targets}}
    for name, raw in originals.items():
        (backup / name).parent.mkdir(parents=True, exist_ok=True)
        (backup / name).write_bytes(raw)
    proof = {"schema_version": 1, "phase": "exact-compiled-pvt-link", "arch": arch, "metadata": metadata,
             "origin_recipe": old["recipe"], "current_recipe": new_recipe, "old_patch_count": OLD_COUNT,
             "new_patch_count": migration["new_patch_count"], "patches": selected["patches"][OLD_COUNT:],
             "before": {name: pin["before"] for name, pin in targets.items()},
             "after": {name: pin["after"] for name, pin in targets.items()},
             "originals": {n: source.digest(b) for n, b in originals.items()},
             "output_tree": m["output_tree"], "runtime_verified": False}
    # Publication is guarded until source, receipts and source-final are committed together.
    with (src / PENDING).open("xb") as stream:
        stream.write(json_bytes(proof))
        stream.flush()
        os.fsync(stream.fileno())
    with (src / ".chromix-layer-in-progress").open("xb") as stream:
        stream.write(json_bytes(proof))
        stream.flush()
        os.fsync(stream.fileno())
    prior.verify_files(work, m["files"])
    prior.verify_files(src, old["substituted_outputs"])
    publish_sources(src, before, revised, targets)
    receipt = copy.deepcopy(old)
    receipt.update(recipe=new_recipe, chromix_selection=selected)
    for name in targets:
        receipt["prepared_outputs"][name] = source.digest(revised[name])
        receipt["substituted_outputs"][name] = source.digest(revised[name])
    tool["recipe"] = new_recipe
    new_final = {"schema_version": 1, "status": "verified", "method": "reverse-forward-in-scratch",
                 "qualification": "complete old232 and new reviewed stack private roundtrips; exactly the approved sources published",
                 "domain_substituted": True, "patch_count": migration["new_patch_count"], "identity": identity,
                 "outputs": {n: source.digest(b) if b is not None else None for n, b in revised.items()}}
    atomic_bytes(src / source.RECEIPT, json_bytes(receipt))
    atomic_bytes(src / ".chromix-linux157-toolchain.json", json_bytes(tool))
    atomic_bytes(work / "fingerprint-diagnostics/source-final.json", json_bytes(new_final))
    atomic_bytes(src / ".chromix-source-ready", f"linux157-fresh|{arch}|{new_recipe['sha256']}\n".encode())
    if snapshot_outputs(src / "out") != out_before:
        raise ValueError("compiled outputs/ThinLTO cache changed during publication")
    prior.verify_files(src, new_final["outputs"])
    if source.recipe(repo) != new_recipe or patch_selection.select(repo, "linux", src=src, core=core)[0] != selected:
        raise ValueError("review changed during storage publication")
    source.verify_receipt(src, repo)
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
        raise ValueError("incomplete storage migration")
    m = load(repo, arch)
    review(m)
    proof = json.loads(source.regular(src, MARKER).read_bytes())
    selected, raw = patch_selection.select(repo, "linux", src=src)
    validate_additions(selected, raw, m, repo)
    migration = review(m)
    targets = migration["sources"]
    if (arch != m["arch"] or proof.get("metadata") != expected(arch, repo)
            or proof.get("origin_recipe") != m["source_receipt"]["recipe"]
            or proof.get("current_recipe") != source.recipe(repo) or proof.get("phase") != "exact-compiled-pvt-link"
            or proof.get("old_patch_count") != OLD_COUNT
            or proof.get("new_patch_count") != migration["new_patch_count"] or proof.get("patches") != selected["patches"][OLD_COUNT:]
            or proof.get("runtime_verified") is not False):
        raise ValueError("storage migration identity mismatch")
    recipe_delta(proof["origin_recipe"], proof["current_recipe"], repo, arch)
    expected_originals = {source.RECEIPT: m["files"]["src/" + source.RECEIPT],
                          ".chromix-linux157-toolchain.json": m["files"]["src/.chromix-linux157-toolchain.json"],
                          ".chromix-source-ready": m["files"]["src/.chromix-source-ready"],
                          "source-final.json": m["files"]["fingerprint-diagnostics/source-final.json"],
                          **{"sources/" + name: pin["before"] for name, pin in targets.items()}}
    if proof.get("originals") != expected_originals:
        raise ValueError("storage origin backups do not match exact compiled donor")
    prior.verify_files(work / BACKUP, expected_originals)
    outputs = json.loads(source.regular(work / BACKUP, "output-hashes.json").read_bytes())
    if proof.get("output_tree") != m["output_tree"] or output_identity(outputs) != m["output_tree"]:
        raise ValueError("missing original per-output hash proof")
    chain = {name: digest for name, digest in m["files"].items()
             if name.startswith(("linux157-pre-gn-origin/", "linux157-postgn-origin/", "linux157-locale-origin/", "linux157-ua-origin/", "linux157-storage-origin/"))
             or name in ("src/.chromix-linux157-gn-migration.json", "src/.chromix-linux157-libclang-migration.json",
                         "src/.chromix-linux157-locale-migration.json", "src/.chromix-linux157-ua-migration.json",
                         "src/.chromix-linux157-storage-migration.json")}
    prior.verify_files(work, chain)
    verify_storage_chain(work, m, repo)
    package_receipt = json.loads(source.regular(src, ".chromix-linux157-libclang.json").read_bytes())
    package = libclang.load(repo)
    if (package_receipt.get("files") != package["files"]
            or package_receipt.get("package_sha256") != package["download"]["sha256"]
            or package_receipt.get("probe", {}).get("version") != package["clang_version"]
            or package_receipt.get("probe", {}).get("metagen") != "instance-types-and-layouts-passed"):
        raise ValueError("Storage continuation lost the verified libclang tool identity")
    expected_before = {name: pin["before"] for name, pin in targets.items()}
    expected_after = {name: pin["after"] for name, pin in targets.items()}
    if proof.get("before") != expected_before or proof.get("after") != expected_after:
        raise ValueError("storage source preimage/postimage identity mismatch")
    receipt = json.loads(source.regular(src, source.RECEIPT).read_bytes())
    if receipt.get("chromix_selection") != selected or receipt.get("recipe") != proof["current_recipe"]:
        raise ValueError("storage source receipt is not bound to the current reviewed stack")
    for field in ("prepared_outputs", "substituted_outputs"):
        outputs = dict(m["source_receipt"][field])
        outputs.update(expected_after)
        if receipt.get(field) != outputs:
            raise ValueError("storage receipt changed beyond the exact reviewed-source delta")
    prior.verify_files(src, expected_after)
    core = work / "tooling/ungoogled-chromium"
    tooling = work / "tooling/ungoogled-chromium-portablelinux"
    _, identity, stack = selected_stacks(repo, core, tooling, src, m)
    final = json.loads(source.regular(work, "fingerprint-diagnostics/source-final.json").read_bytes())
    names = {entry[0] for _, _, entries in stack for entry in entries}
    if (final.get("status") != "verified" or final.get("identity") != identity
            or final.get("patch_count") != migration["new_patch_count"]
            or final.get("domain_substituted") is not True or set(final.get("outputs", {})) != names):
        raise ValueError("Storage source-final identity changed")
    prior.verify_files(src, final["outputs"])
    prior.verify_files(work, {"src/out/Chromix/args.gn": m["files"]["src/out/Chromix/args.gn"]})
    libclang.verify_package(src / libclang.PACKAGE, libclang.load(repo))
    return proof


def restore(work, arch, repo=source.ROOT):
    if work.exists() or work.is_symlink():
        raise ValueError("compiled restore requires absent workdir")
    m = load(repo, arch)
    review(m)
    selected, patches = patch_selection.select(repo, "linux")
    validate_additions(selected, patches, m, repo)
    metadata = validate_metadata(snapshot.Client(m["repository"], os.environ.get("GH_TOKEN", "")), arch, repo)
    work.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="linux157-pvt-link-", dir=work.parent) as temp:
        root = Path(temp)
        (root / "metadata.json").write_bytes(json_bytes(metadata))
        subprocess.run([sys.executable, str(repo / "tools/download_posix_snapshot.py"),
                        "--manifest", str(root / "metadata.json"), "--destination", str(root / "volumes"),
                        "--report", str(root / "download.json")], check=True)
        subprocess.run([sys.executable, str(repo / "tools/restore_posix_snapshot.py"),
                        str(root / "volumes"), str(work)], check=True)
        (work / "linux157-pvt-link-download.json").write_bytes((root / "download.json").read_bytes())
    return admit(work, arch, metadata, repo)


def main(argv=None):
    raise ValueError("PVT link recovery requires the independently reviewed wrapper")


if __name__ == "__main__":
    raise SystemExit(main())
