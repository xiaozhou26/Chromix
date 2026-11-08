#!/usr/bin/env python3
"""Migrate only the reviewed TS DOM contract in exact Linux156 failed snapshots."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apply_restored_patches as arp
import migrate_restored_snapshot as shared
import restore_ninja
import restored_reuse_evidence as evidence
import restore_upstream_cache as restore
import validate_linux156_ts_checkpoint as donor
import verify_patch_stack

FIX_COMMIT = "d3e6b8868b688f6ecbc75d832acb71ba6701bb6d"
PATCH_COUNT = 226
KEY_HASH = "9a544296e3603f829b1023b3c0ca0f32926568d4f29d3237882d0d5075e4ddd6"
DOM = "third_party/devtools-frontend/src/node_modules/typescript/lib/lib.dom.d.ts"
VALIDATOR = "tools/typescript/validate_tsconfig.py"
SURFACE = {
    DOM: ("8f94946b666574f23ae405f192dce40df552fc49bc048fdfdaeb44ff6203ac41",
          "973d88f96c6e775d613bddb9848043b1de35f4ed4f7129d779feb1cd4e3596f8"),
    VALIDATOR: ("f4b280e7b1776995645e9db53d331ba06cd430201dbdd717e7b64842a73c1032",
                "ea4234c9c286bebcb4adc5df2cf4ade0e3c132b9e1433ef18abb428a824565e6"),
}
TOOLS = {
    "tools/linux_restored_generators.py": (
        "424805c8e304da7d5867e2954048b41b6a8ca0f87d3a18790d1ff53aa93c0041",
        "e14ba18063bc4650d8a1ba818889ddcd23fdf6e0f398aa823b9fed868b5f07ea"),
    "tools/prepare_restored_build.py": (
        "7885aa7b0edaaae7019ef1b6938bb37624e672530bfd0787d411a39ad66ed42a",
        "f5e4121b12ba87d5b67a11c0eddcbad216416d3ba7cc19613eb1cfcd3ad8ba11"),
}
PRESERVED = (
    *shared.PIN_FILES, *shared.SCRIPT_FILES, "patches/series", "build/patch-selection-156.json",
    "build/args.gn", "build/build.sh", "build/posix/prepare-restored-tools.sh",
    "build/posix/upstream-cache.sh", "tools/install_linux_generators.py",
    "tools/restore_upstream_cache.py", "tools/restore_ninja.py", "tools/verify_patch_stack.py",
    "tools/upstream_script_identity.py", "tools/merge_gn_args.py", "tools/bootstrap_gn.py",
    "tools/import_upstream_cache.py", "tools/fetch_upstream_cache.py", "tools/upstream_object_cache.py",
    "tools/restored_reuse_evidence.py",
)
PACKAGE = "third_party/devtools-frontend/src/node_modules/typescript"
PACKAGE_HASHES = ("2dda99b559c5bbc04a1e5ec68440bf9afc14866593f4d9ca0de6c00556d2fb91",
                  "4694d67f47214ca388c2e3287eb33b4531e2e085b6fdea8e43670961366edc44")
PREPARED = ".chromix-restored-build-prepared.json"
PREPARED_HASHES = {"x64": "0236fb54cfb5afd88023fab4d52c24258bbda924c422f0ca45259b76d8c751c6",
                   "arm64": "2d39d50e062dd3c7b689119b5678fce906d654b8ad823d7b81622ce49034b39a"}
MARKER = ".chromix-linux156-ts-migration.json"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def regular(root: Path, name: str) -> Path:
    path = restore.safe_path(root, Path(name))
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe migration input: " + name)
    return path


def checked(root: Path, name: str, digest: str) -> bytes:
    raw = regular(root, name).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == digest, "migration SHA-256 mismatch: " + name)
    return raw


def authenticate_inputs(previous: Path, repo: Path):
    require(shared._donor_git(previous, repo, "rev-parse", "HEAD").decode().strip() == donor.HEAD,
            "migration requires exact Linux156 donor HEAD")
    for name in dict.fromkeys((*PRESERVED, *TOOLS)):
        original = arp._read(previous, name)
        require(original == shared._donor_git(previous, repo, "cat-file", "blob", donor.HEAD + ":" + name),
                "donor checkout bytes changed: " + name)
        current = arp._read(repo, name)
        if name in TOOLS:
            require(tuple(hashlib.sha256(raw).hexdigest() for raw in (original, current)) == TOOLS[name],
                    "unreviewed Linux156 TS tool transition: " + name)
        else:
            require(original == current, "migration must preserve unrelated input: " + name)
    old, patches = arp.patch_selection.select(previous, "linux")
    current, current_patches = arp.patch_selection.select(repo, "linux")
    require(len(patches) == PATCH_COUNT and (old, patches) == (current, current_patches),
            "migration requires the identical Linux156 226-patch stack")
    for name, raw in patches:
        require(raw == shared._donor_git(previous, repo, "cat-file", "blob", donor.HEAD + ":" + name),
                "historical patch bytes changed: " + name)
    require(arp.patch_selection.preparation_key(repo, "linux", "posix") == KEY_HASH,
            "migration must preserve original source-ready key")
    spec = importlib.util.spec_from_file_location("chromix_reviewed_linux156_generators", repo / "tools/linux_restored_generators.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def download_identity(origin: Path, report: Path, arch: str):
    manifest = json.loads(origin.read_bytes())
    donor.check_manifest(manifest, arch)
    downloaded = json.loads(report.read_bytes())
    require(downloaded.get("status") == "success" and downloaded.get("publication") == "published"
            and downloaded.get("phase") == "complete", "latest donor download was not completely verified/published")
    for key in ("repository", "head_sha", "run_id"):
        require(downloaded.get(key) == manifest[key], "download origin mismatch: " + key)
    artifacts = downloaded.get("artifacts", [])
    require(len(artifacts) == 1 and all(artifacts[0].get(k) == v for k, v in manifest["artifacts"][0].items()),
            "download artifact metadata differs from exact donor")
    attempts = artifacts[0].get("attempts", [])
    require(attempts and attempts[-1].get("status") == "success"
            and attempts[-1].get("size_in_bytes") == donor.DONORS[arch]["size"]
            and "sha256:" + attempts[-1].get("sha256", "") == donor.DONORS[arch]["digest"],
            "download artifact has no complete matching digest evidence")
    volumes = downloaded.get("volumes", [])
    require(len(volumes) == 1 and volumes[0].get("name") == "tree.tar.zst.001"
            and volumes[0].get("artifact_id") == donor.DONORS[arch]["artifact_id"],
            "unexpected latest donor volume inventory")
    for item in volumes:
        require(type(item.get("size_in_bytes")) is int and item["size_in_bytes"] > 0
                and isinstance(item.get("sha256"), str) and len(item["sha256"]) == 64
                and all(char in "0123456789abcdef" for char in item["sha256"]),
                "download volume lacks verified size/digest")
    require(downloaded.get("total_size_in_bytes") == sum(item["size_in_bytes"] for item in volumes),
            "download volume total size changed")
    return manifest


def walk_error(error):
    raise error


def output_inventory(out: Path):
    result = {}
    for directory, dirs, files in os.walk(out, followlinks=False, onerror=walk_error):
        for name in sorted([*dirs, *files]):
            path = Path(directory) / name
            info = path.lstat()
            result[path.relative_to(out).as_posix()] = (
                info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                info.st_ino, info.st_nlink, os.readlink(path) if path.is_symlink() else None)
    return result


def package_digest(src: Path, replacements=None):
    replacements = replacements or {}
    root = restore.safe_path(src, Path(PACKAGE))
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs.sort()
        for name in dirs:
            restore.safe_path(src, (Path(directory) / name).relative_to(src))
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(src).as_posix()
            payload = replacements.get(relative)
            if payload is None:
                payload = regular(src, relative).read_bytes()
            digest.update(json.dumps([path.relative_to(root).as_posix(), hashlib.sha256(payload).hexdigest()]).encode())
    return digest.hexdigest()


def source_plan(module, src: Path):
    identities, changes = module.source_plan(src, pins=module.PINS156)
    require({change[0] for change in changes} == set(SURFACE) and len(changes) == len(SURFACE),
            "Linux156 migration refuses broader source replay")
    for name, before, after, _ in changes:
        require(tuple(hashlib.sha256(raw).hexdigest() for raw in (before, after)) == SURFACE[name],
                "unreviewed source transition: " + name)
    require(package_digest(src) == PACKAGE_HASHES[0], "unexpected donor TypeScript package/inventory")
    require(package_digest(src, {name: after for name, _, after, _ in changes}) == PACKAGE_HASHES[1],
            "unexpected repaired TypeScript package/inventory")
    return identities, changes


def prepared_transition(src: Path, arch: str, module, identities):
    raw = checked(src, PREPARED, PREPARED_HASHES[arch])
    marker = json.loads(raw)
    require(marker.get("arch") == arch and marker.get("platform") == "linux" and marker.get("ready_for_gn") is True,
            "invalid donor preparation marker")
    result = copy.deepcopy(marker)
    typescript = result["generator_fingerprint"]["typescript"]
    require(typescript["package_sha256"] == PACKAGE_HASHES[0], "preparation package identity mismatch")
    records = typescript["esbuild"]["sources"]
    require(set(records) == set(identities) - {DOM}, "unexpected donor generator inventory")
    for name, identity in identities.items():
        if name != DOM:
            expected = dict(identity)
            if name == VALIDATOR:
                expected["repaired_sha256"] = SURFACE[VALIDATOR][0]
            require(records[name] == expected, "donor generator source identity mismatch: " + name)
    typescript["package_sha256"] = PACKAGE_HASHES[1]
    for name in SURFACE:
        profile = module.generator_profile(module.PINS156)
        record = profile["root_sources"][name] if name == VALIDATOR else profile["sources"][name.removeprefix(module.DEVTOOLS + "/")]
        records[name] = {"sha256": record[1], "original_sha256": record[0], "repaired_sha256": record[1]}
    # Only the reviewed generator identity advances; compiler/GN/environment state stays original.
    return raw, (json.dumps(result, sort_keys=True) + "\n").encode()


def verify_source(work: Path, repo: Path):
    report = verify_patch_stack.verify(work / "src", repo, core=work / "tooling/ungoogled-chromium",
                                      tooling=work / "tooling/ungoogled-chromium-portablelinux", platform="linux")
    require(report["status"] == "verified" and report["patch_count"] == PATCH_COUNT,
            "complete Linux156 reverse/forward source verification failed")
    return report


def migrate(workdir: Path, previous_repo: Path, repo: Path, arch: str, origin: Path, download_report: Path):
    work, previous, repo = (restore.local_path(path) for path in (workdir, previous_repo, repo))
    src = restore.local_path(work / "src")
    require(all(path.is_dir() for path in (work, previous, repo, src)), "migration directories are missing")
    require(not any(root.is_relative_to(src) for root in (previous, repo)), "repositories must remain outside source")
    manifest = download_identity(origin, download_report, arch)
    for name in (shared.TRANSACTION, arp.IN_PROGRESS, MARKER, ".chromix-restored-build-inspection.json"):
        require(not (src / name).exists() and not (src / name).is_symlink(), "interrupted or already migrated tree: " + name)
    temp_root = Path(tempfile.gettempdir()).resolve()
    require(not any(temp_root.is_relative_to(root) for root in (work, previous, repo)),
            "migration scratch must remain outside source/repositories")
    module = authenticate_inputs(previous, repo)
    receipt = restore.verify_restored(work, "linux", arch, repo=repo)
    require(receipt == restore.verify_restored(work, "linux", arch, repo=previous), "upstream receipt identity changed")
    pinned = donor.DONORS[arch]
    checked(src, restore.MARKER, pinned["receipt_sha256"])
    checked(src, arp.MARKER, donor.PATCH_RECEIPT_SHA256)
    checked(src, "out/Default/args.gn", pinned["args_sha256"])
    ninja_raw = checked(work, restore_ninja.REPORT, pinned["ninja_sha256"])
    ninja = json.loads(ninja_raw)
    require(ninja["selected"]["version"] == "1.12.1" and ninja["log"]["format"] == 6
            and ninja["arch"] == arch and ninja["host"] == ["linux", "x64"], "donor Ninja identity mismatch")
    with regular(src, "out/Default/.ninja_log").open("rb") as stream:
        require(stream.readline(129) == b"# ninja log v6\n", "donor Ninja log format changed")
    _, log, _, _ = evidence.read_log(src / "out/Default", set())
    require((log["sha256"], log["size_bytes"]) == (pinned["log_sha256"], pinned["log_size"]),
            "latest failed donor Ninja log digest/size changed")
    with regular(src, "out/Default/.ninja_deps").open("rb") as stream:
        require(stream.read(16) == b"# ninjadeps\n\x04\x00\x00\x00", "donor Ninja deps format changed")
    key = shared.source_ready_key(repo, "linux", arch)
    shared._ready(src, key)
    shared._verify_tooling(work, repo, (work, previous, repo), "linux")
    source = verify_source(work, repo)
    identity, patches, lite = arp._load(repo, work / "tooling/ungoogled-chromium",
                                        work / "tooling/ungoogled-chromium-portablelinux", "linux")
    shared._previous_completed(src, identity, shared._names(patches, lite))
    identities, changes = source_plan(module, src)
    prepared_before, prepared_after = prepared_transition(src, arch, module, identities)
    protected = {shared.READY, arp.MARKER, restore.MARKER, shared.SOURCE_VERSION,
                 "out/Default/args.gn", "out/Default/build.ninja", "out/Default/.ninja_log", "out/Default/.ninja_deps"}
    before = {name: (regular(src, name).read_bytes(), regular(src, name).stat())
              if (src / name).exists() else (None, None)
              for name in protected | set(source["outputs"]) | set(SURFACE) | {PREPARED}}
    out = src / "out/Default"
    outputs = output_inventory(out)
    report = {"schema_version": 1, "status": "in_progress", "origin": manifest, "fix_commit": FIX_COMMIT,
              "old_patch_count": PATCH_COUNT, "patch_count": PATCH_COUNT, "previous_key": key, "key": key,
              "source_verified": True, "changed_files": sorted(SURFACE), "tool_sha256": {k: v[1] for k, v in TOOLS.items()}}
    shared._claim(src / shared.TRANSACTION, arp._json(report))
    shared._claim(src / MARKER, arp._json(report))
    shared._unchanged(src, before)
    for name, old, new, metadata in changes:
        module._atomic_replace(src, name, old, new, expected_metadata=metadata)
    require(package_digest(src) == PACKAGE_HASHES[1], "repaired package changed during publication")
    require(module.source_plan(src, pins=module.PINS156)[1] == [], "repaired generator still requires source changes")
    # Source verifier rejects the blocker; verify an isolated patched-file copy instead.
    with tempfile.TemporaryDirectory(prefix="chromix-linux156-final-", dir=temp_root) as temporary:
        stage = Path(temporary) / "src"
        stage.mkdir()
        for name in set(source["outputs"]) | {shared.SOURCE_VERSION, ".chromix-domain-substituted"}:
            path = restore.safe_path(src, Path(name))
            if path.exists():
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        verified = verify_patch_stack.verify(stage, repo, core=work / "tooling/ungoogled-chromium",
                                             tooling=work / "tooling/ungoogled-chromium-portablelinux", platform="linux")
        require(verified == source, "final complete source verification changed")
    shared._unchanged(src, {name: value for name, value in before.items() if name not in SURFACE})
    require(output_inventory(out) == outputs, "migration modified Ninja/GN/output state")
    require(regular(work, restore_ninja.REPORT).read_bytes() == ninja_raw, "migration modified Ninja receipt")
    authenticate_inputs(previous, repo)
    shared._verify_tooling(work, repo, (work, previous, repo), "linux")
    require(regular(src, PREPARED).read_bytes() == prepared_before, "preparation marker changed concurrently")
    arp._atomic_write(src / PREPARED, prepared_after)
    report["status"] = "migrated"
    arp._atomic_write(src / MARKER, arp._json(report))
    (src / shared.TRANSACTION).unlink()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workdir", "previous-repo", "repo", "origin", "download-report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--arch", choices=tuple(donor.DONORS), required=True)
    args = parser.parse_args()
    try:
        report = migrate(args.workdir, args.previous_repo, args.repo, args.arch, args.origin, args.download_report)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, restore.LocalError, subprocess.SubprocessError) as error:
        print(f"Linux156 TS migration refused: {error}; restore the exact failed checkpoint again", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
