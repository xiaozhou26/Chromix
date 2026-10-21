#!/usr/bin/env python3
"""Private full-stack 225-to-226 PNG migration of exact compiled mac155 donors."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

try:
    from . import migrate_macos155_intel_controller_snapshot as controller
    from . import validate_macos155_png_checkpoint as donor
    from . import validate_macos155_arm64_stage8_checkpoint as arm
except ImportError:
    import migrate_macos155_intel_controller_snapshot as controller
    import validate_macos155_png_checkpoint as donor
    import validate_macos155_arm64_stage8_checkpoint as arm

migration, arp = controller.migration, controller.arp
require, read, sha = donor.require, donor.read, donor.sha
identity_hash = donor.common.identity_hash
RECEIPT = "fingerprint-diagnostics/mac155-png-migration.json"
MANIFEST = "build/patch-additions-macos155.json"
PATCH = "patches/chromium155-macos/0227-opaque-png-export-consistency.patch"
SOURCE = migration.LINUX155_PNG_SOURCE
BEFORE = "b544a82ed5ab6f125d68604312f214135200e0ce89c0c46f61dee9cc9d6a199d"
AFTER = "7d78a117324c383003bcda13cd61161cc336d8f498e246f621019ed2f7406945"
REVIEWED = {
    "tools/patch_selection.py": "6018fe31305999fb8b2ff5538e06db66b5428ae5850da4afa59ad50932a4dde5",
    MANIFEST: "3184e6676d3664217bf316912b4ac1fe7322f7d7a4bc3f55135de92f6ee90462",
    PATCH: "4c048a50930c3541a3bb9e4442ad7f18e94c04ebccf86c0cfbfc286b15a70751",
    donor.METADATA: donor.METADATA_SHA256,
}
RECIPE_FILES = tuple(dict.fromkeys((*controller.RECIPE_FILES, PATCH, donor.METADATA)))
ARCHIVED_RECEIPTS = {
    "src/.chromix-restored-patches.json": "fingerprint-diagnostics/mac155-png-original-patch-receipt.json",
    "fingerprint-diagnostics/source-final.json": "fingerprint-diagnostics/mac155-png-original-source-final.json",
}
MUTABLE_RECEIPTS = {"src/.chromix-restored-patches.json", "fingerprint-diagnostics/source-final.json",
                    "upstream-cache-preparation.json", "src/.chromix-restored-build-prepared.json"}


def reviewed(repo):
    for name, digest in REVIEWED.items():
        require(sha(read(repo, name)) == digest, "reviewed PNG input changed: " + name)
    require(arp.transform_patch(read(repo, PATCH), set(), [])[1] == [(SOURCE, "modify", None)],
            "PNG patch must modify only async canvas")


def validate_inputs(previous, repo, platform, arch):
    require(platform == "macos", "PNG migration requires native macOS")
    d = donor.checkpoint(arch)
    reviewed(repo)
    require(migration._donor_git(previous, repo, "rev-parse", "HEAD").decode().strip() == d["head_sha"]
            and migration._donor_git(previous, repo, "rev-parse", "HEAD^{tree}").decode().strip() == d["source_tree"],
            "exact compiled PNG producer checkout required")
    require(not migration._donor_git(previous, repo, "status", "--porcelain", "--untracked-files=all"),
            "PNG producer checkout must be clean")
    trusted = Path(__file__).resolve().parents[1]
    for name in controller.RECIPE_FILES:
        old = read(previous, name)
        require(old == migration._donor_git(previous, repo, "cat-file", "blob", d["head_sha"] + ":" + name),
                "PNG donor recipe differs from pinned Git object: " + name)
        if name not in ("tools/patch_selection.py", MANIFEST):
            require(old == read(repo, name), "PNG migration must preserve recipe: " + name)
        require(read(repo, name) == read(trusted, name), "untrusted PNG migration recipe: " + name)
    require(sha(read(previous, "tools/patch_selection.py")) == controller.REVIEWED_SELECTOR_SHA256
            and sha(read(previous, MANIFEST)) == controller.REVIEWED_MANIFEST_SHA256,
            "historical 225 selector/manifest mismatch")
    old, old_patches = arp.patch_selection.select(previous, "macos")
    current, patches = arp.patch_selection.select(repo, "macos")
    require(len(old_patches) == 225 and len(patches) == 226
            and patches == [*old_patches, (PATCH, read(repo, PATCH))],
            "PNG migration requires byte-identical 225-patch prefix")
    additions = json.loads(read(repo, MANIFEST))
    expected = dict(old["selection"], additions={
        "schema_version": 2, "kind": "macos155-compiled-png-recovery",
        "manifest": {"path": MANIFEST, "sha256": REVIEWED[MANIFEST]}, "patches": additions["patches"]})
    require(current["selection"] == expected, "PNG selection identity mismatch")
    require(migration.source_ready_key(previous, "macos", arch) == d["previous_key"]
            and migration.source_ready_key(repo, "macos", arch) == d["key"], "PNG preparation key mismatch")


def validate_source(result, d, *, old=False):
    require(result.get("status") == "verified" and result.get("domain_substituted") is True
            and result.get("patch_count") == (225 if old else 226), "full PNG source verification required")
    identity = copy.deepcopy(result.get("identity"))
    migration._tooling_identity_root(identity)
    for name in ("regex", "list"):
        identity[name]["path"] = d["identity_paths"][name]
    prefix = "" if old else "target_"
    require(identity_hash(identity) == d[prefix + "source_identity"]
            and identity_hash(result.get("outputs")) == d[prefix + "source_outputs"],
            "reviewed PNG source identity/output hashes required")


def live_source(repo, work, output, d, *, old=False, trusted=None):
    require(not output.exists(), "new PNG source verification report required")
    subprocess.run([sys.executable, str((trusted or repo) / "tools/verify_patch_stack.py"),
                    "--src", str(work / "src"), "--repo", str(repo),
                    "--core", str(work / "tooling/ungoogled-chromium"),
                    "--platform-tooling", str(work / "tooling/ungoogled-chromium-macos"),
                    "--platform", "macos", "--output", str(output)], check=True)
    result = json.loads(output.read_bytes())
    validate_source(result, d, old=old)
    return result


class Policy:
    validate_inputs = staticmethod(validate_inputs)

    def __init__(self, work, d):
        self.work, self.d = work, d
        self.out = controller.out_inventory(work / "src")
        self.receipts = {name: sha(read(work, name)) for name in d["pinned_files"]
                         if name != "src/.chromix-restored-patches.json"}
        self.gn = sha(read(work / "src", controller.SOURCE))

    def validate_staged(self, src, stage, changed):
        require(changed == [SOURCE], "PNG migration net change must be exactly async canvas")
        require(sha(read(src, SOURCE)) == BEFORE and sha(read(stage, SOURCE)) == AFTER,
                "reviewed PNG preimage/postimage mismatch")
        self.validate_preserved(src)

    def validate_preserved(self, src):
        require(all(sha(read(self.work, name)) == digest for name, digest in self.receipts.items()),
                "PNG migration changed protected source/preparation/historical receipts")
        require(controller.out_inventory(src) == self.out, "PNG migration changed output/cache entries or timestamps")
        require({name: sha(read(src, name)) for name in self.d["state"]} == self.d["state"],
                "PNG migration changed latest compiled Ninja state")
        require(sha(read(src, controller.SOURCE)) == self.gn == controller.REVIEWED_SOURCE_AFTER_SHA256,
                "PNG migration changed controller GN source")


def migrate(work, previous, repo, arch, *, logs, patch_bin=None):
    work, previous, repo = (migration.restore.local_path(p) for p in (work, previous, repo))
    d = donor.checkpoint(arch)
    require(not (work / RECEIPT).exists(), "PNG migration receipt already exists")
    validate_inputs(previous, repo, "macos", arch)
    evidence = donor.downloaded(logs, d)
    donor.donor_receipts(work, d)
    migration._ready(work / "src", d["previous_key"])
    old = live_source(previous, work, work / "fingerprint-diagnostics/mac155-png-donor-source.json",
                      d, old=True, trusted=repo)
    policy = Policy(work, d)
    for original, archive in ARCHIVED_RECEIPTS.items():
        require(not (work / archive).exists(), "original PNG receipt archive already exists")
        arp._atomic_write(work / archive, read(work, original))
    result = migration.migrate(work, previous, repo, "macos", arch, patch_bin=patch_bin, policy=policy)
    policy.validate_preserved(work / "src")
    current = live_source(repo, work, work / "fingerprint-diagnostics/mac155-png-source-admission.json", d)
    require(current["outputs"] == dict(old["outputs"], **{SOURCE: AFTER}),
            "PNG migration changed other patch outputs")
    receipt = dict(result, operation="mac155-compiled-png-migration", donor=d, download_evidence=evidence,
                   recipes={name: sha(read(repo, name)) for name in RECIPE_FILES},
                   preserved_out_inventory=identity_hash(policy.out),
                   patch_receipt=sha(read(work / "src", arp.MARKER)))
    arp._atomic_write(work / RECEIPT, arp._json(receipt))
    return receipt


def continuation(repo, work, stage, phase):
    require(1 <= stage <= 8 and phase in ("before", "after"), "PNG continuation stage/phase invalid")
    receipt = json.loads(read(work, RECEIPT))
    d = donor.checkpoint(receipt.get("donor", {}).get("arch"))
    require(os.environ.get("GITHUB_WORKFLOW") == d["consumer_workflow"]
            and os.environ.get("CHROMIX_BUILD_PROFILE") == "fast"
            and os.environ.get("CHROMIX_USE_UPSTREAM_CACHE") == "1",
            "PNG continuation requires its native fast/cache workflow")
    reviewed(repo)
    expected = dict(status="migrated", operation="mac155-compiled-png-migration", donor=d,
                    old_patch_count=225, patch_count=226, changed_files=[SOURCE],
                    previous_key=d["previous_key"], key=d["key"])
    require(all(receipt.get(k) == v for k, v in expected.items()), "PNG migration provenance mismatch")
    require(receipt.get("recipes") == {name: sha(read(repo, name)) for name in RECIPE_FILES},
            "admitted PNG recipes changed")
    require(migration.source_ready_key(repo, "macos", d["arch"]) == d["key"], "PNG preparation key changed")
    donor.reject_mixed_receipts(work, d)
    src = work / "src"
    for name in (migration.TRANSACTION, arp.IN_PROGRESS):
        require(not (src / name).exists(), "interrupted PNG migration")
    migration._ready(src, d["key"])
    require(arp.patch_selection.source_version(src) == d["version"], "PNG source version changed")
    require(sha(read(src, arp.MARKER)) == receipt.get("patch_receipt"), "PNG patch receipt changed")
    require(sha(read(src, SOURCE)) == AFTER
            and sha(read(src, controller.SOURCE)) == controller.REVIEWED_SOURCE_AFTER_SHA256,
            "PNG/controller source changed")
    for name, digest in d["pinned_files"].items():
        if name not in MUTABLE_RECEIPTS:
            require(sha(read(work, name)) == digest, "historical PNG provenance/GN changed: " + name)
    for original, archive in ARCHIVED_RECEIPTS.items():
        require(sha(read(work, archive)) == d["pinned_files"][original], "original PNG receipt archive changed")
    (arm if d["arch"] == "arm64" else donor.common).preparation(work)
    migration.restore.verify_restored(work, "macos", d["arch"], repo=repo)
    state = {}
    for name in d["state"]:
        data = read(src, name)
        require(bool(data), "missing incremental PNG state: " + name)
        state[name] = sha(data)
    if stage == 1 and phase == "before":
        require(state == d["state"] and identity_hash(controller.out_inventory(src))
                == receipt.get("preserved_out_inventory"), "compiled outputs changed before PNG continuation")
    output = work / "fingerprint-diagnostics" / (
        f"mac155-png-s{stage}-{phase}-attempt-{os.environ.get('GITHUB_RUN_ATTEMPT', 'local')}.json")
    live_source(repo, work, output.with_name(output.stem + "-source.json"), d)
    result = dict(expected, continuation_stage=stage, phase=phase,
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
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"mac155 PNG continuation rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
