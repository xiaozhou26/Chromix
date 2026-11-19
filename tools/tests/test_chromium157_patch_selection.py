"""Real 157 patch identities, strict rejection, and historical selection isolation."""
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
MANIFEST = json.loads((ROOT / selection.MANIFEST157).read_bytes())


@pytest.fixture(params=("windows", "macos"))
def repo157(tmp_path, request):
    repo = clone_repo(tmp_path)
    shutil.copy2(ROOT / selection.MANIFEST157, repo / selection.MANIFEST157)
    platform = request.param
    set_platform_pins(repo, platform, selection.VERSION157, selection.CORE157,
                      selection.PLATFORM157_COMMITS[platform])
    return repo, platform


def test_reviewed_manifest_and_actual_patch_hashes(repo157):
    repo, platform = repo157
    assert selection.digest((repo / selection.MANIFEST157).read_bytes()) == selection.MANIFEST157_SHA256
    assert selection.digest((repo / selection.REVIEW157).read_bytes()) == selection.REVIEW157_SHA256
    assert selection.validate_manifest157(MANIFEST) == MANIFEST
    review = json.loads((repo / selection.REVIEW157).read_bytes())
    old = json.loads((ROOT / selection.MANIFEST156).read_bytes())
    assert MANIFEST["base_patches"] == review["base_patches"] == old["base_patches"]
    assert MANIFEST["base_series_sha256"] == old["base_series_sha256"]
    assert len(MANIFEST["overrides"]) == 31
    assert MANIFEST["overrides"] == [
        {key: entry[key] for key in ("path", "original_sha256", "sha256")}
        for entry in review["overrides"]]
    for target, entry in MANIFEST["platforms"].items():
        assert {key: entry[key] for key in ("version", "commit", "core_gitlink")} == {
            key: review["platform_identities"][target][key]
            for key in ("version", "commit", "core_gitlink")}
        assert entry["additions"] == [
            {key: patch[key] for key in ("path", "sha256")} for patch in review["additions"][target]]
        assert len(entry["additions"]) + 224 == selection.PATCH_COUNTS157[target]
        for patch in entry["additions"]:
            assert selection.digest((repo / patch["path"]).read_bytes()) == patch["sha256"]
    identity, patches = selection.select(repo, platform)
    replacements = {"patches/" + Path(entry["path"]).name: entry["path"]
                    for entry in MANIFEST["overrides"]}
    additions = MANIFEST["platforms"][platform]["additions"]
    expected = [replacements.get(entry["path"], entry["path"]) for entry in MANIFEST["base_patches"]]
    assert [name for name, _ in patches] == expected + [entry["path"] for entry in additions]
    assert len(patches) == selection.PATCH_COUNTS157[platform]
    assert identity["patches"] == [{"path": name, "sha256": selection.digest(raw)} for name, raw in patches]
    chosen = identity["selection"]
    assert chosen["version"] == selection.VERSION157
    assert chosen["core_commit"] == chosen["core_gitlink"] == selection.CORE157
    assert chosen["platform_commit"] == selection.PLATFORM157_COMMITS[platform]
    assert chosen["manifest"] == {"path": selection.MANIFEST157, "sha256": selection.MANIFEST157_SHA256}
    assert chosen["review_manifest"] == {"path": selection.REVIEW157, "sha256": selection.REVIEW157_SHA256}
    assert chosen["selection_status"] == "reviewed-patches"
    assert chosen["additions"]["patches"] == additions
    series = (repo / "patches/series").read_bytes()
    for original, replacement in replacements.items():
        series = series.replace(original.encode(), replacement.encode())
    series += "".join(entry["path"] + "\n" for entry in additions).encode()
    assert identity["series_sha256"] == selection.digest(series)
    result = subprocess.run([sys.executable, str(ROOT / "tools/patch_selection.py"),
                             "--repo", str(repo), "--platform", platform, "--json"],
                            check=True, capture_output=True, text=True, timeout=30)
    assert json.loads(result.stdout) == identity


@pytest.mark.parametrize("target", ["base", "override", "addition", "unselected-addition", "manifest", "review"])
@pytest.mark.parametrize("mutation", ["contents", "missing", "extra", "symlink", "linked-directory"])
def test_authenticated_inputs_and_inventory_fail_closed(repo157, target, mutation):
    repo, platform = repo157
    name = {
        "base": MANIFEST["base_patches"][0]["path"],
        "override": MANIFEST["overrides"][0]["path"],
        "addition": MANIFEST["platforms"][platform]["additions"][0]["path"],
        "unselected-addition": MANIFEST["platforms"]["macos" if platform == "windows" else "windows"]["additions"][-1]["path"],
        "manifest": selection.MANIFEST157, "review": selection.REVIEW157,
    }[target]
    path = repo / name
    if mutation == "contents":
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "missing":
        path.unlink()
    elif mutation == "extra":
        if target == "manifest":
            manifest = copy.deepcopy(MANIFEST)
            manifest["extra"] = True
            path.write_text(json.dumps(manifest))
        else:
            (path.parent / ("extra.json" if target == "review" else "9999-extra.patch")).write_text("unreviewed")
    elif mutation == "symlink":
        path.unlink()
        path.symlink_to(ROOT / name)
    else:
        directory = path.parent
        moved = directory.with_name(directory.name + "-linked")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(selection.SelectionError):
        selection.select(repo, platform)


@pytest.mark.parametrize("mutation", [
    "extra-field", "schema-bool", "wrong-version", "wrong-core", "wrong-platform", "gitlink",
    "fake-linux-gitlink", "unblock-linux", "extra-platform", "base-count", "base-duplicate",
    "bad-hash", "override-predecessor", "override-duplicate", "addition-duplicate",
    "addition-collision", "addition-type", "unsafe-path", "156-path", "addition-missing",
])
def test_strict_schema_rejects_unreviewed_identity_and_additions(mutation):
    manifest = copy.deepcopy(MANIFEST)
    entry = manifest["platforms"]["windows"]
    if mutation == "extra-field":
        manifest["extra"] = True
    elif mutation == "schema-bool":
        manifest["schema_version"] = True
    elif mutation == "wrong-version":
        manifest["chromium_version"] = "157.0.8103.79"
    elif mutation == "wrong-core":
        manifest["core_commit"] = selection.CORE156
    elif mutation == "wrong-platform":
        entry["commit"] = selection.PLATFORM156_COMMITS["windows"]
    elif mutation == "gitlink":
        entry["core_gitlink"] = selection.CORE156
    elif mutation == "fake-linux-gitlink":
        manifest["platforms"]["linux"]["core_gitlink"] = selection.CORE157
    elif mutation == "unblock-linux":
        manifest["platforms"]["linux"]["selection_status"] = "reviewed-patches"
    elif mutation == "extra-platform":
        manifest["platforms"]["android"] = entry
    elif mutation == "base-count":
        manifest["base_patches"].pop()
    elif mutation == "base-duplicate":
        manifest["base_patches"][1] = manifest["base_patches"][0]
    elif mutation == "bad-hash":
        manifest["base_series_sha256"] = "A" * 64
    elif mutation == "override-predecessor":
        manifest["overrides"][0]["original_sha256"] = "0" * 64
    elif mutation == "override-duplicate":
        manifest["overrides"].append(manifest["overrides"][0])
    elif mutation == "addition-duplicate":
        entry["additions"][1] = entry["additions"][0]
    elif mutation == "addition-collision":
        entry["additions"][0]["path"] = selection.ADDITIONS157_ROOT + "/" + Path(manifest["base_patches"][0]["path"]).name
    elif mutation == "addition-type":
        entry["additions"] = {}
    elif mutation == "unsafe-path":
        entry["additions"][0]["path"] = "patches/../9999-unsafe.patch"
    elif mutation == "156-path":
        entry["additions"][0]["path"] = entry["additions"][0]["path"].replace("157", "156")
    else:
        entry["additions"].pop()
    with pytest.raises(selection.SelectionError):
        selection.validate_manifest157(manifest)


@pytest.mark.parametrize("mutation", ["addition-order", "addition-swap", "override-hash", "shared-hash", "gitlink"])
def test_even_reauthenticated_manifest_cannot_disagree_with_review(repo157, monkeypatch, mutation):
    repo, platform = repo157
    manifest = copy.deepcopy(MANIFEST)
    entry = manifest["platforms"][platform]
    if mutation == "addition-order":
        entry["additions"].reverse()
    elif mutation == "addition-swap":
        other = "macos" if platform == "windows" else "windows"
        selected = {patch["path"] for patch in entry["additions"]}
        replacement = next(patch for patch in manifest["platforms"][other]["additions"]
                           if patch["path"] not in selected)
        entry["additions"][-1] = copy.deepcopy(replacement)
    elif mutation == "override-hash":
        patch = manifest["overrides"][0]
        path = repo / patch["path"]
        path.write_bytes(path.read_bytes() + b"\n")
        patch["sha256"] = selection.digest(path.read_bytes())
    elif mutation == "shared-hash":
        entry["additions"][0]["sha256"] = "0" * 64
    else:
        entry["core_gitlink"] = selection.CORE156
    raw = json.dumps(manifest).encode()
    (repo / selection.MANIFEST157).write_bytes(raw)
    monkeypatch.setattr(selection, "MANIFEST157_SHA256", selection.digest(raw))
    with pytest.raises(selection.SelectionError):
        selection.select(repo, platform)


@pytest.mark.parametrize("raw", [b"{", b"\xff", b"[]", b"null", b'{"schema_version":1,"schema_version":1}'])
def test_malformed_or_duplicate_json_rejected(repo157, monkeypatch, raw):
    repo, platform = repo157
    (repo / selection.MANIFEST157).write_bytes(raw)
    monkeypatch.setattr(selection, "MANIFEST157_SHA256", selection.digest(raw))
    with pytest.raises(selection.SelectionError):
        selection.select(repo, platform)


@pytest.mark.parametrize("version", ["157.0.8103.77", "157.0.8103.79", "158.0.8103.78"])
def test_unknown_versions_never_fall_back(repo157, version):
    repo, platform = repo157
    set_platform_pins(repo, platform, version, selection.CORE157, selection.PLATFORM157_COMMITS[platform])
    with pytest.raises(selection.SelectionError, match="unsupported Chromium patch version"):
        selection.select(repo, platform)


@pytest.mark.parametrize("field", ["core", "platform", "core-version", "platform-version"])
def test_wrong_repository_pins_rejected(repo157, field):
    repo, platform = repo157
    prefix = selection.PLATFORMS[platform]
    key = {"core": prefix + "UngoogledCommit", "platform": "Ungoogled" + prefix + "Commit",
           "core-version": prefix + "UngoogledVersion", "platform-version": "Ungoogled" + prefix + "Version"}[field]
    value = selection.VERSION157 + "-2" if field.endswith("version") else "0" * 40
    path = repo / "build/ungoogled-revisions.psd1"
    path.write_text(re.sub(rf'(?m)^(  {key} = )"[^"]+"', rf'\g<1>"{value}"', path.read_text()))
    with pytest.raises(selection.SelectionError):
        selection.select(repo, platform)


@pytest.mark.parametrize("which", ["src", "core"])
@pytest.mark.parametrize("version", [None, selection.VERSION156, selection.VERSION157])
def test_source_and_core_version_are_exact(repo157, tmp_path, which, version):
    repo, platform = repo157
    directory = tmp_path / which
    path = directory / ("chrome/VERSION" if which == "src" else "chromium_version.txt")
    path.parent.mkdir(parents=True)
    if version:
        path.write_text("".join(f"{key}={part}\n" for key, part in
                               zip(("MAJOR", "MINOR", "BUILD", "PATCH"), version.split(".")))
                        if which == "src" else version + "\n")
    if version == selection.VERSION157:
        assert selection.select(repo, platform, **{which: directory})[0]["selection"]
    else:
        with pytest.raises(selection.SelectionError):
            selection.select(repo, platform, **{which: directory})


def test_linux_records_real_156_gitlink_but_fails_closed(repo157, monkeypatch):
    repo, _ = repo157
    linux = MANIFEST["platforms"]["linux"]
    assert linux["version"] == selection.VERSION156 + "-1"
    assert linux["commit"] == selection.PLATFORM156_COMMITS["linux"]
    assert linux["core_gitlink"] == selection.CORE156 != selection.CORE157
    assert len(linux["additions"]) + 224 == 228
    set_platform_pins(repo, "linux", selection.VERSION157, selection.CORE157, linux["commit"])
    historical = subprocess.check_output(["git", "-C", str(ROOT), "show",
        "63dd0fc5934b7c27d4a1e3439a024a96ad96bba0:" + selection.REVIEW157])
    (repo / selection.REVIEW157).write_bytes(historical)
    monkeypatch.setattr(selection, "REVIEW157_SHA256", selection.digest(historical))
    monkeypatch.setattr(selection, "LINUX157_VALIDATED_REVIEW_SHA256", None)
    for name in ("build/linux/157.0.8103.78/source.json",):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, path)
    shutil.copytree(ROOT / "build/linux/157.0.8103.78/patches", repo / "build/linux/157.0.8103.78/patches")
    pins = repo / "build/ungoogled-revisions.psd1"
    pins.write_text(pins.read_text().replace('UngoogledLinuxVersion = "157.0.8103.78-1"',
                                             'UngoogledLinuxVersion = "156.0.8078.79-1"'))
    with pytest.raises(selection.SelectionError, match="Linux blocked"):
        selection.select(repo, "linux")
    with pytest.raises(selection.SelectionError, match="Linux blocked"):
        selection.select157(repo, "linux", b"", [])


@pytest.mark.parametrize("mutation", ["bytes", "order", "missing", "extra"])
def test_base_series_cannot_change(repo157, mutation):
    repo, platform = repo157
    path = repo / "patches/series"
    lines = path.read_bytes().splitlines(keepends=True)
    if mutation == "bytes":
        lines.append(b"# changed\n")
    elif mutation == "order":
        lines.reverse()
    elif mutation == "missing":
        lines.pop()
    else:
        lines.append(b"patches/9999-extra.patch\n")
    path.write_bytes(b"".join(lines))
    with pytest.raises(selection.SelectionError):
        selection.select(repo, platform)


@pytest.mark.parametrize("style", ["posix", "windows"])
def test_preparation_keys_bind_157_manifest_and_platform(repo157, monkeypatch, style):
    repo, platform = repo157
    key = selection.preparation_key(repo, platform, style)
    other = "macos" if platform == "windows" else "windows"
    set_platform_pins(repo, other, selection.VERSION157, selection.CORE157, selection.PLATFORM157_COMMITS[other])
    assert selection.preparation_key(repo, other, style) != key
    path = repo / selection.MANIFEST157
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(selection.SelectionError):
        selection.preparation_key(repo, platform, style)
    monkeypatch.setattr(selection, "MANIFEST157_SHA256", selection.digest(path.read_bytes()))
    assert selection.preparation_key(repo, platform, style) != key
    result = subprocess.run([sys.executable, str(ROOT / "tools/patch_selection.py"),
                             "--repo", str(repo), "--platform", platform, "--json"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert not result.stdout
    assert "selection manifest" in result.stderr


@pytest.mark.parametrize("platform", ["linux", "windows", "macos"])
def test_156_identity_and_keys_ignore_all_157_inputs(tmp_path, platform):
    repo = clone_repo(tmp_path)
    shutil.copy2(ROOT / selection.MANIFEST156, repo / selection.MANIFEST156)
    set_platform_pins(repo, platform, selection.VERSION156, selection.CORE156,
                      selection.PLATFORM156_COMMITS[platform])
    before = selection.select(repo, platform)
    historical_identities = {
        "windows": "d0844b60fa3acd6623e5a55979d4f1f2120c1bddb0010ec872fca19e171464ff",
        "macos": "2977f93643c32c40397a5175e6eedb7b1b497fa25db074ef82361c070f5aae04",
        "linux": "a13cd4fddb171884bdeb2b1e0657ae66039caa19a539b910e10276fd8db9c97a",
    }
    assert selection.digest(json.dumps(before[0], sort_keys=True, separators=(",", ":")).encode()) == historical_identities[platform]
    keys = {style: selection.preparation_key(repo, platform, style) for style in ("posix", "windows")}
    (repo / selection.MANIFEST157).write_bytes(b"invalid")
    (repo / selection.REVIEW157).write_bytes(b"tampered")
    shutil.rmtree(repo / selection.ADDITIONS157_ROOT)
    assert selection.select(repo, platform) == before
    for style, key in keys.items():
        assert selection.preparation_key(repo, platform, style) == key


@pytest.mark.parametrize("new_input", [selection.MANIFEST157, selection.OVERRIDE157_ROOT,
                                        selection.ADDITIONS157_ROOT, *(
    selection.OVERRIDE157_ROOT + "-" + target for target in selection.PLATFORM157_COMMITS)])
def test_unpinned_157_inputs_never_use_root_only_fallback(tmp_path, new_input):
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


@pytest.mark.parametrize("platform", [None, "android"])
def test_explicit_supported_platform_required(repo157, platform):
    repo, _ = repo157
    path = repo / "build/ungoogled-revisions.psd1"
    text = path.read_text()
    for field, value in {"ChromiumVersion": selection.VERSION157,
                         "UngoogledVersion": selection.VERSION157 + "-1",
                         "UngoogledCommit": selection.CORE157}.items():
        text = re.sub(rf'(?m)^(  {field} = )"[^"]+"', rf'\g<1>"{value}"', text)
    path.write_text(text)
    (repo / "CHROMIUM_VERSION").write_text(selection.VERSION157 + "\n")
    with pytest.raises(selection.SelectionError, match="platform|require Windows"):
        selection.select(repo, platform)


def test_manifest_without_pinned_digest_is_not_admitted(repo157, monkeypatch):
    repo, platform = repo157
    monkeypatch.setattr(selection, "MANIFEST157_SHA256", None)
    with pytest.raises(selection.SelectionError, match="reviewed pinned manifest"):
        selection.select(repo, platform)


@pytest.mark.parametrize("root", [selection.OVERRIDE157_ROOT + "-" + platform
                                  for platform in selection.PLATFORM157_COMMITS])
def test_unlisted_platform_additions_fail_closed(repo157, root):
    repo, platform = repo157
    directory = repo / root
    directory.mkdir()
    (directory / "9999-extra.patch").write_text("unreviewed")
    with pytest.raises(selection.SelectionError, match="inventory"):
        selection.select(repo, platform)


@pytest.mark.parametrize("substituted", [False, True])
def test_selected_patch_bytes_replay_exact_source_subset(repo157, tmp_path, substituted):
    repo, platform = repo157
    spec = importlib.util.spec_from_file_location(
        "chromium157_review_verify", ROOT / "patches/chromium157-verification/verify.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    review = json.loads((ROOT / selection.REVIEW157).read_bytes())
    fixture = json.loads(gzip.decompress(verifier.checked(review["fixture"])))
    assert fixture["source"]["version"] == selection.VERSION157
    assert fixture["source"]["core_commit"] == selection.CORE157
    assert not fixture["source"]["errors"]
    src = tmp_path / "src"
    for name, text in fixture["files"].items():
        data = text.encode()
        entry = fixture["source"]["files"][name]
        assert selection.digest(data) == entry["sha256"]
        assert hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == entry["git_blob"]
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CEILING_DIRECTORIES=str(tmp_path))
    for entry in fixture["predecessors"]["core"] + fixture["predecessors"][platform]:
        data = entry["text"].encode()
        assert selection.digest(data) == entry["sha256"]
        verifier.apply(src, data, env, check=True)
        verifier.apply(src, data, env)
    listed = set(fixture["domain_lists"][platform].splitlines()) if substituted else set()
    rules = verifier.arp._rules(fixture["domain_regex"].encode()) if substituted else []
    for name in listed:
        path = src / name
        if path.is_file():
            path.write_text(verifier.arp._substitute(path.read_text(), rules))
    identity, patches = selection.select(repo, platform, src=src)
    assert len(patches) == selection.PATCH_COUNTS157[platform]
    prepared, targets = [], set()
    for name, raw in patches:
        data, entries = verifier.arp.transform_patch(raw, listed, rules)
        prepared.append(data)
        targets.update(target for target, _, _ in entries)
    initial = verifier.snapshot(src, targets)
    for data in prepared:
        verifier.apply(src, data, env, check=True)
        verifier.apply(src, data, env)
    verifier.invariants(src, platform)
    final = verifier.snapshot(src, targets)
    for reverse in (True, False):
        for data in reversed(prepared) if reverse else prepared:
            verifier.apply(src, data, env, check=True, reverse=reverse)
            verifier.apply(src, data, env, reverse=reverse)
        assert verifier.snapshot(src, targets) == (initial if reverse else final)
    verifier.invariants(src, platform)
    assert identity["selection"]["review_manifest"]["sha256"] == selection.REVIEW157_SHA256
