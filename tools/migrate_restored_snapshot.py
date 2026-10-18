#!/usr/bin/env python3
"""Explicitly migrate a verified, already-patched restored POSIX source snapshot.

The caller must authenticate previous-repo's exact original head_sha separately.
Only current Python helpers and trusted host Git/GNU patch are executed. Interrupted
migrations are not resumable; restore a clean snapshot rather than removing markers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

try:
    from . import apply_restored_patches as arp
    from . import restore_upstream_cache as restore
    from .platform_pins import OVERRIDES, load_pins
except ImportError:
    import apply_restored_patches as arp
    import restore_upstream_cache as restore
    from platform_pins import OVERRIDES, load_pins

READY = ".chromix-source-ready"
SOURCE_VERSION = "chrome/VERSION"
# Existing preparation entry points recognize both blockers.
TRANSACTION = ".chromix-domain-substitution-in-progress"
PIN_FILES = ("CHROMIUM_VERSION", "build/ungoogled-revisions.psd1", "build/upstream-cache.json",
             *(filename for _, filename in OVERRIDES.values()))
SCRIPT_FILES = ("build/prepare-ungoogled.sh", "build/apply-patches.sh",
                "tools/apply_restored_patches.py", "tools/patch_selection.py",
                "tools/platform_pins.py")
PLATFORM_TOOLING = {
    "linux": ("ungoogled-chromium-portablelinux", "UngoogledLinuxCommit"),
    "macos": ("ungoogled-chromium-macos", "UngoogledMacOSCommit"),
}
LINUX155_DONOR = "e1263e2a52ea8eb7fa4f6c3a9a1930b92a8afe7e"
LINUX155_VERSION = "155.0.8059.39"
LINUX155_CORE = "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a"
LINUX155_OVERLAY = "025d99db1673a21992534551598b73ca79360762"
LINUX155_OLD_SELECTOR = "87d06a9ed672af8bfe1065d43d69140c142d1770287d1b82fb9c0e5d2fad4f11"
LINUX155_NEW_SELECTOR = "9a706c5ec4aca59a1a06154fe7c592746f32c2c28571ee33a7c5d55fd523bb26"
MACOS154_NEW_SELECTOR = "3eb4381914588389feec36640cfd70c7838c38d9da2ba0ab34c3e53dfa3281f3"
LINUX155_PNG_SELECTOR = "7e59710e98b88556c19d5cf53563eee0dc4f94b2cdabed0051adcad68fe11542"
WINDOWS155_PNG_SELECTOR = "4c2392a036f0e3c754ed887b41e67e624d80350f53d0059f7308205d05ca5185"
LINUX15579_SELECTOR = "5924597c7cf2836651d818f7cc711acbe26c1483377c340904f58a6e365bdf1a"
LINUX155_SUPPORTING_SELECTORS = (
    LINUX155_NEW_SELECTOR, MACOS154_NEW_SELECTOR, LINUX155_PNG_SELECTOR, WINDOWS155_PNG_SELECTOR,
    LINUX15579_SELECTOR,
)
MACOS154_SUPPORTING_SELECTORS = (
    MACOS154_NEW_SELECTOR, LINUX155_PNG_SELECTOR, WINDOWS155_PNG_SELECTOR, LINUX15579_SELECTOR,
)
LINUX155_PNG_SUPPORTING_SELECTORS = (LINUX155_PNG_SELECTOR, WINDOWS155_PNG_SELECTOR, LINUX15579_SELECTOR)
LINUX155_MANIFEST = "build/patch-additions-linux155.json"
LINUX155_MANIFEST_SHA256 = "28fb67c9207bfe6e9b8314a0e56856c5136cc6a98461ad826f9ad7e8c4fbe38f"
LINUX155_PATCH = "patches/chromium155-linux/0225-managed-profile-controller-link.patch"
LINUX155_PATCH_SHA256 = "350b333232d5865d9dff93a32b1e7b9c2e16a0c1fd31479809a0fd9106db700a"
LINUX155_OLD_IDENTITY = "9143bbdf713f7280f80d3225b7f0a4481f62a7682a4e41e62fe5cb38412235fe"
LINUX155_PRESERVED = (
    "build/prepare-ungoogled.sh", "build/apply-patches.sh",
    "build/posix/prepare-restored-tools.sh", "tools/apply_restored_patches.py",
    "tools/platform_pins.py", "tools/prepare_restored_build.py",
    "tools/linux_restored_generators.py", "tools/install_linux_generators.py",
)

MACOS154_DONOR = "4632c980d1154ff59243b97fa1b0b816d2440eaa"
MACOS154_VERSION = "154.0.8037.97"
MACOS154_CORE = "3e46b13825f808f0886e484d44532372655e5fe4"
MACOS154_OVERLAY = "f7ba75f94442abda7ac3ea81790c217f8636d3ba"
MACOS154_OLD_SELECTOR = "f8500dae237c64a4493b354c798ee4d023f2131a8f0bba0d34a92865728c07ea"
MACOS154_MANIFEST = "build/patch-additions-macos154.json"
MACOS154_MANIFEST_SHA256 = "be73565822a8b4177e63ea16d6e5936d263fed0fbf011366b3fcda4ec7b9a8bd"
MACOS154_PATCH = "patches/chromium154-macos/0226-headless-command-explicit-quit.patch"
MACOS154_PATCH_SHA256 = "ec1868b516e0ffea9366952f1e2d9c424aa2d5555b0e12d2bc456fd631d0ab73"
MACOS154_OLD_IDENTITY = "832840b6e1089964c28c35c8b68796b666d4159d2fc6919775231312c35051f2"
MACOS154_PRESERVED = LINUX155_PRESERVED

LINUX155_PNG_DONOR = "12a501c7abe607552c49d5daaabf29d891dde9ae"
LINUX155_PNG_OLD_IDENTITY = "5865eec63d8ecf9683a33406b42ce6557e3f7b37757c39a7fad1cac622e83396"
LINUX155_PNG_MANIFEST_SHA256 = "5480c06026cf67f27f23fc4bdff4122c294cb0a8a16c4b5dfd7865a72e6c6f1f"
LINUX155_PNG_PATCH = "patches/chromium155-linux/0227-opaque-png-export-consistency.patch"
LINUX155_PNG_PATCH_SHA256 = "4c048a50930c3541a3bb9e4442ad7f18e94c04ebccf86c0cfbfc286b15a70751"
LINUX155_PNG_SOURCE = "third_party/blink/renderer/core/html/canvas/canvas_async_blob_creator.cc"


# Historical 155.39 recovery must not borrow the new donor's version/tool identities.
HISTORICAL_PREPARATION_HASHES = {
    "build/prepare-ungoogled.sh": "585dfbf764936e1b1b04a006a1f045ccf2d7423473d05813bb660bf4fba61384",
    "tools/linux_restored_generators.py": "51c08b4bd442430d37a9a6fa779db01e9467a1346e0f3e402af0e41ed2a74c8b",
}


def _trusted_preparation(name: str, raw: bytes) -> bool:
    return (raw == arp._read(Path(__file__).resolve().parents[1], name)
            or arp._sha(raw) == HISTORICAL_PREPARATION_HASHES.get(name))


def _pins(repo: Path, platform: str) -> dict:
    for name in ("build/ungoogled-revisions.psd1", "CHROMIUM_VERSION"):
        arp._read(repo, name)
    filename = OVERRIDES[platform][1]
    path = arp._path(repo, filename)
    if path.exists():
        arp._read(repo, filename)
    return load_pins(repo, platform)


def _donor_git(previous: Path, repo: Path, *args: str) -> bytes:
    program = _host_program("git", (previous, repo))
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1",
                       LC_ALL="C")
    result = subprocess.run(
        [program, "--no-pager", "-c", "core.fsmonitor=false", "-c",
         "core.hooksPath=" + os.devnull, "-C", str(previous), *args],
        env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=60, check=False)
    if result.returncode:
        raise arp.ApplyError("migration cannot authenticate donor Git objects")
    return result.stdout


def _linux155_selector_transition(previous: Path, repo: Path, platform: str,
                                  arch: str | None) -> None:
    """Authenticate one historical selector as data; execute only the current selector."""
    if (platform, arch) != ("linux", "x64"):
        raise arp.ApplyError("Linux155 selector transition requires linux x64")
    selector = "tools/patch_selection.py"
    if (arp._sha(arp._read(previous, selector)) != LINUX155_OLD_SELECTOR
            or arp._sha(arp._read(repo, selector)) not in LINUX155_SUPPORTING_SELECTORS):
        raise arp.ApplyError("Linux155 migration selector hash mismatch")
    if _donor_git(previous, repo, "rev-parse", "HEAD").decode().strip() != LINUX155_DONOR:
        raise arp.ApplyError("Linux155 migration requires exact donor HEAD")
    pins = _pins(repo, platform)
    if tuple(pins[key] for key in ("ChromiumVersion", "UngoogledCommit", "UngoogledLinuxCommit")) != (
            LINUX155_VERSION, LINUX155_CORE, LINUX155_OVERLAY):
        raise arp.ApplyError("Linux155 migration core/platform/version mismatch")
    for name in (*LINUX155_PRESERVED, selector):
        original = arp._read(previous, name)
        if original != _donor_git(previous, repo, "cat-file", "blob", LINUX155_DONOR + ":" + name):
            raise arp.ApplyError("Linux155 donor preparation bytes changed: " + name)
        if name != selector and (original != arp._read(repo, name)
                                 or not _trusted_preparation(name, original)):
            raise arp.ApplyError("Linux155 migration requires preserved preparation tooling: " + name)
    manifest = arp._read(repo, LINUX155_MANIFEST)
    patch = arp._read(repo, LINUX155_PATCH)
    if (arp._sha(manifest) != LINUX155_MANIFEST_SHA256
            or arp._sha(patch) != LINUX155_PATCH_SHA256):
        raise arp.ApplyError("Linux155 migration addition hash mismatch")
    old, old_patches = arp.patch_selection.select(previous, platform)
    current, patches = arp.patch_selection.select(repo, platform)
    if (len(old_patches) != 224 or len(patches) != 225
            or patches != [*old_patches, (LINUX155_PATCH, patch)]
            or arp._sha(arp._json(old)) != LINUX155_OLD_IDENTITY):
        raise arp.ApplyError("Linux155 migration requires identical historical 224-patch prefix")
    expected = dict(old["selection"], additions={
        "schema_version": 1, "kind": "linux155-managed-profile-controller-link",
        "manifest": {"path": LINUX155_MANIFEST, "sha256": LINUX155_MANIFEST_SHA256},
        "patches": [{"path": LINUX155_PATCH, "sha256": LINUX155_PATCH_SHA256}],
    })
    if current["selection"] != expected:
        raise arp.ApplyError("Linux155 migration selection identity changed")
    if arp._read(previous, "patches/series") != arp._read(repo, "patches/series"):
        raise arp.ApplyError("Linux155 migration must preserve root series bytes")


def _linux155_png_selector_transition(previous: Path, repo: Path, platform: str,
                                      arch: str | None) -> None:
    """Authenticate the compiled 225-patch donor and one opaque PNG addition."""
    if (platform, arch) != ("linux", "x64"):
        raise arp.ApplyError("Linux155 PNG selector transition requires linux x64")
    selector = "tools/patch_selection.py"
    if (arp._sha(arp._read(previous, selector)) != LINUX155_NEW_SELECTOR
            or arp._sha(arp._read(repo, selector)) not in LINUX155_PNG_SUPPORTING_SELECTORS):
        raise arp.ApplyError("Linux155 PNG migration selector hash mismatch")
    if _donor_git(previous, repo, "rev-parse", "HEAD").decode().strip() != LINUX155_PNG_DONOR:
        raise arp.ApplyError("Linux155 PNG migration requires exact compiled donor HEAD")
    pins = _pins(repo, platform)
    if tuple(pins[key] for key in ("ChromiumVersion", "UngoogledCommit", "UngoogledLinuxCommit")) != (
            LINUX155_VERSION, LINUX155_CORE, LINUX155_OVERLAY):
        raise arp.ApplyError("Linux155 PNG migration core/platform/version mismatch")
    for name in dict.fromkeys((*PIN_FILES, *SCRIPT_FILES, *LINUX155_PRESERVED, "patches/series")):
        original = arp._read(previous, name)
        if original != _donor_git(previous, repo, "cat-file", "blob", LINUX155_PNG_DONOR + ":" + name):
            raise arp.ApplyError("Linux155 PNG donor preparation bytes changed: " + name)
        if name != selector and original != arp._read(repo, name):
            raise arp.ApplyError("Linux155 PNG migration requires preserved inputs: " + name)
        if name in LINUX155_PRESERVED and not _trusted_preparation(name, original):
            raise arp.ApplyError("Linux155 PNG migration requires trusted preparation tooling: " + name)
    if (arp._sha(arp._read(previous, LINUX155_MANIFEST)) != LINUX155_MANIFEST_SHA256
            or arp._sha(arp._read(repo, LINUX155_MANIFEST)) != LINUX155_PNG_MANIFEST_SHA256):
        raise arp.ApplyError("Linux155 PNG migration manifest hash mismatch")
    patch = arp._read(repo, LINUX155_PNG_PATCH)
    if arp._sha(patch) != LINUX155_PNG_PATCH_SHA256:
        raise arp.ApplyError("Linux155 PNG migration addition hash mismatch")
    if arp.transform_patch(patch, set(), [])[1] != [(LINUX155_PNG_SOURCE, "modify", None)]:
        raise arp.ApplyError("Linux155 PNG migration addition must only modify async canvas")
    old, old_patches = arp.patch_selection.select(previous, platform)
    current, patches = arp.patch_selection.select(repo, platform)
    if (len(old_patches) != 225 or len(patches) != 226
            or patches != [*old_patches, (LINUX155_PNG_PATCH, patch)]
            or arp._sha(arp._json(old)) != LINUX155_PNG_OLD_IDENTITY):
        raise arp.ApplyError("Linux155 PNG migration requires identical historical 225-patch prefix")
    expected = dict(old["selection"], additions={
        "schema_version": 2, "kind": "linux155-compiled-png-recovery",
        "manifest": {"path": LINUX155_MANIFEST, "sha256": LINUX155_PNG_MANIFEST_SHA256},
        "patches": [{"path": LINUX155_PATCH, "sha256": LINUX155_PATCH_SHA256},
                    {"path": LINUX155_PNG_PATCH, "sha256": LINUX155_PNG_PATCH_SHA256}],
    })
    if current["selection"] != expected:
        raise arp.ApplyError("Linux155 PNG migration selection identity changed")


def _linux155_arm64_selector_transition(previous: Path, repo: Path, platform: str,
                                      arch: str | None) -> None:
    """Authenticate the ARM64 stage4 224-patch donor and both ordered additions."""
    if (platform, arch) != ("linux", "arm64"):
        raise arp.ApplyError("Linux155 ARM64 selector transition requires linux arm64")
    selector = "tools/patch_selection.py"
    if (arp._sha(arp._read(previous, selector)) != LINUX155_OLD_SELECTOR
            or arp._sha(arp._read(repo, selector)) not in LINUX155_PNG_SUPPORTING_SELECTORS):
        raise arp.ApplyError("Linux155 ARM64 migration selector hash mismatch")
    if _donor_git(previous, repo, "rev-parse", "HEAD").decode().strip() != LINUX155_DONOR:
        raise arp.ApplyError("Linux155 ARM64 migration requires exact stage4 donor HEAD")
    pins = _pins(repo, platform)
    if tuple(pins[key] for key in ("ChromiumVersion", "UngoogledCommit", "UngoogledLinuxCommit")) != (
            LINUX155_VERSION, LINUX155_CORE, LINUX155_OVERLAY):
        raise arp.ApplyError("Linux155 ARM64 migration core/platform/version mismatch")
    for name in dict.fromkeys((*PIN_FILES, *SCRIPT_FILES, *LINUX155_PRESERVED, "patches/series")):
        original = arp._read(previous, name)
        if original != _donor_git(previous, repo, "cat-file", "blob", LINUX155_DONOR + ":" + name):
            raise arp.ApplyError("Linux155 ARM64 donor preparation bytes changed: " + name)
        if name != selector and original != arp._read(repo, name):
            raise arp.ApplyError("Linux155 ARM64 migration requires preserved inputs: " + name)
        if name in LINUX155_PRESERVED and not _trusted_preparation(name, original):
            raise arp.ApplyError("Linux155 ARM64 migration requires trusted preparation tooling: " + name)
    if arp._sha(arp._read(repo, LINUX155_MANIFEST)) != LINUX155_PNG_MANIFEST_SHA256:
        raise arp.ApplyError("Linux155 ARM64 migration manifest hash mismatch")
    patch = arp._read(repo, LINUX155_PNG_PATCH)
    if arp._sha(patch) != LINUX155_PNG_PATCH_SHA256:
        raise arp.ApplyError("Linux155 ARM64 migration addition hash mismatch")
    if arp.transform_patch(patch, set(), [])[1] != [(LINUX155_PNG_SOURCE, "modify", None)]:
        raise arp.ApplyError("Linux155 ARM64 migration addition must only modify async canvas")
    profile_patch = arp._read(repo, LINUX155_PATCH)
    if arp._sha(profile_patch) != LINUX155_PATCH_SHA256:
        raise arp.ApplyError("Linux155 ARM64 migration profile addition hash mismatch")
    old, old_patches = arp.patch_selection.select(previous, platform)
    current, patches = arp.patch_selection.select(repo, platform)
    if (len(old_patches) != 224 or len(patches) != 226
            or patches != [*old_patches, (LINUX155_PATCH, profile_patch), (LINUX155_PNG_PATCH, patch)]
            or arp._sha(arp._json(old)) != LINUX155_OLD_IDENTITY):
        raise arp.ApplyError("Linux155 ARM64 migration requires identical historical 224-patch prefix")
    expected = dict(old["selection"], additions={
        "schema_version": 2, "kind": "linux155-compiled-png-recovery",
        "manifest": {"path": LINUX155_MANIFEST, "sha256": LINUX155_PNG_MANIFEST_SHA256},
        "patches": [{"path": LINUX155_PATCH, "sha256": LINUX155_PATCH_SHA256},
                    {"path": LINUX155_PNG_PATCH, "sha256": LINUX155_PNG_PATCH_SHA256}],
    })
    if current["selection"] != expected:
        raise arp.ApplyError("Linux155 ARM64 migration selection identity changed")


def _macos154_selector_transition(previous: Path, repo: Path, platform: str,
                                  arch: str | None) -> None:
    """Authenticate one historical selector as data; execute only the current selector."""
    if platform != "macos" or arch not in ("x64", "arm64"):
        raise arp.ApplyError("macOS154 selector transition requires macos x64/arm64")
    selector = "tools/patch_selection.py"
    if (arp._sha(arp._read(previous, selector)) != MACOS154_OLD_SELECTOR
            or arp._sha(arp._read(repo, selector)) not in MACOS154_SUPPORTING_SELECTORS):
        raise arp.ApplyError("macOS154 migration selector hash mismatch")
    if _donor_git(previous, repo, "rev-parse", "HEAD").decode().strip() != MACOS154_DONOR:
        raise arp.ApplyError("macOS154 migration requires exact donor HEAD")
    pins = _pins(repo, platform)
    if tuple(pins[key] for key in ("ChromiumVersion", "UngoogledCommit", "UngoogledMacOSCommit")) != (
            MACOS154_VERSION, MACOS154_CORE, MACOS154_OVERLAY):
        raise arp.ApplyError("macOS154 migration core/platform/version mismatch")
    for name in (*MACOS154_PRESERVED, selector):
        original = arp._read(previous, name)
        if original != _donor_git(previous, repo, "cat-file", "blob", MACOS154_DONOR + ":" + name):
            raise arp.ApplyError("macOS154 donor preparation bytes changed: " + name)
        if name != selector and original != arp._read(repo, name):
            raise arp.ApplyError("macOS154 migration requires preserved preparation tooling: " + name)
    manifest = arp._read(repo, MACOS154_MANIFEST)
    patch = arp._read(repo, MACOS154_PATCH)
    if (arp._sha(manifest) != MACOS154_MANIFEST_SHA256
            or arp._sha(patch) != MACOS154_PATCH_SHA256):
        raise arp.ApplyError("macOS154 migration addition hash mismatch")
    old, old_patches = arp.patch_selection.select(previous, platform)
    current, patches = arp.patch_selection.select(repo, platform)
    if (len(old_patches) != 224 or len(patches) != 225
            or patches != [*old_patches, (MACOS154_PATCH, patch)]
            or arp._sha(arp._json(old)) != MACOS154_OLD_IDENTITY):
        raise arp.ApplyError("macOS154 migration requires identical historical 224-patch prefix")
    expected = dict(old["selection"], additions={
        "schema_version": 1, "kind": "macos154-headless-command-explicit-quit",
        "manifest": {"path": MACOS154_MANIFEST, "sha256": MACOS154_MANIFEST_SHA256},
        "patches": [{"path": MACOS154_PATCH, "sha256": MACOS154_PATCH_SHA256}],
    })
    if current["selection"] != expected:
        raise arp.ApplyError("macOS154 migration selection identity changed")
    if arp._read(previous, "patches/series") != arp._read(repo, "patches/series"):
        raise arp.ApplyError("macOS154 migration must preserve root series bytes")


def _same_inputs(previous: Path, repo: Path, platform: str = "linux",
                 arch: str | None = None) -> None:
    png_transition = (platform == "linux" and (repo / LINUX155_MANIFEST).is_file()
                      and arp._sha(arp._read(repo, LINUX155_MANIFEST)) == LINUX155_PNG_MANIFEST_SHA256)
    if png_transition:
        if arch == "arm64":
            _linux155_arm64_selector_transition(previous, repo, platform, arch)
        else:
            _linux155_png_selector_transition(previous, repo, platform, arch)
    macos_transition = (platform == "macos" and arp._read(previous, "tools/patch_selection.py")
                        != arp._read(repo, "tools/patch_selection.py"))
    if macos_transition:
        _macos154_selector_transition(previous, repo, platform, arch)
    for name in (*PIN_FILES, *SCRIPT_FILES):
        if name in {filename for _, filename in OVERRIDES.values()}:
            if name != OVERRIDES[platform][1]:
                continue
            paths = [arp._path(root, name) for root in (previous, repo)]
            if all(not path.exists() for path in paths):
                continue
            if not all(path.is_file() for path in paths):
                raise arp.ApplyError(f"migration requires identical pins/preparation tooling: {name}")
        if arp._read(previous, name) != arp._read(repo, name):
            if name == "tools/patch_selection.py":
                if not macos_transition and not png_transition:
                    if arp._sha(arp._read(previous, name)) == LINUX155_NEW_SELECTOR:
                        _linux155_png_selector_transition(previous, repo, platform, arch)
                    else:
                        _linux155_selector_transition(previous, repo, platform, arch)
            else:
                raise arp.ApplyError(f"migration requires identical pins/preparation tooling: {name}")
    for name in SCRIPT_FILES:
        if (name == "tools/patch_selection.py" and platform == "linux"
                and arp._read(previous, name) != arp._read(repo, name)):
            # The Linux transition already authenticated its exact reviewed selector.
            continue
        # The exact macOS donor tooling was authenticated above, never executed here.
        if macos_transition and name in (*MACOS154_PRESERVED, "tools/patch_selection.py"):
            continue
        raw = arp._read(repo, name)
        historical_linux = (platform == "linux" and arp._read(previous, "tools/patch_selection.py")
                            != arp._read(repo, "tools/patch_selection.py"))
        if (raw != arp._read(Path(__file__).resolve().parents[1], name)
                and not (historical_linux and _trusted_preparation(name, raw))):
            raise arp.ApplyError(f"repository preparation tooling differs from running trusted checkout: {name}")


def source_ready_key(repo: Path, platform: str, arch: str) -> str:
    """Match prepare-ungoogled.sh's path-plus-content hash without running it."""
    pins = _pins(repo, platform)
    digest = arp.patch_selection.preparation_key(repo, platform, "posix")
    return "|".join((platform, arch, pins["ChromiumVersion"], pins["UngoogledCommit"],
                     pins[PLATFORM_TOOLING[platform][1]], digest))


def _host_program(name: str, roots: tuple[Path, ...]) -> str:
    found = shutil.which(name)
    if not found:
        raise arp.ApplyError(f"required trusted host executable not found: {name}")
    path = Path(found).resolve()
    if any(path.is_relative_to(root) for root in roots):
        raise arp.ApplyError(f"refusing to execute previous repository/donor code: {path}")
    return str(path)


def _verify_tooling(work: Path, repo: Path, roots: tuple[Path, ...], platform: str) -> None:
    """Hash tracked files against pinned Git objects without invoking worktree filters."""
    git = _host_program("git", roots)
    pins = _pins(repo, platform)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1",
                       LC_ALL="C")
    platform_name, platform_pin = PLATFORM_TOOLING[platform]
    for name, commit in (("ungoogled-chromium", pins["UngoogledCommit"]),
                         (platform_name, pins[platform_pin])):
        root = restore.local_path(work / "tooling" / name)
        command = [git, "--no-pager", "-c", "core.fsmonitor=false", "-c",
                   "core.hooksPath=" + os.devnull, "-C", str(root)]

        def run(*args):
            result = subprocess.run([*command, *args], env=environment, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                                    check=False)
            if result.returncode:
                raise arp.ApplyError(f"pinned tooling verification failed: {name}: "
                                     + result.stderr.decode(errors="replace"))
            return result.stdout

        if run("rev-parse", "HEAD").decode().strip() != commit:
            raise arp.ApplyError(f"tooling checkout does not match pins: {name}")
        required = {"domain_regex.list", "domain_substitution.list"} if name == "ungoogled-chromium" else set()
        for entry in run("ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
            if not entry:
                continue
            metadata, filename = entry.split(b"\t", 1)
            mode, kind, digest = metadata.decode().split()
            filename = filename.decode("utf-8")
            if (name == platform_name and filename == "ungoogled-chromium"
                    and (mode, kind, digest) == ("160000", "commit", pins["UngoogledCommit"])):
                continue
            if kind != "blob" or mode not in ("100644", "100755"):
                raise arp.ApplyError(f"unsupported pinned tooling entry: {name}/{filename}")
            raw = arp._read(root, filename)
            actual = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
            executable = bool(arp._path(root, filename).stat().st_mode & 0o111)
            if actual != digest or executable != (mode == "100755"):
                raise arp.ApplyError(f"pinned tooling content/mode changed: {name}/{filename}")
            required.discard(filename)
        if required:
            raise arp.ApplyError(f"regex/list missing from pinned tooling: {name}: {sorted(required)}")


def _names(patches: list, lite: dict) -> set[str]:
    names = set(lite) | {entry[0] for _, _, entries in patches for entry in entries}
    if any(name.split("/", 1)[0].lower() == "out" for name in names):
        raise arp.ApplyError("patch/lite inputs must not touch the object cache (out/)")
    return names


def _marker(src: Path, name: str) -> Path:
    return arp._path(src, name, marker=True)


def _ready(src: Path, key: str) -> None:
    path = _marker(src, READY)
    if not path.is_file() or path.read_bytes().rstrip(b"\n") != key.encode():
        raise arp.ApplyError(f"previous source-ready key does not match scripts/pins/arch. {arp.CLEAN}")


def _tooling_identity_root(identity: dict) -> Path:
    roots = []
    for field, filename in (("regex", "domain_regex.list"), ("list", "domain_substitution.list")):
        entry = identity.get(field)
        raw = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(raw, str) or not raw.startswith("/"):
            raise ValueError(f"invalid {field} tooling path")
        path = Path(raw)
        if (path.as_posix() != raw or path.parts[-3:] != ("tooling", "ungoogled-chromium", filename)
                or len(path.parts) < 5):
            raise ValueError(f"invalid {field} tooling root/suffix")
        # The old host need not exist here; any existing path components must be safe.
        arp._path(Path("/"), raw[1:])
        roots.append(path.parents[2])
    if roots[0] != roots[1]:
        raise ValueError("regex/list must share one tooling root")
    return roots[0]


def _previous_completed(src: Path, identity: dict, names: set[str]) -> bytes:
    """Accept only a relocation of the two POSIX tooling paths, without rewriting receipts."""
    marker = _marker(src, arp.MARKER)
    if not marker.exists():
        raise arp.ApplyError("old restored patch completion manifest is missing")
    try:
        saved = json.loads(marker.read_bytes())
        old = saved.get("identity") if isinstance(saved, dict) else None
        if (not isinstance(old, dict) or type(saved.get("schema_version")) is not int
                or saved["schema_version"] != arp.SCHEMA
                or saved.get("identity_sha256") != arp._sha(arp._json(old))):
            raise ValueError("invalid saved identity hash/schema")
        _tooling_identity_root(old)
        _tooling_identity_root(identity)
        relocated = dict(old)
        for field in ("regex", "list"):
            relocated[field] = dict(old[field], path=identity[field]["path"])
        if arp._json(relocated) != arp._json(identity):
            raise ValueError("non-path restored patch identity changed")
        # Reuse the strict checker for the original identity and every output, including deletions.
        if not arp._completed(src, old, names):
            raise ValueError("completion manifest disappeared")
        return arp._json(saved)
    except (ValueError, TypeError, OSError) as exc:
        raise arp.ApplyError(f"invalid previous restored patches: {exc}. {arp.CLEAN}") from exc


def _claim(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise arp.ApplyError(f"another or interrupted migration owns {path.name}. {arp.CLEAN}") from exc


def _reverse(stage: Path, scratch: Path, patches: list, program: str) -> None:
    for number, (name, data, entries) in enumerate(reversed(patches)):
        for target, action, _ in entries:
            path = arp._path(stage, target)
            if (action == "delete" and path.exists()
                    or action != "delete" and not path.is_file()):
                raise arp.ApplyError(f"unexpected old reverse input: {target}")
        patch_file = scratch / f"reverse-{number:04d}.patch"
        patch_file.write_bytes(data)
        result = subprocess.run(arp._patch_command(program, patch_file, "--reverse"), cwd=stage,
                                env=arp._patch_environment(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        if result.returncode:
            raise arp.ApplyError(f"old patch reverse failed: {name}\n"
                                 + result.stdout.decode(errors="replace"))
        for target, action, mode in entries:
            path = arp._path(stage, target)
            if action == "create":
                if path.exists():
                    raise arp.ApplyError(f"reverse did not remove old created file: {target}")
            elif not path.is_file():
                raise arp.ApplyError(f"reverse did not restore file: {target}")
            elif action == "delete":
                os.chmod(path, mode or 0o644)


def _unchanged(src: Path, before: dict) -> None:
    for name, (data, info) in before.items():
        path = arp._path(src, name, marker=name.startswith(".chromix"))
        current = (path.read_bytes(), path.stat()) if path.exists() else (None, None)
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (data != current[0] or (info is None) != (current[1] is None)
                or info and any(getattr(info, field) != getattr(current[1], field) for field in fields)):
            raise arp.ApplyError(f"source/receipt changed concurrently: {name}. {arp.CLEAN}")


def migrate(workdir: Path | str, previous_repo: Path | str, repo: Path | str,
            platform: str, arch: str, *, patch_bin: str | None = None) -> dict:
    """Verify, stage, and publish only net source changes, retaining Ninja state."""
    if platform not in PLATFORM_TOOLING or arch not in ("x64", "arm64"):
        raise arp.ApplyError("migration supports only linux/macos x64/arm64")
    work, previous, repo = (restore.local_path(p) for p in (workdir, previous_repo, repo))
    src = restore.local_path(work / "src")
    core = restore.local_path(work / "tooling/ungoogled-chromium")
    tooling = restore.local_path(work / "tooling" / PLATFORM_TOOLING[platform][0])
    roots = (work, previous, repo)
    for root in (*roots, src, core, tooling):
        if not root.is_dir():
            raise arp.ApplyError(f"not a directory: {root}")
    if any(root.is_relative_to(src) for root in (previous, repo, core, tooling)):
        raise arp.ApplyError("SRC must not contain repositories or donor tooling")
    for name in (TRANSACTION, arp.IN_PROGRESS):
        if _marker(src, name).exists():
            raise arp.ApplyError(f"in-progress/partial migration detected: {name}. {arp.CLEAN}")
    _same_inputs(previous, repo, platform, arch)
    old_receipt = restore.verify_restored(work, platform, arch, repo=previous)
    receipt = restore.verify_restored(work, platform, arch, repo=repo)
    if old_receipt != receipt:
        raise arp.ApplyError("restore receipt changed during validation")
    _verify_tooling(work, repo, roots, platform)
    old_identity, old_patches, old_lite = arp._load(previous, core, tooling, platform)
    identity, patches, lite = arp._load(repo, core, tooling, platform)
    if old_identity["lite"] != identity["lite"] or old_lite != lite:
        raise arp.ApplyError("unsupported lite payload change; migration requires identical lite files/modes")
    old_names, names = _names(old_patches, old_lite), _names(patches, lite)
    if SOURCE_VERSION in old_names | names:
        raise arp.ApplyError("patch/lite inputs must not touch immutable chrome/VERSION")
    old_key, key = (source_ready_key(root, platform, arch) for root in (previous, repo))
    _ready(src, old_key)
    previous_identity = _previous_completed(src, old_identity, old_names)
    before = arp._snapshot(src, old_names | names | {SOURCE_VERSION})
    try:
        if arp.patch_selection.source_version(src) != receipt["identity"]["chromium_version"]:
            raise arp.ApplyError("source chrome/VERSION does not match repository pins")
    except ValueError as exc:
        raise arp.ApplyError(str(exc)) from exc
    markers = {name: (_marker(src, name).read_bytes(), _marker(src, name).stat())
               for name in (READY, arp.MARKER, restore.MARKER)}
    # Revalidate after taking the snapshot, before acquiring persistent blockers.
    _ready(src, old_key)
    if _previous_completed(src, old_identity, old_names) != previous_identity:
        raise arp.ApplyError("previous completion identity changed during validation")
    if restore.verify_restored(work, platform, arch, repo=repo) != receipt:
        raise arp.ApplyError("restore receipt changed during validation")
    _unchanged(src, before | markers)
    temp_root = Path(tempfile.gettempdir()).resolve()
    if any(temp_root.is_relative_to(root) for root in roots):
        raise arp.ApplyError("temporary directory must be outside workdir/repositories; set TMPDIR")
    candidates = [patch_bin] if patch_bin is not None else [os.environ.get("PATCH_BIN", "gpatch"), "patch"]
    program = arp.select_patch_program(candidates, roots)
    report = {"status": "migrated", "previous_key": old_key, "key": key,
              "identity_sha256": arp._sha(arp._json(identity)),
              "old_patch_count": len(old_patches), "patch_count": len(patches), "changed_files": []}
    payload = arp._json(dict(report, operation="restored-snapshot-migration"))
    _claim(_marker(src, TRANSACTION), payload)
    _claim(_marker(src, arp.IN_PROGRESS), payload)
    with tempfile.TemporaryDirectory(prefix="chromix-snapshot-migration-", dir=temp_root) as temporary:
        scratch = Path(temporary)
        stage = scratch / "src"
        stage.mkdir()
        for name, (data, info) in before.items():
            if data is not None:
                path = arp._path(stage, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                os.chmod(path, stat.S_IMODE(info.st_mode))
        # Selection reads VERSION even when no patch targets it.
        stage_version = arp._snapshot(stage, {SOURCE_VERSION})
        _reverse(stage, scratch, old_patches, program)
        for name, (data, _) in lite.items():
            if arp._read(stage, name) != data:
                raise arp.ApplyError(f"old reversed lite content cannot be proven unchanged: {name}")
        arp.run_apply(stage, repo, core, tooling, platform, program)
        arp.run_apply(stage, repo, core, tooling, platform, program, check=True)
        after = arp._snapshot(stage, set(before))
        _unchanged(stage, stage_version)
        _same_inputs(previous, repo, platform, arch)
        _verify_tooling(work, repo, roots, platform)
        if (arp._load(previous, core, tooling, platform) != (old_identity, old_patches, old_lite)
                or arp._load(repo, core, tooling, platform) != (identity, patches, lite)
                or source_ready_key(previous, platform, arch) != old_key
                or source_ready_key(repo, platform, arch) != key):
            raise arp.ApplyError("repository/tooling inputs changed during migration")
        if (restore.verify_restored(work, platform, arch, repo=previous) != receipt
                or restore.verify_restored(work, platform, arch, repo=repo) != receipt):
            raise arp.ApplyError("restore receipt changed during migration")
        _tooling_identity_root(json.loads(previous_identity)["identity"])
        _tooling_identity_root(old_identity)
        _ready(src, old_key)
        _unchanged(src, before | markers)
        changed = [name for name in sorted(before) if before[name][0] != after[name][0]
                   or before[name][1] and after[name][1]
                   and stat.S_IMODE(before[name][1].st_mode) != stat.S_IMODE(after[name][1].st_mode)]
        if SOURCE_VERSION in changed:
            raise arp.ApplyError("staged immutable chrome/VERSION changed during migration")
        for name in changed:
            path = arp._path(src, name)
            data, info = after[name]
            if data is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                old_info = before[name][1]
                mtime = max(time.time_ns(), old_info.st_mtime_ns + 1_000_000_000 if old_info else 0)
                arp._atomic_write(path, data, stat.S_IMODE(info.st_mode), mtime)
        # Publish the scratch manifest verbatim: it describes only the current series.
        arp._atomic_write(_marker(src, arp.MARKER), _marker(stage, arp.MARKER).read_bytes())
        arp._atomic_write(_marker(src, READY), (key + "\n").encode())
        _marker(src, arp.IN_PROGRESS).unlink()
        # TRANSACTION still blocks all preparation while the final check runs.
        arp.run_apply(src, repo, core, tooling, platform, program, check=True)
        restore.verify_restored(work, platform, arch, repo=repo)
        _ready(src, key)
        _unchanged(src, {SOURCE_VERSION: before[SOURCE_VERSION]})
        report["changed_files"] = changed
    _marker(src, TRANSACTION).unlink()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("workdir", "previous-repo", "repo"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--platform", choices=("linux", "macos"), required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--patch-bin", help="trusted host GNU patch (otherwise PATCH_BIN/gpatch/patch)")
    args = parser.parse_args(argv)
    try:
        result = migrate(args.workdir, args.previous_repo, args.repo, args.platform, args.arch,
                         patch_bin=args.patch_bin)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"snapshot migration failed: {exc}. {arp.CLEAN}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
