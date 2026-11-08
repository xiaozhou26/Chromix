"""Strict synthetic schema cases and authenticated production 156 source replay."""
from __future__ import annotations

import copy
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from tools import patch_selection as selection
from tools.tests.test_patch_selection import clone_repo, set_platform_pins

ROOT = Path(__file__).resolve().parents[2]


def write_manifest(repo, manifest, monkeypatch):
    raw = (json.dumps(manifest, indent=2) + "\n").encode()
    (repo / selection.MANIFEST156).write_bytes(raw)
    monkeypatch.setattr(selection, "MANIFEST156_SHA256", selection.digest(raw))
    return raw


def manifest_fixture(repo):
    shared = repo / selection.ADDITIONS156_ROOT
    if shared.exists():
        shutil.rmtree(shared)
    series = (repo / "patches/series").read_bytes()
    names = [line.split("#", 1)[0].strip() for line in series.decode().splitlines()]
    base = [{"path": name, "sha256": selection.digest((repo / name).read_bytes())}
            for name in names if name]
    override_root = repo / selection.OVERRIDE156_ROOT
    if override_root.exists():
        shutil.rmtree(override_root)
    override_root.mkdir()
    shutil.copy2(ROOT / selection.REVIEW156, repo / selection.REVIEW156)
    overrides = []
    for original in base[:2]:
        name = selection.OVERRIDE156_ROOT + "/" + Path(original["path"]).name
        raw = (repo / original["path"]).read_bytes() + b"\n"
        (repo / name).write_bytes(raw)
        overrides.append({"path": name, "original_sha256": original["sha256"],
                          "sha256": selection.digest(raw)})
    platforms = {}
    for platform, commit in selection.PLATFORM156_COMMITS.items():
        directory = repo / (selection.OVERRIDE156_ROOT + "-" + platform)
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir()
        additions = []
        for number, feature in enumerate(("png", "controller", "timezone"), 225):
            path = f"{directory.relative_to(repo)}/{number:04d}-{feature}.patch"
            raw = f"synthetic {platform} {feature}\n".encode()
            (repo / path).write_bytes(raw)
            additions.append({"path": path, "sha256": selection.digest(raw)})
        platforms[platform] = {
            "version": selection.PLATFORM156_VERSIONS[platform], "commit": commit,
            "core_gitlink": selection.CORE156, "additions": additions,
        }
    return {
        "schema_version": 1, "kind": "chromium156-patch-selection",
        "chromium_version": selection.VERSION156, "core_commit": selection.CORE156,
        "base_series_sha256": selection.digest(series), "base_patches": base,
        "overrides": overrides, "platforms": platforms,
    }


@pytest.fixture(params=tuple(selection.PLATFORM156_COMMITS))
def repo156(tmp_path, monkeypatch, request):
    repo = clone_repo(tmp_path)
    platform = request.param
    set_platform_pins(repo, platform, selection.VERSION156, selection.CORE156,
                      selection.PLATFORM156_COMMITS[platform])
    manifest = manifest_fixture(repo)
    write_manifest(repo, manifest, monkeypatch)
    return repo, platform, manifest


def test_exact_156_order_identity_and_platform_additions(repo156):
    repo, platform, manifest = repo156
    identity, patches = selection.select(repo, platform)
    base = [entry["path"] for entry in manifest["base_patches"]]
    replacements = {"patches/" + Path(entry["path"]).name: entry["path"]
                    for entry in manifest["overrides"]}
    additions = manifest["platforms"][platform]["additions"]
    assert [name for name, _ in patches] == [replacements.get(name, name) for name in base] + [
        entry["path"] for entry in additions]
    assert len(patches) == 227
    assert identity["patches"] == [{"path": name, "sha256": selection.digest(raw)}
                                   for name, raw in patches]
    series = (repo / "patches/series").read_bytes()
    for original, replacement in replacements.items():
        series = series.replace(original.encode(), replacement.encode())
    series += "".join(entry["path"] + "\n" for entry in additions).encode()
    assert identity["series_sha256"] == selection.digest(series)
    chosen = identity["selection"]
    assert chosen["version"] == selection.VERSION156
    assert chosen["platform"] == platform
    assert chosen["core_commit"] == chosen["core_gitlink"] == selection.CORE156
    assert chosen["platform_commit"] == selection.PLATFORM156_COMMITS[platform]
    assert chosen["manifest"] == {
        "path": selection.MANIFEST156, "sha256": selection.MANIFEST156_SHA256}
    assert chosen["overrides"] == manifest["overrides"]
    assert chosen["additions"]["patches"] == additions
    assert chosen["base_series_sha256"] == manifest["base_series_sha256"]


@pytest.mark.parametrize("mutation", ["missing", "tampered", "symlink", "unpinned"])
def test_manifest_must_exist_and_match_exact_reviewed_bytes(repo156, monkeypatch, mutation):
    repo, platform, _ = repo156
    path = repo / selection.MANIFEST156
    if mutation == "missing":
        path.unlink()
    elif mutation == "tampered":
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "symlink":
        target = repo / "build/unreviewed.json"
        path.rename(target)
        path.symlink_to(target)
    else:
        monkeypatch.setattr(selection, "MANIFEST156_SHA256", None)
    with pytest.raises(selection.SelectionError, match="manifest|symlink|missing patch input"):
        selection.select(repo, platform)


@pytest.mark.parametrize("target", ["base", "override", "linux", "windows", "macos"])
@pytest.mark.parametrize("mutation", ["contents", "missing", "extra", "symlink", "directory"])
def test_all_patch_inventories_fail_closed(repo156, target, mutation):
    repo, platform, manifest = repo156
    entry = (manifest["base_patches"][0] if target == "base" else
             manifest["overrides"][0] if target == "override" else
             manifest["platforms"][target]["additions"][0])
    patch = repo / entry["path"]
    if mutation == "contents":
        patch.write_bytes(patch.read_bytes() + b"tampered\n")
    elif mutation == "missing":
        patch.unlink()
    elif mutation == "extra":
        (patch.parent / "9999-unreviewed.patch").write_bytes(b"extra")
    elif mutation == "symlink":
        target = repo / "linked-patch"
        patch.rename(target)
        patch.symlink_to(target)
    else:
        directory = patch.parent
        target = directory.with_name(directory.name + "-linked")
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(selection.SelectionError):
        selection.select(repo, platform)


@pytest.mark.parametrize("mutation", ["bytes", "order", "missing", "extra"])
def test_base_series_authentication(repo156, mutation):
    repo, platform, manifest = repo156
    path = repo / "patches/series"
    raw = path.read_bytes()
    if mutation == "bytes":
        path.write_bytes(raw + b"# changed\n")
    elif mutation == "order":
        first, second = [entry["path"].encode() for entry in manifest["base_patches"][:2]]
        path.write_bytes(raw.replace(first, b"TEMP").replace(second, first).replace(b"TEMP", second))
    elif mutation == "missing":
        path.write_bytes(raw.replace((manifest["base_patches"][0]["path"] + "\n").encode(), b""))
    else:
        path.write_bytes(raw + b"patches/9999-extra.patch\n")
    with pytest.raises(selection.SelectionError, match="base series"):
        selection.select(repo, platform)


@pytest.mark.parametrize("mutation", [
    "extra-field", "missing-field", "schema-bool", "schema-version", "wrong-kind",
    "wrong-version", "wrong-core", "wrong-platform", "wrong-platform-version", "gitlink",
    "missing-platform", "extra-platform", "base-count", "base-duplicate", "base-type",
    "bad-hash", "upper-hash", "override-predecessor", "override-duplicate", "override-unknown",
    "addition-duplicate", "addition-collision", "addition-type", "unsafe-path", "cross-platform-path",
])
def test_strict_schema_even_with_authenticated_manifest(repo156, monkeypatch, mutation):
    repo, platform, original = repo156
    manifest = copy.deepcopy(original)
    entry = manifest["platforms"][platform]
    if mutation == "extra-field":
        manifest["unexpected"] = []
    elif mutation == "missing-field":
        del manifest["overrides"]
    elif mutation == "schema-bool":
        manifest["schema_version"] = True
    elif mutation == "schema-version":
        manifest["schema_version"] = 2
    elif mutation == "wrong-kind":
        manifest["kind"] = "chromium155-patch-selection"
    elif mutation == "wrong-version":
        manifest["chromium_version"] = "156.0.8078.80"
    elif mutation == "wrong-core":
        manifest["core_commit"] = "0" * 40
    elif mutation == "wrong-platform":
        entry["commit"] = "0" * 40
    elif mutation == "wrong-platform-version":
        entry["version"] = selection.VERSION156 + "-2"
    elif mutation == "gitlink":
        entry["core_gitlink"] = selection.CORE155
    elif mutation == "missing-platform":
        del manifest["platforms"][platform]
    elif mutation == "extra-platform":
        manifest["platforms"]["android"] = entry
    elif mutation == "base-count":
        manifest["base_patches"].pop()
    elif mutation == "base-duplicate":
        manifest["base_patches"][1] = manifest["base_patches"][0]
    elif mutation == "base-type":
        manifest["base_patches"][0] = None
    elif mutation == "bad-hash":
        manifest["base_series_sha256"] = 123
    elif mutation == "upper-hash":
        manifest["base_series_sha256"] = "A" * 64
    elif mutation == "override-predecessor":
        manifest["overrides"][0]["original_sha256"] = "0" * 64
    elif mutation == "override-duplicate":
        manifest["overrides"].append(manifest["overrides"][0])
    elif mutation == "override-unknown":
        manifest["overrides"][0]["path"] = selection.OVERRIDE156_ROOT + "/9999-unknown.patch"
    elif mutation == "addition-duplicate":
        entry["additions"].append(entry["additions"][0])
    elif mutation == "addition-collision":
        entry["additions"][0]["path"] = (selection.OVERRIDE156_ROOT + "-" + platform + "/" +
                                         Path(manifest["base_patches"][0]["path"]).name)
    elif mutation == "addition-type":
        entry["additions"] = {}
    elif mutation == "unsafe-path":
        entry["additions"][0]["path"] = "patches/../9999-unreviewed.patch"
    else:
        other = next(target for target in selection.PLATFORM156_COMMITS if target != platform)
        entry["additions"][0]["path"] = manifest["platforms"][other]["additions"][0]["path"]
    with pytest.raises(selection.SelectionError):
        selection.validate_manifest156(manifest)
    write_manifest(repo, manifest, monkeypatch)
    with pytest.raises(selection.SelectionError):
        selection.select(repo, platform)


@pytest.mark.parametrize("raw", [b"{", b"\xff", b"[]", b"null",
                                   b'{"schema_version":1,"schema_version":1}'])
def test_malformed_or_duplicate_json_is_a_selection_error(repo156, monkeypatch, raw):
    repo, platform, _ = repo156
    (repo / selection.MANIFEST156).write_bytes(raw)
    monkeypatch.setattr(selection, "MANIFEST156_SHA256", selection.digest(raw))
    with pytest.raises(selection.SelectionError):
        selection.select(repo, platform)


@pytest.mark.parametrize("field,value", [
    ("core_commit", "0" * 40), ("platform_commit", "0" * 40),
    ("core_version", selection.VERSION156 + "-2"),
    ("platform_version", selection.VERSION156 + "-1.2"),
])
def test_exact_repository_pin_identity_is_required(repo156, field, value):
    repo, platform, _ = repo156
    prefix = selection.PLATFORMS[platform]
    key = {"core_commit": prefix + "UngoogledCommit", "core_version": prefix + "UngoogledVersion",
           "platform_commit": "Ungoogled" + prefix + "Commit",
           "platform_version": "Ungoogled" + prefix + "Version"}[field]
    path = repo / "build/ungoogled-revisions.psd1"
    path.write_text(re.sub(rf'(?m)^(  {key} = )"[^"]+"', rf'\g<1>"{value}"', path.read_text()))
    with pytest.raises(selection.SelectionError, match="pins|Ungoogled"):
        selection.select(repo, platform)


@pytest.mark.parametrize("which", ["src", "core"])
@pytest.mark.parametrize("value", [None, selection.VERSION155, "156.0.8078.80", selection.VERSION156])
def test_source_and_core_version_match_exactly(tmp_path, repo156, which, value):
    repo, platform, _ = repo156
    directory = tmp_path / which
    path = directory / ("chrome/VERSION" if which == "src" else "chromium_version.txt")
    path.parent.mkdir(parents=True)
    if value:
        text = ("".join(f"{key}={part}\n" for key, part in
                        zip(("MAJOR", "MINOR", "BUILD", "PATCH"), value.split(".")))
                if which == "src" else value + "\n")
        path.write_text(text)
    if value == selection.VERSION156:
        assert selection.select(repo, platform, **{which: directory})[0]["selection"]
    else:
        with pytest.raises(selection.SelectionError, match="version|missing patch input"):
            selection.select(repo, platform, **{which: directory})


@pytest.mark.parametrize("version", ["156.0.8078.78", "156.0.8078.80", "157.0.8078.79"])
def test_nearby_versions_are_not_accepted(repo156, version):
    repo, platform, _ = repo156
    set_platform_pins(repo, platform, version, selection.CORE156,
                      selection.PLATFORM156_COMMITS[platform])
    with pytest.raises(selection.SelectionError, match="unsupported Chromium patch version"):
        selection.select(repo, platform)


@pytest.mark.parametrize("platform", [None, "android"])
def test_explicit_supported_platform_required(repo156, platform):
    repo, _, _ = repo156
    path = repo / "build/ungoogled-revisions.psd1"
    text = path.read_text()
    for field, value in {"ChromiumVersion": selection.VERSION156,
                         "UngoogledVersion": selection.VERSION156 + "-1",
                         "UngoogledCommit": selection.CORE156}.items():
        text = re.sub(rf'(?m)^(  {field} = )"[^"]+"', rf'\g<1>"{value}"', text)
    path.write_text(text)
    (repo / "CHROMIUM_VERSION").write_text(selection.VERSION156 + "\n")
    with pytest.raises(selection.SelectionError, match="platform|require Windows"):
        selection.select(repo, platform)


def test_156_inputs_without_pins_cannot_fall_back_to_legacy(repo156):
    repo, platform, _ = repo156
    (repo / "build/ungoogled-revisions.psd1").unlink()
    with pytest.raises(selection.SelectionError, match="complete repository pins"):
        selection.select(repo, platform)


@pytest.mark.parametrize("version", sorted(selection.LEGACY_VERSIONS) + [selection.VERSION, selection.VERSION155])
@pytest.mark.parametrize("platform", ["linux", "windows", "macos"])
def test_older_versions_ignore_all_156_inputs(tmp_path, monkeypatch, version, platform):
    if version == selection.VERSION155 and platform == "macos":
        pytest.skip("155 macOS was not supported by the baseline")
    repo = clone_repo(tmp_path)
    if version == selection.VERSION155:
        core, commit = selection.CORE155, selection.PLATFORM155_COMMITS[platform]
    elif version == selection.VERSION:
        core = selection.MACOS154_CORE if platform == "macos" else selection.CORE
        commit = selection.PLATFORM_COMMITS[platform]
    elif version == selection.MACOS152_VERSION and platform == "macos":
        core, commit = selection.MACOS152_CORE, selection.MACOS152_PLATFORM
    else:
        core, commit = selection.CORE, selection.PLATFORM_COMMITS[platform]
    set_platform_pins(repo, platform, version, core, commit)
    before = selection.select(repo, platform)
    keys = {style: selection.preparation_key(repo, platform, style) for style in ("posix", "windows")}
    manifest_fixture(repo)
    (repo / selection.MANIFEST156).write_text("invalid manifest\n")
    monkeypatch.setattr(selection, "MANIFEST156_SHA256", "0" * 64)
    assert selection.select(repo, platform) == before
    for style, key in keys.items():
        assert selection.preparation_key(repo, platform, style) == key
    for target in (selection.OVERRIDE156_ROOT, *(selection.OVERRIDE156_ROOT + "-" + p
                                               for p in selection.PLATFORM156_COMMITS)):
        shutil.rmtree(repo / target)
    (repo / selection.MANIFEST156).unlink()
    assert selection.select(repo, platform) == before


@pytest.mark.parametrize("style", ["posix", "windows"])
def test_keys_bind_manifest_bytes_and_156_identity(repo156, monkeypatch, style):
    repo, platform, _ = repo156
    key = selection.preparation_key(repo, platform, style)
    path = repo / selection.MANIFEST156
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(selection.SelectionError, match="manifest"):
        selection.preparation_key(repo, platform, style)
    monkeypatch.setattr(selection, "MANIFEST156_SHA256", selection.digest(path.read_bytes()))
    assert selection.preparation_key(repo, platform, style) != key
    version, core = selection.VERSION, selection.MACOS154_CORE if platform == "macos" else selection.CORE
    set_platform_pins(repo, platform, version, core, selection.PLATFORM_COMMITS[platform])
    assert selection.preparation_key(repo, platform, style) != key


@pytest.mark.parametrize("present", [False, True])
def test_empty_reviewed_lists_do_not_require_git_empty_directories(repo156, monkeypatch, present):
    repo, platform, manifest = repo156
    manifest["overrides"] = []
    for entry in manifest["platforms"].values():
        entry["additions"] = []
    directories = [repo / selection.OVERRIDE156_ROOT, *(
        repo / (selection.OVERRIDE156_ROOT + "-" + target)
        for target in selection.PLATFORM156_COMMITS)]
    for directory in directories:
        shutil.rmtree(directory)
        if present:
            directory.mkdir()
    (repo / selection.OVERRIDE156_ROOT).mkdir(exist_ok=True)
    shutil.copy2(ROOT / selection.REVIEW156, repo / selection.REVIEW156)
    write_manifest(repo, manifest, monkeypatch)
    identity, patches = selection.select(repo, platform)
    assert len(patches) == 224
    assert identity["series_sha256"] == manifest["base_series_sha256"]
    assert identity["selection"]["overrides"] == []
    assert identity["selection"]["additions"]["patches"] == []
    directory = directories[-1]
    directory.mkdir(exist_ok=True)
    (directory / "9999-unlisted.patch").write_bytes(b"unreviewed")
    with pytest.raises(selection.SelectionError, match="inventory"):
        selection.select(repo, platform)


@pytest.mark.parametrize("new_input", [selection.MANIFEST156, selection.OVERRIDE156_ROOT,
                                        selection.ADDITIONS156_ROOT, *(
    selection.OVERRIDE156_ROOT + "-" + target for target in selection.PLATFORM156_COMMITS)])
def test_unpinned_root_only_repository_rejects_any_156_input(tmp_path, new_input):
    repo = tmp_path / "repo"
    (repo / "patches").mkdir(parents=True)
    (repo / "patches/series").write_text("patches/0001-test.patch\n")
    (repo / "patches/0001-test.patch").write_bytes(b"legacy")
    assert "selection" not in selection.select(repo)[0]
    path = repo / new_input
    if path.suffix == ".json":
        path.parent.mkdir()
        path.write_text("{}")
    else:
        path.mkdir()
    with pytest.raises(selection.SelectionError, match="complete repository pins"):
        selection.select(repo)


def test_only_fixed_reviewed_roots_are_selected(repo156):
    repo, platform, _ = repo156
    before = selection.select(repo, platform)
    directory = repo / "patches/chromium156-unreviewed"
    directory.mkdir()
    (directory / "9999-ignored.patch").write_bytes(b"unreviewed")
    assert selection.select(repo, platform) == before


@pytest.mark.parametrize("mutation", [None, "conflicting-hash", "extra", "missing", "contents"])
def test_shared_additions_have_exact_union_and_platform_order(repo156, monkeypatch, mutation):
    repo, platform, manifest = repo156
    directory = repo / selection.ADDITIONS156_ROOT
    directory.mkdir()
    paths = []
    for number in (230, 231):
        path = f"{selection.ADDITIONS156_ROOT}/{number:04d}-shared.patch"
        raw = f"synthetic shared addition {number}\n".encode()
        (repo / path).write_bytes(raw)
        paths.append({"path": path, "sha256": selection.digest(raw)})
    for target, entry in manifest["platforms"].items():
        entry["additions"] += paths if target == platform else list(reversed(paths))
    write_manifest(repo, manifest, monkeypatch)
    identity, patches = selection.select(repo, platform)
    assert identity["patches"][-2:] == paths
    assert [name for name, _ in patches][-2:] == [entry["path"] for entry in paths]
    if mutation is None:
        return
    if mutation == "conflicting-hash":
        other = next(target for target in selection.PLATFORM156_COMMITS if target != platform)
        manifest["platforms"][other]["additions"][-1] = dict(paths[0], sha256="0" * 64)
        write_manifest(repo, manifest, monkeypatch)
    elif mutation == "extra":
        (directory / "9999-unlisted.patch").write_bytes(b"unreviewed")
    elif mutation == "missing":
        (repo / paths[0]["path"]).unlink()
    else:
        (repo / paths[0]["path"]).write_bytes(b"tampered")
    with pytest.raises(selection.SelectionError):
        selection.select(repo, platform)


def test_cli_cannot_use_test_only_manifest_trust(repo156):
    repo, platform, _ = repo156
    result = subprocess.run([sys.executable, str(ROOT / "tools/patch_selection.py"),
                             "--repo", str(repo), "--platform", platform, "--json"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert not result.stdout
    assert "manifest" in result.stderr


@pytest.fixture(params=tuple(selection.PLATFORM156_COMMITS))
def real_repo156(tmp_path, request):
    repo = clone_repo(tmp_path)
    shutil.copy2(ROOT / selection.MANIFEST156, repo / selection.MANIFEST156)
    platform = request.param
    set_platform_pins(repo, platform, selection.VERSION156, selection.CORE156,
                      selection.PLATFORM156_COMMITS[platform])
    return repo, platform


@pytest.mark.parametrize("platform,count", [("linux", 226), ("windows", 232), ("macos", 226)])
def test_integrated_repository_selects_real_156_stack(platform, count):
    identity, patches = selection.select(ROOT, platform)
    assert identity["selection"]["version"] == selection.VERSION156
    assert len(patches) == count
    assert len([name for name, _ in patches if name.startswith(selection.OVERRIDE156_ROOT + "/")]) == 23
    assert identity["selection"]["manifest"]["sha256"] == selection.MANIFEST156_SHA256
    assert identity["selection"]["review_manifest"]["sha256"] == selection.REVIEW156_SHA256


def test_production_manifest_matches_review_and_root224_evidence(real_repo156):
    repo, platform = real_repo156
    raw = (repo / selection.MANIFEST156).read_bytes()
    assert selection.digest(raw) == selection.MANIFEST156_SHA256
    manifest = selection.validate_manifest156(json.loads(raw))
    raw_review = (repo / selection.REVIEW156).read_bytes()
    assert selection.digest(raw_review) == selection.REVIEW156_SHA256
    review = json.loads(raw_review)
    root_report = json.loads((repo / "patches/chromium156-verification/root224-report.json").read_bytes())
    assert manifest["base_patches"] == [
        {"path": entry["base_patch"], "sha256": entry["sha256"]} for entry in root_report["patches"]]
    assert manifest["base_series_sha256"] == review["base_series_sha256"] == (
        "71613faf45339ae3debd3dcf8afb95101989c4e6537d14dc7b0cebd1766f5863")
    assert manifest["overrides"] == [
        {key: entry[key] for key in ("path", "original_sha256", "sha256")}
        for entry in review["overrides"]]
    assert len(manifest["overrides"]) == 23
    for target, entry in manifest["platforms"].items():
        assert entry["commit"] == review["platform_commits"][target]
        assert entry["core_gitlink"] == review["core_commit"] == selection.CORE156
        assert entry["additions"] == review["additions"][target]
    identity, patches = selection.select(repo, platform)
    assert len(patches) == (232 if platform == "windows" else 226)
    assert identity["selection"]["review_manifest"] == {
        "path": selection.REVIEW156, "sha256": selection.REVIEW156_SHA256}
    assert selection.REVIEW156 not in dict(patches)
    assert identity["selection"]["additions"]["patches"] == review["additions"][platform]
    result = subprocess.run([sys.executable, str(ROOT / "tools/patch_selection.py"),
                             "--repo", str(repo), "--platform", platform, "--json"],
                            check=True, capture_output=True, text=True, timeout=30)
    assert json.loads(result.stdout) == identity


@pytest.mark.parametrize("mutation", ["missing", "contents", "symlink", "extra-json", "renamed"])
def test_review_evidence_is_required_and_never_a_wildcard_exception(real_repo156, mutation):
    repo, platform = real_repo156
    path = repo / selection.REVIEW156
    if mutation == "missing":
        path.unlink()
    elif mutation == "contents":
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "symlink":
        path.unlink()
        path.symlink_to(ROOT / selection.REVIEW156)
    elif mutation == "extra-json":
        (path.parent / "unreviewed.json").write_text("{}")
    else:
        path.rename(path.with_name("alternate-manifest.json"))
    with pytest.raises(selection.SelectionError):
        selection.select(repo, platform)


@pytest.mark.parametrize("platform", tuple(selection.PLATFORM156_COMMITS))
def test_production_156_manifest_does_not_mix_into_154(tmp_path, platform):
    repo = clone_repo(tmp_path)
    core = selection.MACOS154_CORE if platform == "macos" else selection.CORE
    set_platform_pins(repo, platform, selection.VERSION, core, selection.PLATFORM_COMMITS[platform])
    before = selection.select(repo, platform)
    keys = {style: selection.preparation_key(repo, platform, style) for style in ("posix", "windows")}
    shutil.copy2(ROOT / selection.MANIFEST156, repo / selection.MANIFEST156)
    (repo / selection.REVIEW156).write_bytes(b"tampered evidence\n")
    assert selection.select(repo, platform) == before
    for style, key in keys.items():
        assert selection.preparation_key(repo, platform, style) == key
    assert all("chromium156" not in name for name, _ in before[1])


@pytest.fixture(scope="module")
def source_replay156():
    spec = importlib.util.spec_from_file_location(
        "chromium156_review_verify", ROOT / "patches/chromium156-verification/verify.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    raw = (ROOT / selection.REVIEW156).read_bytes()
    assert selection.digest(raw) == selection.REVIEW156_SHA256
    review = json.loads(raw)
    fixture = json.loads(gzip.decompress(verifier.checked(review["fixture"])))
    assert fixture["source"]["version"] == selection.VERSION156
    assert fixture["source"]["core_commit"] == selection.CORE156
    assert fixture["source"]["chromium_commit"] == review["chromium_commit"]
    assert not fixture["source"]["errors"]
    for name, entry in fixture["source"]["files"].items():
        data = fixture["files"][name].encode()
        assert selection.digest(data) == entry["sha256"], name
        assert hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == entry["git_blob"]
    return verifier, fixture


@pytest.mark.parametrize("substituted", [False, True])
def test_production_selected_full_source_fixture_roundtrip(tmp_path, real_repo156, source_replay156, substituted):
    repo, platform = real_repo156
    verifier, fixture = source_replay156
    src, core, tooling = (tmp_path / name for name in ("src", "core", "tooling"))
    for directory in (src, core, tooling):
        directory.mkdir()
    (core / "chromium_version.txt").write_text(selection.VERSION156 + "\n")
    (core / "domain_regex.list").write_text(fixture["domain_regex"])
    listed_text = fixture["domain_lists"][platform]
    (core / "domain_substitution.list").write_text(listed_text)
    (tooling / "domain_substitution.list").write_text(listed_text)
    for name, text in fixture["files"].items():
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CEILING_DIRECTORIES=str(tmp_path))
    for entry in fixture["predecessors"]["core"] + fixture["predecessors"][platform]:
        data = entry["text"].encode()
        assert selection.digest(data) == entry["sha256"]
        verifier.apply(src, data, env, check=True)
        verifier.apply(src, data, env)
    arp = verifier.arp
    listed = set(listed_text.splitlines()) if substituted else set()
    rules = arp._rules(fixture["domain_regex"].encode()) if substituted else []
    for name in listed:
        path = src / name
        if path.is_file():
            path.write_text(arp._substitute(path.read_text(), rules))
    if substituted:
        (src / ".chromix-domain-substituted").write_text(selection.CORE156 + "\n")
    identity, patches = selection.select(repo, platform, src=src, core=core)
    expected_count = 232 if platform == "windows" else 226
    assert len(patches) == expected_count
    for _, raw in patches:
        data, _ = arp.transform_patch(raw, listed, rules)
        verifier.apply(src, data, env, check=True)
        verifier.apply(src, data, env)
    report = verifier.verify_patch_stack.verify(src, repo, core=core, tooling=tooling, platform=platform)
    assert report["status"] == "verified"
    assert report["patch_count"] == expected_count
    assert report["domain_substituted"] == substituted
    assert report["identity"]["selection"] == identity["selection"]
    selected_identity = report["identity"]["series"] if substituted else report["identity"]
    assert selected_identity["patches"] == identity["patches"]
    controller = (src / "chrome/browser/enterprise/BUILD.gn").read_text()
    assert controller.count('"signin/managed_profile_creation_controller.cc"') == 1
    png = (src / "third_party/blink/renderer/core/html/canvas/canvas_async_blob_creator.cc").read_text()
    assert "mime_type_ == kMimeTypePng &&" in png
    if platform == "windows":
        timezone = (src / "third_party/blink/renderer/core/timezone/timezone_controller.cc").read_text()
        assert "bool TimeZoneController::SetDefaultTimeZone(" in timezone
        renderer = (src / "content/renderer/render_thread_impl.cc").read_text()
        assert "CHECK(blink::SetRendererDefaultTimeZone(blink::WebString::FromUtf8(timezone)))" in renderer
