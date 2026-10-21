#!/usr/bin/env python3
"""Strict 224-to-225 mac155 Intel controller migration and continuation receipts."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

try:
    from . import migrate_restored_snapshot as migration
    from . import validate_macos155_intel_controller_checkpoint as donor
except ImportError:
    import migrate_restored_snapshot as migration
    import validate_macos155_intel_controller_checkpoint as donor

arp = migration.arp
ancestor = donor.ancestor
require = donor.require
read = donor.read
sha = donor.sha
RECEIPT = "fingerprint-diagnostics/mac155-intel-controller-migration.json"
MANIFEST = "build/patch-additions-macos155.json"
PATCH = "patches/chromium155-macos/0225-managed-profile-controller-link.patch"
SOURCE = "chrome/browser/enterprise/BUILD.gn"
OLD_SELECTOR = "44a9a15a798ddb53a59c64cb2669e89a47b283a3a5e877e8543e3eca404531a5"
OLD_SELECTION = "240138342c298c629fdf94b216f2989c880b66a52dcb660beba0b50db32b8ac6"
REVIEWED_SELECTOR_SHA256 = "13e603e032b1854a52e905b6e77c4b6f89ad202f2710c1296087628fe0908396"
REVIEWED_MANIFEST_SHA256 = "94bb12f24f1c840f5a434bdc24971ab948be908944fd7c2fb51e6a39852af64f"
REVIEWED_PATCH_SHA256 = "350b333232d5865d9dff93a32b1e7b9c2e16a0c1fd31479809a0fd9106db700a"
REVIEWED_SOURCE_BEFORE_SHA256 = "7706d16e7d5a6f494f7ef2f3d7269fbc26aff03df989e562651f5689d120e71a"
REVIEWED_SOURCE_AFTER_SHA256 = "7e21eff01a1f3925b9b13514731ac15d61c1678a68cae573c35e6bc4ef9168c9"
TARGET_KEY = ("macos|x64|155.0.8059.39|ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a|"
              "b1238bb72b02cd36219991d9029c1c65183ea295|"
              "e1bf946113b645cd4463da31483f601437093c9b79140ae4e0dae880bb90c5d9")
TARGET_SOURCE_IDENTITY = "1834a6e72106473a079b71964e84fb3d37ebbbc1705bef1a23c67d5e80c8d916"
TARGET_SOURCE_OUTPUTS = "22768a1d0153ca510934d58ded12e8ac523a514a0e9ec5a841c1a63e21f074a7"
RECIPE_FILES = tuple(dict.fromkeys((*ancestor.RECIPE_FILES, MANIFEST, PATCH)))


def reviewed(repo):
    expected = {"tools/patch_selection.py": REVIEWED_SELECTOR_SHA256,
                MANIFEST: REVIEWED_MANIFEST_SHA256, PATCH: REVIEWED_PATCH_SHA256}
    require(all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in (
        *expected.values(), REVIEWED_SOURCE_BEFORE_SHA256, REVIEWED_SOURCE_AFTER_SHA256)),
        "controller migration is unarmed: integrate and pin the reviewed selector/manifest/patch/source hashes")
    for name, digest in expected.items():
        require(sha(read(repo, name)) == digest, f"reviewed controller input changed: {name}")
    require(arp.transform_patch(read(repo, PATCH), set(), [])[1] == [(SOURCE, "modify", None)],
            "controller patch must modify only the reviewed GN source-inclusion file")


def validate_inputs(previous, repo, platform, arch):
    require((platform, arch) == ("macos", "x64"), "controller migration requires native macOS Intel")
    reviewed(repo)
    require(migration._donor_git(previous, repo, "rev-parse", "HEAD").decode().strip() == donor.SHA
            and migration._donor_git(previous, repo, "rev-parse", "HEAD^{tree}").decode().strip() == donor.TREE,
            "exact latest stage8 producer checkout required")
    require(not migration._donor_git(previous, repo, "status", "--porcelain", "--untracked-files=all"),
            "producer checkout must be clean")
    trusted = Path(__file__).resolve().parents[1]
    for name in ancestor.RECIPE_FILES:
        old = read(previous, name)
        require(old == migration._donor_git(previous, repo, "cat-file", "blob", donor.SHA + ":" + name),
                f"producer recipe differs from pinned Git object: {name}")
        if name != "tools/patch_selection.py":
            require(old == read(repo, name), f"controller migration must preserve recipe: {name}")
        require(read(repo, name) == read(trusted, name), f"untrusted migration recipe: {name}")
    require(sha(read(previous, "tools/patch_selection.py")) == OLD_SELECTOR,
            "historical selector identity mismatch")
    old, old_patches = arp.patch_selection.select(previous, "macos")
    current, patches = arp.patch_selection.select(repo, "macos")
    require(ancestor.identity_hash(old) == OLD_SELECTION and len(old_patches) == 224
            and patches == [*old_patches, (PATCH, read(repo, PATCH))] and len(patches) == 225,
            "controller migration requires identical 224-patch prefix and one reviewed addition")
    expected = dict(old["selection"], additions={
        "schema_version": 1, "kind": "macos155-managed-profile-controller-link",
        "manifest": {"path": MANIFEST, "sha256": REVIEWED_MANIFEST_SHA256},
        "patches": [{"path": PATCH, "sha256": REVIEWED_PATCH_SHA256}],
    })
    require(current["selection"] == expected, "controller selection identity mismatch")
    require(migration.source_ready_key(previous, "macos", "x64") == ancestor.KEY,
            "latest stage8 preparation key mismatch")
    require(migration.source_ready_key(repo, "macos", "x64") == TARGET_KEY,
            "controller migration requires the exact reviewed 225-patch preparation key")


def out_inventory(src):
    root = src / "out/Default"
    require(root.is_dir() and not root.is_symlink() and not (src / "out").is_symlink(),
            "real incremental output directory required")
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode),
                "unsupported output cache entry")
        result[path.relative_to(src).as_posix()] = [
            info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            os.readlink(path) if path.is_symlink() else None,
        ]
    return result


def live_source(repo, work, output, *, old=False, trusted=None):
    require(not output.exists(), "new source verification report required")
    subprocess.run([sys.executable, str((trusted or repo) / "tools/verify_patch_stack.py"),
                    "--src", str(work / "src"), "--repo", str(repo),
                    "--core", str(work / "tooling/ungoogled-chromium"),
                    "--platform-tooling", str(work / "tooling/ungoogled-chromium-macos"),
                    "--platform", "macos", "--output", str(output)], check=True)
    result = json.loads(output.read_bytes())
    require(result.get("status") == "verified" and result.get("domain_substituted") is True
            and result.get("patch_count") == (224 if old else 225), "full source validation required")
    if old:
        require(ancestor.identity_hash(result.get("identity")) == ancestor.SOURCE_IDENTITY
                and ancestor.identity_hash(result.get("outputs")) == ancestor.SOURCE_OUTPUTS,
                "latest donor source identity/output hashes changed")
    else:
        require(ancestor.identity_hash(result.get("identity")) == TARGET_SOURCE_IDENTITY
                and ancestor.identity_hash(result.get("outputs")) == TARGET_SOURCE_OUTPUTS,
                "reviewed Intel controller source identity/output hashes required")
    return result


class Policy:
    validate_inputs = staticmethod(validate_inputs)

    def __init__(self, work):
        self.work = work
        self.out = out_inventory(work / "src")
        self.receipts = {name: sha(read(work, name)) for name in donor.PINNED_FILES
                         if name != "src/.chromix-restored-patches.json"}

    def validate_staged(self, src, stage, changed):
        require(changed == [SOURCE], "controller migration net change must be exactly one GN source file")
        require(sha(read(src, SOURCE)) == REVIEWED_SOURCE_BEFORE_SHA256
                and sha(read(stage, SOURCE)) == REVIEWED_SOURCE_AFTER_SHA256,
                "reviewed GN preimage/postimage mismatch")
        self.validate_preserved(src)

    def validate_preserved(self, src):
        require(all(sha(read(self.work, name)) == digest for name, digest in self.receipts.items()),
                "migration changed protected donor/preparation receipts")
        require(out_inventory(src) == self.out, "migration must preserve every output/cache entry and timestamp")
        require({name: sha(read(src, name)) for name in donor.STATE} == donor.STATE,
                "migration changed latest stage8 Ninja state")
        require(sha(read(src, "out/Default/args.gn")) == ancestor.PINNED_FILES["src/out/Default/args.gn"],
                "migration changed GN arguments")


def migrate(work, previous, repo, *, logs, patch_bin=None):
    work, previous, repo = (migration.restore.local_path(p) for p in (work, previous, repo))
    require(not (work / RECEIPT).exists(), "controller receipt already exists")
    validate_inputs(previous, repo, "macos", "x64")
    evidence = donor.downloaded(logs)
    donor.donor_state(work)
    migration._ready(work / "src", ancestor.KEY)
    old_report = work / "fingerprint-diagnostics/mac155-intel-controller-donor-source.json"
    # Only trusted current verification code executes; the donor selector is read as data.
    require(read(previous, "tools/verify_patch_stack.py") == read(repo, "tools/verify_patch_stack.py"),
            "source verifier changed")
    old = live_source(previous, work, old_report, old=True, trusted=repo)
    policy = Policy(work)
    result = migration.migrate(work, previous, repo, "macos", "x64", patch_bin=patch_bin, policy=policy)
    policy.validate_preserved(work / "src")
    current = live_source(repo, work, work / "fingerprint-diagnostics/mac155-intel-controller-source-admission.json")
    require(all(current["outputs"].get(name) == digest for name, digest in old["outputs"].items()),
            "controller migration changed an existing patch output")
    receipt = dict(result, operation="mac155-intel-stage8-controller-migration", repository=donor.REPOSITORY,
                   run_id=donor.RUN, attempt=donor.ATTEMPT, stage=donor.STAGE, job_id=donor.JOB,
                   head_sha=donor.SHA, tree=donor.TREE, artifacts=donor.ARTIFACTS, download_evidence=evidence,
                   source_identity=ancestor.identity_hash(current["identity"]),
                   source_outputs=ancestor.identity_hash(current["outputs"]),
                   recipes={name: sha(read(repo, name)) for name in RECIPE_FILES},
                   state=donor.STATE, preserved_out_inventory=ancestor.identity_hash(policy.out),
                   patch_receipt=sha(read(work / "src", arp.MARKER)))
    arp._atomic_write(work / RECEIPT, arp._json(receipt))
    return receipt


def continuation(repo, work, stage, phase):
    require(1 <= stage <= 8 and phase in ("before", "after"), "controller continuation stage/phase invalid")
    require(os.environ.get("GITHUB_WORKFLOW") == donor.WORKFLOW
            and os.environ.get("CHROMIX_BUILD_PROFILE") == "fast"
            and os.environ.get("CHROMIX_USE_UPSTREAM_CACHE") == "1",
            "controller continuation requires its native fast/cache workflow")
    reviewed(repo)
    require(migration.source_ready_key(repo, "macos", "x64") == TARGET_KEY,
            "controller continuation preparation key changed")
    receipt = json.loads(read(work, RECEIPT))
    expected = dict(status="migrated", operation="mac155-intel-stage8-controller-migration",
                    repository=donor.REPOSITORY, run_id=donor.RUN, attempt=1, stage=8, job_id=donor.JOB,
                    head_sha=donor.SHA, tree=donor.TREE, artifacts=donor.ARTIFACTS,
                    old_patch_count=224, patch_count=225, changed_files=[SOURCE], previous_key=ancestor.KEY,
                    key=migration.source_ready_key(repo, "macos", "x64"))
    require(all(receipt.get(k) == v for k, v in expected.items()), "controller migration provenance mismatch")
    require(receipt.get("recipes") == {name: sha(read(repo, name)) for name in RECIPE_FILES},
            "admitted controller recipes changed")
    src = work / "src"
    for name in (migration.TRANSACTION, arp.IN_PROGRESS):
        require(not (src / name).exists(), "interrupted controller migration")
    migration._ready(src, expected["key"])
    require(arp.patch_selection.source_version(src) == ancestor.VERSION, "source version changed")
    require(sha(read(src, arp.MARKER)) == receipt.get("patch_receipt"), "migrated patch receipt changed")
    require(sha(read(src, SOURCE)) == REVIEWED_SOURCE_AFTER_SHA256, "controller GN source changed")
    for name in ("src/out/Default/args.gn", "src/.chromix-upstream-restored.json"):
        require(sha(read(work, name)) == ancestor.PINNED_FILES[name], f"controller source/GN changed: {name}")
    for name in (ancestor.RECEIPT, donor.LATEST_RECEIPT,
                 donor.LATEST_SOURCE):
        require(sha(read(work, name)) == donor.PINNED_FILES[name], "historical donor provenance changed")
    ancestor.preparation(work)
    migration.restore.verify_restored(work, "macos", "x64", repo=repo)
    state = {}
    for name in donor.STATE:
        data = read(src, name)
        require(bool(data), f"missing incremental controller state: {name}")
        state[name] = sha(data)
    if stage == 1 and phase == "before":
        require(state == donor.STATE and ancestor.identity_hash(out_inventory(src)) == receipt.get("preserved_out_inventory"),
                "latest compiled outputs changed before first continuation")
    output = work / "fingerprint-diagnostics" / (
        f"mac155-intel-controller-s{stage}-{phase}-attempt-{os.environ.get('GITHUB_RUN_ATTEMPT', 'local')}.json")
    source = live_source(repo, work, output.with_name(output.stem + "-source.json"))
    require(ancestor.identity_hash(source["identity"]) == receipt.get("source_identity")
            and ancestor.identity_hash(source["outputs"]) == receipt.get("source_outputs"),
            "migrated live source identity/output hashes changed")
    result = dict(expected, phase=phase, continuation_stage=stage, logical_stage=15 + stage,
                  continuation_run=os.environ.get("GITHUB_RUN_ID"),
                  continuation_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"), state=state)
    arp._atomic_write(output, arp._json(result))
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
        print(f"mac155 controller continuation rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
