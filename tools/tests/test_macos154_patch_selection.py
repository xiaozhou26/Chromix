"""MAC154 donor selection and optional independent core/mac-overlay verification."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from tools import apply_restored_patches as arp
from tools import patch_selection as selection
from tools.tests.test_macos152_patch_selection import run_patch
from tools.tests.test_patch_selection import clone_repo, set_macos_pins

ROOT = Path(__file__).resolve().parents[2]
VERSION = "154.0.8037.97"
CORE = "3e46b13825f808f0886e484d44532372655e5fe4"
PLATFORM = "f7ba75f94442abda7ac3ea81790c217f8636d3ba"
BUILD = "third_party/blink/renderer/platform/BUILD.gn"
PREIMAGES = {
    BUILD: "1fb791dd0cc5208e889554a22feecfb7753f78c6adf1aca00c90d570e2b659fc",
    "third_party/blink/renderer/core/frame/web_frame_widget_test.cc":
        "5f44b5990982d74cf37d22f0ee278a9a57018134412e1303c26b3c3c3dfaa62a",
}


@pytest.fixture
def macos154_repo(tmp_path):
    repo = clone_repo(tmp_path, additions=False)
    set_macos_pins(repo)
    return repo


def test_exact_macos154_reuses_all_seven_authenticated_overrides(macos154_repo):
    identity, patches = selection.select(macos154_repo, "macos")
    series = (macos154_repo / "patches/series").read_bytes()
    assert identity["selection"] == {
        "schema_version": 1, "version": VERSION, "platform": "macos",
        "core_commit": CORE, "platform_commit": PLATFORM,
        "base_series_sha256": selection.digest(series),
    }
    assert len(patches) == 224
    assert patches == selection.select(macos154_repo, "linux")[1]
    assert patches == selection.select(macos154_repo, "windows")[1]
    assert {path for path, _ in patches if path.count("/") > 1} == {
        selection.OVERRIDE_ROOT + "/" + name for name in selection.OVERRIDES}
    for name, (_, replacement) in selection.OVERRIDES.items():
        path = selection.OVERRIDE_ROOT + "/" + name
        assert selection.digest(dict(patches)[path]) == replacement
        series = series.replace(("patches/" + name).encode(), path.encode())
    assert identity["series_sha256"] == selection.digest(series)
    assert identity["patches"] == [
        {"path": path, "sha256": selection.digest(raw)} for path, raw in patches]
    assert "patches/0209-display-native-screen-regressions.patch" in dict(patches)


@pytest.mark.parametrize("field,value", [
    ("MacOSUngoogledCommit", "0" * 40),
    ("MacOSUngoogledCommit", "37085e47cf580c815a30402917d350ce97399ded"),
    ("UngoogledMacOSCommit", "0" * 40),
    ("UngoogledMacOSCommit", "038db2b41f7aeb00bbceb2f5a56912b26eb5b284"),
    ("MacOSUngoogledVersion", VERSION + "-2"),
    ("UngoogledMacOSVersion", VERSION + "-1"),
    ("UngoogledMacOSVersion", VERSION + "-1.2"),
    ("MacOSChromiumVersion", "154.0.8037.98"),
])
def test_macos154_rejects_unreviewed_pins(macos154_repo, field, value):
    path = macos154_repo / "build/ungoogled-revisions.psd1"
    path.write_text(re.sub(rf'(?m)^(  {field} = )"[^"]+"',
                           rf'\g<1>"{value}"', path.read_text()))
    with pytest.raises(selection.SelectionError, match="pins|Ungoogled|ChromiumVersion"):
        selection.select(macos154_repo, "macos")


@pytest.mark.parametrize("mutation", ["missing", "changed", "extra", "predecessor",
                                      "symlink", "directory-symlink", "series", "pins"])
def test_macos154_authenticates_inventory_and_predecessor_hashes(macos154_repo, mutation):
    directory = macos154_repo / selection.OVERRIDE_ROOT
    name = next(iter(selection.OVERRIDES))
    patch = directory / name
    if mutation == "missing":
        patch.unlink()
    elif mutation == "changed":
        patch.write_bytes(patch.read_bytes() + b"\n")
    elif mutation == "extra":
        (directory / "unexpected.patch").write_bytes(b"unexpected")
    elif mutation == "predecessor":
        path = macos154_repo / "patches" / name
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "symlink":
        patch.unlink()
        patch.symlink_to(ROOT / selection.OVERRIDE_ROOT / name)
    elif mutation == "directory-symlink":
        shutil.rmtree(directory)
        directory.symlink_to(ROOT / selection.OVERRIDE_ROOT, target_is_directory=True)
    elif mutation == "series":
        path = macos154_repo / "patches/series"
        path.write_text(path.read_text().replace("patches/" + name + "\n", ""))
    else:
        (macos154_repo / "build/ungoogled-revisions.psd1").unlink()
    with pytest.raises(selection.SelectionError):
        selection.select(macos154_repo, "macos")


@pytest.mark.parametrize("which", ["source", "core"])
@pytest.mark.parametrize("value", [None, "152.0.7977.82", VERSION])
def test_macos154_requires_matching_source_and_core_versions(
        tmp_path, macos154_repo, which, value):
    directory = tmp_path / which
    path = directory / ("chrome/VERSION" if which == "source" else "chromium_version.txt")
    path.parent.mkdir(parents=True)
    if value:
        text = ("".join(f"{k}={v}\n" for k, v in
                        zip(("MAJOR", "MINOR", "BUILD", "PATCH"), value.split(".")))
                if which == "source" else value + "\n")
        path.write_text(text)
    kwargs = {"src" if which == "source" else "core": directory}
    if value == VERSION:
        assert selection.select(macos154_repo, "macos", **kwargs)[0]["selection"]["core_commit"] == CORE
    else:
        with pytest.raises(selection.SelectionError, match="version|missing patch input"):
            selection.select(macos154_repo, "macos", **kwargs)


@pytest.mark.parametrize("style", ["posix", "windows"])
def test_macos154_cache_key_separates_platform_identity(macos154_repo, style):
    keys = {selection.preparation_key(macos154_repo, platform, style)
            for platform in ("macos", "linux", "windows")}
    assert len(keys) == 3
    current = selection.preparation_key(macos154_repo, "macos", style)
    set_macos_pins(macos154_repo, historical=True)
    assert selection.preparation_key(macos154_repo, "macos", style) != current


def predecessor_sections(tooling, targets):
    for name in (tooling / "patches/series").read_text().splitlines():
        name = name.split("#", 1)[0].strip()
        if not name:
            continue
        text = (tooling / "patches" / name).read_text()
        for section in re.split(r"(?m)(?=^--- (?:a/|/dev/null))", text):
            match = re.match(r"--- (\S+)[^\n]*\n\+\+\+ (\S+)", section)
            if not match:
                continue
            target = (match[2] if match[2] != "/dev/null" else match[1])[2:]
            if target in targets:
                yield name, target, section


@pytest.mark.parametrize("substituted", [False, True])
def test_optional_independent_154_full_stack(tmp_path, macos154_repo, substituted):
    root = os.environ.get("CHROMIX_MACOS154_SOURCES")
    if not root:
        pytest.skip("set CHROMIX_MACOS154_SOURCES to independent pristine/core/mac directories")
    donor = Path(root)
    pristine, core, mac = (donor / name for name in ("pristine", "core", "mac"))
    assert selection.source_version(pristine) == VERSION
    assert (core / "chromium_version.txt").read_text().strip() == VERSION
    for name, expected in PREIMAGES.items():
        assert selection.digest((pristine / name).read_bytes()) == expected
    src = tmp_path / "src"
    shutil.copytree(pristine, src)
    _, selected = selection.select(macos154_repo, "macos", src=src, core=core)
    targets = {target for _, raw in selected
               for target, _, _ in arp.transform_patch(raw, set(), [])[1]}
    predecessors = list(predecessor_sections(core, targets))
    mac_sections = list(predecessor_sections(mac, targets))
    assert len(targets) == 167
    assert len(predecessors) == 21
    assert [(name, target) for name, target, _ in mac_sections] == [
        ("ungoogled-chromium/macos/fix-build-without-crubit.patch", BUILD)]
    log = []
    for name, target, section in [*predecessors, *mac_sections]:
        patch = tmp_path / "predecessor.patch"
        patch.write_text(section)
        result = run_patch(src, patch)
        log.append(name + ":" + target + "\n" + result.stdout + result.stderr)
        assert result.returncode == 0, log[-1]
    if substituted:
        rules = arp._rules((core / "domain_regex.list").read_bytes())
        listed = set((core / "domain_substitution.list").read_text().splitlines())
        for name in targets & listed:
            path = src / name
            if path.is_file():
                text, encoding = arp._decode(path.read_bytes())
                path.write_bytes(arp._substitute(text, rules).encode(encoding))
        program = shutil.which("gpatch") or "patch"
        report = arp.run_apply(src, macos154_repo, core, mac, "macos", program)
        assert report["status"] == "applied"
        assert report["patch_count"] == 224
        assert arp.run_apply(src, macos154_repo, core, mac, "macos", program, check=True)["status"] == "checked"
        (src / ".chromix-domain-substituted").write_text(CORE)
    else:
        shutil.copytree(macos154_repo / arp.LITE, src, dirs_exist_ok=True)
        for name, _ in selected:
            result = run_patch(src, macos154_repo / name)
            log.append(name + "\n" + result.stdout + result.stderr)
            assert result.returncode == 0, log[-1]
            assert not re.search(r"fuzz|FAILED", result.stdout + result.stderr, re.I)
    (tmp_path / "apply.log").write_text("\n".join(log))
    output = tmp_path / "verified.json"
    result = subprocess.run([
        sys.executable, str(ROOT / "tools/verify_patch_stack.py"),
        "--src", str(src), "--repo", str(macos154_repo), "--core", str(core),
        "--platform-tooling", str(mac), "--platform", "macos", "--output", str(output)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["status"] == "verified"
    assert report["patch_count"] == 224
    assert report["identity"]["selection"]["core_commit"] == CORE


def test_current_macos_addition_is_isolated_and_keeps_historical_prefix(tmp_path):
    repo = clone_repo(tmp_path)
    old_repo = clone_repo(tmp_path / "historical", additions=False)
    current, patches = selection.select(repo, "macos")
    old, old_patches = selection.select(old_repo, "macos")
    assert len(old_patches) == 224 and len(patches) == 225
    assert patches[:-1] == old_patches
    assert (repo / "patches/series").read_bytes() == (old_repo / "patches/series").read_bytes()
    addition = current["selection"].pop("additions")
    assert current["selection"] == old["selection"]
    assert addition["kind"] == "macos154-headless-command-explicit-quit"
    assert addition["manifest"] == {
        "path": selection.MACOS154_ADDITIONS_MANIFEST,
        "sha256": selection.MACOS154_ADDITIONS_SHA256,
    }
    assert addition["patches"] == current["patches"][-1:]
    assert patches[-1][0] == "patches/chromium154-macos/0226-headless-command-explicit-quit.patch"
    assert selection.digest(patches[-1][1]) == (
        "ec1868b516e0ffea9366952f1e2d9c424aa2d5555b0e12d2bc456fd631d0ab73")
    linux = selection.select(repo, "linux")
    windows = selection.select(repo, "windows")
    linux_key = selection.preparation_key(repo, "linux", "posix")
    assert len(windows[1]) == 231
    assert windows[0]["selection"]["additions"]["manifest"]["sha256"] == selection.WINDOWS155_ADDITIONS_SHA256
    assert len(linux[1]) == 226
    assert linux[0]["selection"]["version"] == selection.LINUX15579_VERSION
    assert linux[0]["selection"]["additions"]["manifest"]["sha256"] == (
        selection.LINUX15579_ADDITIONS_SHA256)
    shutil.rmtree(repo / selection.MACOS154_ADDITIONS_ROOT)
    (repo / selection.MACOS154_ADDITIONS_MANIFEST).unlink()
    assert selection.select(repo, "macos") == (old, old_patches)
    assert selection.select(repo, "linux") == linux
    assert selection.select(repo, "windows") == windows
    assert selection.preparation_key(repo, "linux", "posix") == linux_key
    for style in ("posix", "windows"):
        assert selection.preparation_key(repo, "macos", style) == selection.preparation_key(
            old_repo, "macos", style)


@pytest.mark.parametrize("change", [
    "manifest-missing", "manifest-changed", "manifest-symlink", "patch-missing", "patch-changed",
    "patch-symlink", "directory-missing", "directory-symlink", "extra", "pins",
])
def test_current_macos_addition_inventory_fails_closed(tmp_path, change):
    repo = clone_repo(tmp_path)
    manifest = repo / selection.MACOS154_ADDITIONS_MANIFEST
    directory = repo / selection.MACOS154_ADDITIONS_ROOT
    patch = next(directory.iterdir())
    if change == "extra":
        (directory / "unexpected.patch").write_text("unexpected")
    elif change == "pins":
        (repo / "build/ungoogled-revisions.psd1").unlink()
    else:
        path = {"manifest": manifest, "patch": patch, "directory": directory}[change.split("-")[0]]
        if change.endswith("changed"):
            path.write_bytes(path.read_bytes() + b"\n")
        else:
            relative = path.relative_to(repo)
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            if change.endswith("symlink"):
                path.symlink_to(ROOT / relative, target_is_directory=change.startswith("directory"))
    with pytest.raises(selection.SelectionError):
        selection.select(repo, "macos")
