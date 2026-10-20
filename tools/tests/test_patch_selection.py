from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from tools import patch_selection as selection

ROOT = Path(__file__).resolve().parents[2]


def clone_repo(tmp_path: Path, *, historical: bool = False, additions: bool = True) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "patches", repo / "patches")
    (repo / "build").mkdir()
    # Historical fixtures omit both inputs for each platform-specific addition.
    for directory_name, manifest in (
        (selection.LINUX155_ADDITIONS_ROOT, selection.LINUX155_ADDITIONS_MANIFEST),
        (selection.MACOS154_ADDITIONS_ROOT, selection.MACOS154_ADDITIONS_MANIFEST),
        (selection.MACOS155_ADDITIONS_ROOT, selection.MACOS155_ADDITIONS_MANIFEST),
        (selection.WINDOWS155_ADDITIONS_ROOT, selection.WINDOWS155_ADDITIONS_MANIFEST),
    ):
        directory = repo / directory_name
        if not additions and directory.exists():
            shutil.rmtree(directory)
        if additions and (ROOT / manifest).is_file():
            shutil.copy2(ROOT / manifest, repo / manifest)
    for name in ("build/ungoogled-revisions.psd1", "CHROMIUM_VERSION",
                 "CHROMIUM_LINUX_VERSION", "CHROMIUM_WINDOWS_VERSION",
                 "CHROMIUM_MACOS_VERSION"):
        source = ROOT / name
        if source.is_file():
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for name in ("build/prepare-ungoogled.sh", "build/apply-patches.sh"):
        shutil.copy2(ROOT / name, repo / name)
    shutil.copytree(ROOT / "build/windows/lite-tarball-files",
                    repo / "build/windows/lite-tarball-files")
    if historical:
        for platform in ("linux", "windows"):
            set_platform_pins(repo, platform, selection.VERSION, selection.CORE,
                              selection.PLATFORM_COMMITS[platform])
    return repo


def set_platform_pins(repo: Path, platform: str, version: str, core: str,
                      commit: str) -> None:
    prefix = selection.PLATFORMS[platform]
    values = {
        prefix + "ChromiumVersion": version,
        prefix + "UngoogledVersion": version + "-1",
        prefix + "UngoogledCommit": core,
        "Ungoogled" + prefix + "Version": version + ("-1.1" if platform != "linux" else "-1"),
        "Ungoogled" + prefix + "Commit": commit,
    }
    path = repo / "build/ungoogled-revisions.psd1"
    text = path.read_text()
    for field, value in values.items():
        text, count = re.subn(rf'(?m)^(  {field} = )"[^"]+"',
                              rf'\g<1>"{value}"', text)
        assert count == 1
    path.write_text(text)
    (repo / ("CHROMIUM_" + prefix.upper() + "_VERSION")).write_text(version + "\n")


def set_macos_pins(repo: Path, *, historical: bool = False) -> None:
    if historical:
        version = "152.0.7977.82"
        core = "e71b91c6e336d0f25cfc6b9ef09298a9d2506e24"
        platform = "038db2b41f7aeb00bbceb2f5a56912b26eb5b284"
    else:
        for target in ("linux", "windows"):
            set_platform_pins(repo, target, selection.VERSION, selection.CORE,
                              selection.PLATFORM_COMMITS[target])
        version = "154.0.8037.97"
        core = "3e46b13825f808f0886e484d44532372655e5fe4"
        platform = "f7ba75f94442abda7ac3ea81790c217f8636d3ba"
    values = {
        "MacOSChromiumVersion": version,
        "MacOSUngoogledVersion": version + "-1",
        "MacOSUngoogledCommit": core,
        "UngoogledMacOSVersion": version + "-1.1",
        "UngoogledMacOSCommit": platform,
    }
    path = repo / "build/ungoogled-revisions.psd1"
    text = path.read_text()
    for field, value in values.items():
        text, count = re.subn(rf'(?m)^(  {field} = )"[^"]+"',
                              rf'\g<1>"{value}"', text)
        assert count == 1
    path.write_text(text)
    (repo / "CHROMIUM_MACOS_VERSION").write_text(version + "\n")


def set_linux_version(repo: Path, version: str) -> None:
    set_platform_pins(repo, "linux", version, selection.CORE,
                      selection.PLATFORM_COMMITS["linux"])


def test_exact_154_selects_reviewed_override_bytes_for_linux_and_windows(tmp_path):
    repo = clone_repo(tmp_path, historical=True)
    linux_identity, linux_patches = selection.select(repo, "linux")
    windows_identity, windows_patches = selection.select(repo, "windows")
    assert len(linux_patches) == len(windows_patches) == 224
    assert linux_identity["selection"] == {
        "schema_version": 1,
        "version": selection.VERSION,
        "platform": "linux",
        "core_commit": selection.CORE,
        "platform_commit": selection.PLATFORM_COMMITS["linux"],
        "base_series_sha256": linux_identity["selection"]["base_series_sha256"],
    }
    assert windows_identity["selection"]["platform"] == "windows"
    for identity, patches in ((linux_identity, linux_patches), (windows_identity, windows_patches)):
        selected = {entry["path"] for entry in identity["patches"]}
        assert all(path.startswith("patches/chromium154/") or path.startswith("patches/")
                   for path in selected)
        assert len(selected & {"patches/chromium154/" + name for name in selection.OVERRIDES}) == 7
        assert [entry["sha256"] for entry in identity["patches"]] == [
            selection.digest(data) for _, data in patches]


def test_historical153_keeps_root_patch_identity(tmp_path):
    repo = clone_repo(tmp_path, historical=True)
    set_linux_version(repo, "153.0.8010.36")
    identity, patches = selection.select(repo, "linux")
    assert "selection" not in identity
    assert all(path.startswith("patches/") for path, _ in patches)


def test_platform_swap_changes_selection_identity_and_preparation_key(tmp_path):
    repo = clone_repo(tmp_path, historical=True)
    linux_identity, _ = selection.select(repo, "linux")
    windows_identity, _ = selection.select(repo, "windows")
    assert linux_identity != windows_identity
    assert linux_identity["selection"]["platform"] != windows_identity["selection"]["platform"]
    assert selection.preparation_key(repo, "linux", "posix") != selection.preparation_key(repo, "windows", "posix")
    assert selection.preparation_key(repo, "linux", "windows") != selection.preparation_key(repo, "windows", "windows")


@pytest.mark.parametrize("mutation", ["missing", "tampered", "extra"])
def test_override_inventory_and_bytes_fail_closed(tmp_path, mutation):
    repo = clone_repo(tmp_path, historical=True)
    directory = repo / selection.OVERRIDE_ROOT
    if mutation == "missing":
        (directory / next(iter(selection.OVERRIDES))).unlink()
    elif mutation == "tampered":
        path = directory / next(iter(selection.OVERRIDES))
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        (directory / "9999-extra.patch").write_bytes(b"unexpected")
    with pytest.raises(selection.SelectionError, match="partial|changed|override"):
        selection.select(repo, "linux")


def test_wrong_154_platform_and_core_pin_fail_closed(tmp_path):
    repo = clone_repo(tmp_path, historical=True)
    set_macos_pins(repo)
    path = repo / "build/ungoogled-revisions.psd1"
    path.write_text(path.read_text().replace(
        'UngoogledMacOSCommit = "f7ba75f94442abda7ac3ea81790c217f8636d3ba"',
        'UngoogledMacOSCommit = "' + selection.PLATFORM_COMMITS["linux"] + '"'))
    with pytest.raises(selection.SelectionError, match="core/platform pins"):
        selection.select(repo, "macos")

    repo = clone_repo(tmp_path / "core", historical=True)
    text = (repo / "build/ungoogled-revisions.psd1").read_text()
    text = text.replace('LinuxUngoogledCommit = "37085e47cf580c815a30402917d350ce97399ded"',
                        'LinuxUngoogledCommit = "' + "0" * 40 + '"')
    (repo / "build/ungoogled-revisions.psd1").write_text(text)
    with pytest.raises(selection.SelectionError, match="core/platform pins"):
        selection.select(repo, "linux")


def test_unknown_version_partial_pin_and_source_mismatch_fail_closed(tmp_path):
    repo = clone_repo(tmp_path, historical=True)
    set_linux_version(repo, "155.0.1.2")
    with pytest.raises(selection.SelectionError, match="unsupported Chromium patch version"):
        selection.select(repo, "linux")

    repo = clone_repo(tmp_path / "partial", historical=True)
    text = (repo / "build/ungoogled-revisions.psd1").read_text()
    text = text.replace('  LinuxChromiumVersion = "154.0.8037.97"\n', "")
    (repo / "build/ungoogled-revisions.psd1").write_text(text)
    with pytest.raises(selection.SelectionError, match="overrides"):
        selection.select(repo, "linux")

    repo = clone_repo(tmp_path / "source", historical=True)
    src = tmp_path / "src/chrome"
    src.mkdir(parents=True)
    (src / "VERSION").write_text("MAJOR=153\nMINOR=0\nBUILD=8010\nPATCH=36\n")
    with pytest.raises(selection.SelectionError, match="source version"):
        selection.select(repo, "linux", src=src.parent)


def test_override_symlink_is_rejected(tmp_path):
    repo = clone_repo(tmp_path, historical=True)
    name = next(iter(selection.OVERRIDES))
    path = repo / selection.OVERRIDE_ROOT / name
    path.unlink()
    path.symlink_to(repo / "patches" / name)
    with pytest.raises(selection.SelectionError, match="symlink|changed"):
        selection.select(repo, "linux")
