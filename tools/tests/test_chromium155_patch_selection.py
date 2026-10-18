"""Chromium 155 authentication and optional independent upstream subset replay."""
from __future__ import annotations

from collections import Counter
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

import pytest

from tools import apply_restored_patches as arp
from tools import patch_selection as selection
from tools.tests.test_macos152_patch_selection import run_patch
from tools.tests.test_macos154_patch_selection import predecessor_sections
from tools.tests.test_patch_selection import clone_repo, set_platform_pins

ROOT = Path(__file__).resolve().parents[2]
VERSION = "155.0.8059.39"
CORE = "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a"
WINDOWS79_VERSION = "155.0.8059.79"
WINDOWS79_CORE = "9f145d8815e6e50cc4a2537fcac40966d911e027"
WINDOWS79_PLATFORM = "a1e3123c8d543b5c9a1eecfaf0f06cf242c0db37"
COMMITS = {
    "linux": "025d99db1673a21992534551598b73ca79360762",
    "windows": "989b4382364baaddb8b3180e1eb1030ca96a7273",
}
ARCHIVES = {
    "core": "83fe5a77fda1e9f558169e2020efd2c5d9fa946e398de28ecfdfa95ecbf6e55d",
    "linux": "ddab5b6d57ba2843655db701e2c22f8316d1069ddc148bea60bd8331d0651d18",
    "windows": "c21fd85df3096e0ce648bdcb769cc47e90f3d29085ed38e69d750824b5572474",
}
MANIFEST = "1e4876729baa2abddd7a219d7c01676454e2daa193a5875b34dadae24f50621f"
NEW_OVERRIDES = {
    "0031-third_party-blink-renderer-platform-graphics-image_data_buffer-cc.patch",
    "0077-third_party-blink-renderer-modules-canvas-canvas2d-base_rendering_context_2d-h.patch",
    "0082-third_party-blink-renderer-modules-webgl-webgl_rendering_context_base-cc.patch",
}


@pytest.fixture(params=("linux", "windows"))
def repo155(tmp_path, request):
    repo = clone_repo(tmp_path, additions=False)
    platform = request.param
    set_platform_pins(repo, platform, VERSION, CORE, COMMITS[platform])
    return repo, platform


def test_exact_155_authenticates_ten_overrides_and_order(repo155):
    repo, platform = repo155
    identity, patches = selection.select(repo, platform)
    assert len(patches) == 224
    assert identity["selection"] == {
        "schema_version": 1, "version": VERSION, "platform": platform,
        "core_commit": CORE, "platform_commit": COMMITS[platform],
        "base_series_sha256": selection.digest((repo / "patches/series").read_bytes()),
    }
    assert set(selection.OVERRIDES155) == set(selection.OVERRIDES) | NEW_OVERRIDES
    series = (repo / "patches/series").read_bytes()
    for name, (original, replacement) in selection.OVERRIDES155.items():
        path = selection.OVERRIDE155_ROOT + "/" + name
        assert selection.digest((repo / "patches" / name).read_bytes()) == original
        assert selection.digest(dict(patches)[path]) == replacement
        series = series.replace(("patches/" + name).encode(), path.encode())
    assert identity["series_sha256"] == selection.digest(series)
    assert identity["patches"] == [
        {"path": name, "sha256": selection.digest(raw)} for name, raw in patches]
    for name in selection.OVERRIDES:
        assert (repo / selection.OVERRIDE155_ROOT / name).read_bytes() == (
            repo / selection.OVERRIDE_ROOT / name).read_bytes()


@pytest.mark.parametrize("mutation", ["missing", "changed", "extra", "predecessor",
                                      "symlink", "directory-symlink", "series", "pins"])
def test_155_inventory_fails_closed(repo155, mutation):
    repo, platform = repo155
    directory = repo / selection.OVERRIDE155_ROOT
    name = sorted(NEW_OVERRIDES)[0]
    patch = directory / name
    if mutation == "missing":
        patch.unlink()
    elif mutation == "changed":
        patch.write_bytes(patch.read_bytes() + b"\n")
    elif mutation == "extra":
        (directory / "unexpected.patch").write_bytes(b"unexpected")
    elif mutation == "predecessor":
        patch = repo / "patches" / name
        patch.write_bytes(patch.read_bytes() + b"\n")
    elif mutation == "symlink":
        patch.unlink()
        patch.symlink_to(ROOT / selection.OVERRIDE155_ROOT / name)
    elif mutation == "directory-symlink":
        shutil.rmtree(directory)
        directory.symlink_to(ROOT / selection.OVERRIDE155_ROOT, target_is_directory=True)
    elif mutation == "series":
        patch = repo / "patches/series"
        patch.write_text(patch.read_text().replace("patches/" + name + "\n", ""))
    else:
        (repo / "build/ungoogled-revisions.psd1").unlink()
    with pytest.raises(selection.SelectionError):
        selection.select(repo, platform)


@pytest.mark.parametrize("field,value", [
    ("core_commit", "0" * 40), ("core_commit", selection.CORE),
    ("platform_commit", "0" * 40), ("core_version", VERSION + "-2"),
    ("platform_version", VERSION + "-1.2"),
])
def test_155_rejects_wrong_donor_revision(repo155, field, value):
    repo, platform = repo155
    prefix = selection.PLATFORMS[platform]
    key = {"core_commit": prefix + "UngoogledCommit",
           "core_version": prefix + "UngoogledVersion",
           "platform_commit": "Ungoogled" + prefix + "Commit",
           "platform_version": "Ungoogled" + prefix + "Version"}[field]
    path = repo / "build/ungoogled-revisions.psd1"
    path.write_text(re.sub(rf'(?m)^(  {key} = )"[^"]+"',
                           rf'\g<1>"{value}"', path.read_text()))
    with pytest.raises(selection.SelectionError, match="pins|Ungoogled"):
        selection.select(repo, platform)


@pytest.mark.parametrize("which", ["src", "core"])
@pytest.mark.parametrize("value", [None, selection.VERSION, VERSION])
def test_155_requires_matching_source_and_core(tmp_path, repo155, which, value):
    repo, platform = repo155
    directory = tmp_path / which
    path = directory / ("chrome/VERSION" if which == "src" else "chromium_version.txt")
    path.parent.mkdir(parents=True)
    if value:
        text = ("".join(f"{key}={part}\n" for key, part in
                        zip(("MAJOR", "MINOR", "BUILD", "PATCH"), value.split(".")))
                if which == "src" else value + "\n")
        path.write_text(text)
    if value == VERSION:
        assert selection.select(repo, platform, **{which: directory})[0]["selection"]
    else:
        with pytest.raises(selection.SelectionError, match="version|missing patch input"):
            selection.select(repo, platform, **{which: directory})


@pytest.mark.parametrize("style", ["posix", "windows"])
def test_155_separates_keys_without_changing_154(repo155, style):
    repo, platform = repo155
    current = selection.preparation_key(repo, platform, style)
    set_platform_pins(repo, platform, selection.VERSION, selection.CORE,
                      selection.PLATFORM_COMMITS[platform])
    historical = selection.preparation_key(repo, platform, style)
    assert current != historical
    before = selection.select(repo, platform)
    shutil.rmtree(repo / selection.OVERRIDE155_ROOT)
    assert selection.select(repo, platform) == before
    assert selection.preparation_key(repo, platform, style) == historical


def test_155_does_not_enable_macos(tmp_path):
    repo = clone_repo(tmp_path)
    set_platform_pins(repo, "macos", VERSION, CORE, COMMITS["linux"])
    with pytest.raises(selection.SelectionError, match="require Windows or Linux"):
        selection.select(repo, "macos")


def test_155_rebase_retains_native_color_conversion_and_canvas_operations():
    image = (ROOT / selection.OVERRIDE155_ROOT / sorted(NEW_OVERRIDES)[0]).read_text()
    assert "GetColorInfoForEncoder(pixmap_.info().colorInfo()," in image
    assert "+    return;\n" in image
    assert "paint_image_info.colorType(), alpha_type" not in image
    for name in sorted(NEW_OVERRIDES)[1:]:
        base = (ROOT / "patches" / name).read_text()
        override = (ROOT / selection.OVERRIDE155_ROOT / name).read_text()
        def additions(text):
            return Counter(line for line in text.splitlines()
                           if line.startswith("+") and line != "+"
                           and not line.startswith("+++"))
        assert additions(base) == additions(override)


def authenticate_donor(donor, name):
    archive = donor / (name + ".tar.gz")
    assert selection.digest(archive.read_bytes()) == ARCHIVES[name]
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if member.isfile():
                relative = Path(*Path(member.name).parts[1:])
                assert (donor / name / relative).read_bytes() == tar.extractfile(member).read()


@pytest.mark.parametrize("substituted", [False, True])
def test_optional_independent_155_full_stack(tmp_path, repo155, substituted):
    root = os.environ.get("CHROMIX_155_SOURCES")
    if not root:
        pytest.skip("set CHROMIX_155_SOURCES to independent pristine/core/linux/windows directories")
    donor = Path(root)
    repo, platform = repo155
    pristine, core, tooling = donor / "pristine", donor / "core", donor / platform
    assert selection.digest((donor / "source-manifest.json").read_bytes()) == MANIFEST
    manifest = json.loads((donor / "source-manifest.json").read_text())
    assert manifest["version"] == VERSION and not manifest["errors"]
    assert manifest["revisions"] == {
        "v8": "56832c166a78bd1eb61068ac69876d6880eaabdf",
        "webrtc": "5bd86a2f17a35dfc986b87d058f511e45805bd1d",
    }
    assert len(manifest["files"]) == 150
    for name, entry in manifest["files"].items():
        assert selection.digest((pristine / name).read_bytes()) == entry["sha256"]
    for name in ("core", platform):
        authenticate_donor(donor, name)
    assert (core / "revision.txt").read_text().strip() == "1"
    if platform == "windows":
        assert (tooling / "revision.txt").read_text().strip() == "1"
    src = tmp_path / "src"
    shutil.copytree(pristine, src)
    _, patches = selection.select(repo, platform, src=src, core=core)
    targets = {target for _, raw in patches
               for target, _, _ in arp.transform_patch(raw, set(), [])[1]}
    assert len(targets) == 167
    core_sections = list(predecessor_sections(core, targets))
    platform_sections = list(predecessor_sections(tooling, targets))
    assert len(core_sections) == 21
    overlay = ("portablelinux/revert-crubit-changes.patch" if platform == "linux"
               else "windows/windows-fix-building-without-crubit.patch")
    assert [(name, target) for name, target, _ in platform_sections] == [
        ("ungoogled-chromium/" + overlay, "third_party/blink/renderer/platform/BUILD.gn")]
    log = []
    for name, target, section in [*core_sections, *platform_sections]:
        patch = tmp_path / "predecessor.patch"
        patch.write_text(section)
        result = run_patch(src, patch)
        log.append(name + ":" + target + "\n" + result.stdout + result.stderr)
        assert result.returncode == 0, log[-1]
        assert not re.search(r"fuzz|FAILED", log[-1], re.I)
    if substituted:
        rules = arp._rules((core / "domain_regex.list").read_bytes())
        listed = set(((tooling if platform == "windows" else core) /
                      "domain_substitution.list").read_text().splitlines())
        for name in targets & listed:
            path = src / name
            if path.is_file():
                text, encoding = arp._decode(path.read_bytes())
                path.write_bytes(arp._substitute(text, rules).encode(encoding))
        program = shutil.which("gpatch") or "patch"
        report = arp.run_apply(src, repo, core, tooling, platform, program)
        assert report["status"] == "applied" and report["patch_count"] == 224
        (tmp_path / "applied.json").write_text(json.dumps(report, indent=2))
        assert arp.run_apply(src, repo, core, tooling, platform, program, check=True)["status"] == "checked"
        (src / ".chromix-domain-substituted").write_text(CORE)
    else:
        shutil.copytree(repo / arp.LITE, src, dirs_exist_ok=True)
        for name, _ in patches:
            result = run_patch(src, repo / name)
            log.append(name + "\n" + result.stdout + result.stderr)
            assert result.returncode == 0, log[-1]
            assert not re.search(r"fuzz|FAILED", log[-1], re.I)
    (tmp_path / "apply.log").write_text("\n".join(log))
    output = tmp_path / "verified.json"
    result = subprocess.run([
        sys.executable, str(ROOT / "tools/verify_patch_stack.py"),
        "--src", str(src), "--repo", str(repo), "--core", str(core),
        "--platform-tooling", str(tooling), "--platform", platform, "--output", str(output)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["status"] == "verified" and report["patch_count"] == 224
    assert report["domain_substituted"] == substituted
    assert report["identity"]["selection"]["core_commit"] == CORE


@pytest.mark.parametrize("additions,expected", [(False, 224), (True, 231)])
def test_windows155_manifest_presence_distinguishes_historical_and_current(tmp_path, additions, expected):
    repo = clone_repo(tmp_path, additions=False)
    set_platform_pins(repo, "windows", VERSION, CORE, COMMITS["windows"])
    if additions:
        manifest = selection.WINDOWS155_ADDITIONS_MANIFEST
        data = json.loads((ROOT / manifest).read_bytes())
        data.update(chromium_version=VERSION, core_commit=CORE,
                    platform_commit=COMMITS["windows"])
        raw = (json.dumps(data, indent=2) + "\n").encode()
        assert selection.digest(raw) == "4e4b2c3655b2ba1c1f5b2601d5d467ee58f98e8f535214719f1f92c5016ebddd"
        (repo / manifest).write_bytes(raw)
        shutil.copytree(ROOT / selection.WINDOWS155_ADDITIONS_ROOT,
                        repo / selection.WINDOWS155_ADDITIONS_ROOT)
    identity, patches = selection.select(repo, "windows")
    assert len(patches) == expected
    assert ("additions" in identity["selection"]) == additions
    if additions:
        assert patches[224][0] == "patches/chromium155-windows/0227-opaque-png-export-consistency.patch"
        assert patches[-1][0] == "patches/chromium155-windows/0233-renderer-timezone-webstring-api.patch"
        manifest = json.loads((repo / selection.WINDOWS155_ADDITIONS_MANIFEST).read_bytes())
        assert identity["patches"][224:] == identity["selection"]["additions"]["patches"] == manifest["patches"]


@pytest.mark.skipif(not shutil.which("git"), reason="host Git required")
def test_windows155_historical_225_selection_identity(tmp_path):
    revision = "b8821cd2f782ca4da9b998b437fc3acb09cb3fea"
    repo = tmp_path / "historical"
    repo.mkdir()
    raw = subprocess.run([shutil.which("git"), "-C", str(ROOT), "archive", revision],
                         check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        archive.extractall(repo, filter="data")
    selector = repo / "tools/patch_selection.py"
    assert selection.digest(selector.read_bytes()) == (
        "4c2392a036f0e3c754ed887b41e67e624d80350f53d0059f7308205d05ca5185")
    command = [sys.executable, str(selector), "--repo", str(repo), "--platform", "windows"]
    identity = json.loads(subprocess.run([*command, "--json"], check=True, capture_output=True).stdout)
    assert len(identity["patches"]) == 225
    assert selection.digest(arp._json(identity)) == (
        "0474128cc29677a220c1138f6ea45e77b798a81e657a14c49fcdb242fe014725")
    manifest_path = selection.WINDOWS155_ADDITIONS_MANIFEST
    manifest = json.loads((repo / manifest_path).read_bytes())
    assert identity["selection"]["additions"] == {
        "schema_version": 1, "kind": "windows155-compiled-png-recovery",
        "manifest": {"path": manifest_path, "sha256":
                     "7563d9ba3ab70750c5f4828fbeb346f1a38efce6589a10083ccfef7fef467fc1"},
        "patches": manifest["patches"],
    }
    assert identity["patches"][224:] == manifest["patches"] == [{
        "path": "patches/chromium155-windows/0227-opaque-png-export-consistency.patch",
        "sha256": "4c048a50930c3541a3bb9e4442ad7f18e94c04ebccf86c0cfbfc286b15a70751",
    }]
    key = subprocess.run([*command, "--key", "windows"], check=True, capture_output=True, text=True).stdout.strip()
    assert key == "8bb13f960d706837d90bbedd76cc4144430ef8c518ba7fdf34694792b2881667"


@pytest.fixture
def windows79(tmp_path):
    repo = clone_repo(tmp_path)
    set_platform_pins(repo, "windows", WINDOWS79_VERSION, WINDOWS79_CORE, WINDOWS79_PLATFORM)
    return repo


def test_windows15579_selects_exact_231_with_unchanged_patch_bytes(windows79):
    identity, patches = selection.select(windows79, "windows")
    assert len(patches) == 231
    assert identity["selection"]["version"] == WINDOWS79_VERSION
    assert identity["selection"]["core_commit"] == WINDOWS79_CORE
    assert identity["selection"]["platform_commit"] == WINDOWS79_PLATFORM
    manifest = windows79 / selection.WINDOWS155_ADDITIONS_MANIFEST
    assert selection.digest(manifest.read_bytes()) == (
        "af343acabc84085f49841168cc05edcd4596da061638a81110adb8ee7c0da49e")
    assert identity["selection"]["additions"]["patches"] == identity["patches"][224:]
    keys = {style: selection.preparation_key(windows79, "windows", style)
            for style in ("windows", "posix")}
    data = json.loads(manifest.read_bytes())
    data.update(chromium_version=VERSION, core_commit=CORE, platform_commit=COMMITS["windows"])
    manifest.write_text(json.dumps(data, indent=2) + "\n")
    set_platform_pins(windows79, "windows", VERSION, CORE, COMMITS["windows"])
    old_identity, old_patches = selection.select(windows79, "windows")
    assert old_patches == patches
    assert old_identity["selection"]["version"] == VERSION
    for style, key in keys.items():
        assert selection.preparation_key(windows79, "windows", style) != key


@pytest.mark.parametrize("field,value", [
    ("WindowsUngoogledCommit", CORE), ("WindowsUngoogledCommit", "0" * 40),
    ("UngoogledWindowsCommit", COMMITS["windows"]), ("UngoogledWindowsCommit", "0" * 40),
    ("WindowsUngoogledVersion", VERSION + "-1"),
    ("UngoogledWindowsVersion", WINDOWS79_VERSION + "-1.2"),
])
def test_windows15579_rejects_wrong_pins(windows79, field, value):
    path = windows79 / "build/ungoogled-revisions.psd1"
    path.write_text(re.sub(rf'(?m)^(  {field} = )"[^"]+"', rf'\g<1>"{value}"', path.read_text()))
    with pytest.raises(selection.SelectionError, match="pins|Ungoogled"):
        selection.select(windows79, "windows")


@pytest.mark.parametrize("which", ["src", "core"])
@pytest.mark.parametrize("version", [None, VERSION, selection.VERSION, WINDOWS79_VERSION])
def test_windows15579_requires_exact_source_and_core(windows79, tmp_path, which, version):
    directory = tmp_path / which
    path = directory / ("chrome/VERSION" if which == "src" else "chromium_version.txt")
    path.parent.mkdir(parents=True)
    if version:
        text = ("".join(f"{key}={value}\n" for key, value in zip(
            ("MAJOR", "MINOR", "BUILD", "PATCH"), version.split(".")))
                if which == "src" else version + "\n")
        path.write_text(text)
    if version == WINDOWS79_VERSION:
        assert len(selection.select(windows79, "windows", **{which: directory})[1]) == 231
    else:
        with pytest.raises(selection.SelectionError, match="version|missing patch input"):
            selection.select(windows79, "windows", **{which: directory})


@pytest.mark.parametrize("version", [VERSION, WINDOWS79_VERSION])
def test_windows155_manifests_cannot_cross_versions(windows79, version):
    manifest = windows79 / selection.WINDOWS155_ADDITIONS_MANIFEST
    if version == WINDOWS79_VERSION:
        data = json.loads(manifest.read_bytes())
        data.update(chromium_version=VERSION, core_commit=CORE, platform_commit=COMMITS["windows"])
        manifest.write_text(json.dumps(data, indent=2) + "\n")
    else:
        set_platform_pins(windows79, "windows", VERSION, CORE, COMMITS["windows"])
    with pytest.raises(selection.SelectionError, match="manifest"):
        selection.select(windows79, "windows")


@pytest.mark.parametrize("mutation", ["no-inputs", "no-manifest", "no-directory", "missing-patch",
                                      "extra-patch", "changed-patch", "changed-manifest",
                                      "linked-manifest", "linked-directory", "linked-patch"])
def test_windows15579_additions_fail_closed(windows79, mutation):
    manifest = windows79 / selection.WINDOWS155_ADDITIONS_MANIFEST
    directory = windows79 / selection.WINDOWS155_ADDITIONS_ROOT
    patch = directory / "0233-renderer-timezone-webstring-api.patch"
    if mutation in ("no-inputs", "no-manifest"):
        manifest.unlink()
    if mutation in ("no-inputs", "no-directory"):
        shutil.rmtree(directory)
    elif mutation == "missing-patch":
        patch.unlink()
    elif mutation == "extra-patch":
        (directory / "unexpected.patch").write_bytes(b"unexpected")
    elif mutation == "changed-patch":
        patch.write_bytes(patch.read_bytes() + b"\n")
    elif mutation == "changed-manifest":
        manifest.write_bytes(manifest.read_bytes() + b"\n")
    elif mutation == "linked-manifest":
        manifest.unlink()
        manifest.symlink_to(ROOT / selection.WINDOWS155_ADDITIONS_MANIFEST)
    elif mutation == "linked-directory":
        shutil.rmtree(directory)
        directory.symlink_to(ROOT / selection.WINDOWS155_ADDITIONS_ROOT, target_is_directory=True)
    elif mutation == "linked-patch":
        patch.unlink()
        patch.symlink_to(ROOT / selection.WINDOWS155_ADDITIONS_ROOT / patch.name)
    with pytest.raises(selection.SelectionError):
        selection.select(windows79, "windows")


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_windows15579_does_not_authorize_other_platforms(windows79, platform):
    set_platform_pins(windows79, platform, WINDOWS79_VERSION, WINDOWS79_CORE, WINDOWS79_PLATFORM)
    with pytest.raises(selection.SelectionError, match="require Windows"):
        selection.select(windows79, platform)


def test_windows15579_cli_dispatch_paths_and_source_rejection(windows79, tmp_path):
    command = [sys.executable, str(ROOT / "tools/patch_selection.py"),
               "--repo", str(windows79), "--platform", "windows", "--paths"]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert len(result.stdout.splitlines()) == 231
    assert result.stdout.splitlines()[-1] == (
        "patches/chromium155-windows/0233-renderer-timezone-webstring-api.patch")
    src = tmp_path / "src"
    (src / "chrome").mkdir(parents=True)
    (src / "chrome/VERSION").write_text("MAJOR=155\nMINOR=0\nBUILD=8059\nPATCH=39\n")
    result = subprocess.run([*command, "--src", str(src)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "source version" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("mutation", ["missing-override", "changed-override", "extra-override",
                                      "changed-predecessor", "incomplete-series"])
def test_windows15579_root_override_inventory_stays_authenticated(windows79, mutation):
    name = sorted(NEW_OVERRIDES)[0]
    directory = windows79 / selection.OVERRIDE155_ROOT
    override = directory / name
    if mutation == "missing-override":
        override.unlink()
    elif mutation == "changed-override":
        override.write_bytes(override.read_bytes() + b"\n")
    elif mutation == "extra-override":
        (directory / "unexpected.patch").write_bytes(b"unexpected")
    elif mutation == "changed-predecessor":
        predecessor = windows79 / "patches" / name
        predecessor.write_bytes(predecessor.read_bytes() + b"\n")
    else:
        series = windows79 / "patches/series"
        series.write_text(series.read_text().replace("patches/" + name + "\n", ""))
    with pytest.raises(selection.SelectionError):
        selection.select(windows79, "windows")
