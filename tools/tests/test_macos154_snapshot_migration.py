"""Exact macOS154 selector transition and complete staged GNU patch migrations."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
import subprocess

import pytest

from tools.tests import test_migrate_restored_snapshot as fixtures
from tools.tests.test_macos154_headless_quit import TARGET, source_fixture

migration = fixtures.migration
arp = fixtures.arp
restore = fixtures.restore
ROOT = Path(__file__).resolve().parents[2]
BUILD = TARGET
SELECTOR = "tools/patch_selection.py"
HISTORICAL_TARGET = "14535a9f86865fe58b47a9f593f2b925c94ce0fe"



def donor_file(name):
    return subprocess.run(["git", "-C", str(ROOT), "show", migration.MACOS154_DONOR + ":" + name],
                          check=True, capture_output=True).stdout


def target_files(repo, ref=HISTORICAL_TARGET):
    for name in (SELECTOR, migration.MACOS154_MANIFEST, migration.MACOS154_PATCH):
        fixtures.put(repo, name, subprocess.run(
            ["git", "-C", str(ROOT), "show", ref + ":" + name],
            check=True, capture_output=True).stdout)


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_production_policy_accepts_exact_donor_without_executing_it(tmp_path, arch):
    previous = tmp_path / "previous"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", str(ROOT), str(previous)],
                   check=True, capture_output=True)
    fixtures.git(previous, "checkout", "--detach", migration.MACOS154_DONOR)
    repo = tmp_path / "current"
    shutil.copytree(previous, repo, ignore=shutil.ignore_patterns(".git"))
    old = arp.patch_selection.select(previous, "macos")
    assert arp._sha(arp._json(old[0])) == migration.MACOS154_OLD_IDENTITY
    for ref, expected in (
        ("6bd75f297c714492164cbde4cdf2eba70f3cbb6b", migration.MACOS154_NEW_SELECTOR),
        ("543885d56bf9159d9d659ea4fc847f84e019ea74", migration.LINUX155_PNG_SELECTOR),
        (HISTORICAL_TARGET, migration.WINDOWS155_PNG_SELECTOR),
    ):
        target_files(repo, ref)
        assert not (repo / arp.patch_selection.LINUX155_ADDITIONS_MANIFEST).exists()
        assert arp._sha((repo / SELECTOR).read_bytes()) == expected
        migration._same_inputs(previous, repo, "macos", arch)
    assert len(old[1]) == 224
    assert len(arp.patch_selection.select(repo, "macos")[1]) == 225
    for style, expected in (
        ("posix", "92d180fc19b60ad88ca332745f975a3c565485b9d0db17d3672330f3ce68b879"),
        ("windows", "93eb26659777d72ad08b2316ef181fdd336dbf879da12e1f4efcc7b35c808b8a"),
    ):
        assert arp.patch_selection.preparation_key(previous, "macos", style) == expected
        assert arp.patch_selection.preparation_key(repo, "macos", style) != expected
    fixtures.put(repo, SELECTOR, (ROOT / SELECTOR).read_bytes())
    with pytest.raises(arp.ApplyError, match="selector hash"):
        migration._same_inputs(previous, repo, "macos", arch)
    target_files(repo)
    fixtures.put(previous, SELECTOR, donor_file(SELECTOR) + b"\n")
    with pytest.raises(arp.ApplyError, match="selector hash"):
        migration._same_inputs(previous, repo, "macos", arch)


@pytest.fixture(params=["x64", "arm64"])
def snapshot154(tmp_path, monkeypatch, request):
    monkeypatch.setattr(fixtures.Fixture, "platform", "macos")
    fx = fixtures.Fixture(tmp_path, arch=request.param, version=migration.MACOS154_VERSION)
    for repo in (fx.previous, fx.repo):
        path = repo / "build/ungoogled-revisions.psd1"
        path.write_text(path.read_text().replace(
            'UngoogledMacOSVersion = "154.0.8037.97-1"',
            'UngoogledMacOSVersion = "154.0.8037.97-1.1"'))
        path = repo / "build/upstream-cache.json"
        manifest = json.loads(path.read_bytes())
        manifest["sources"]["macos"]["head_branch"] = "154.0.8037.97-1.1"
        path.write_text(json.dumps(manifest))
    fx.receipt()
    pins = migration._pins(fx.repo, "macos")
    overrides = arp.patch_selection.OVERRIDES
    # Synthetic Git objects replace only provenance constants, never verification functions.
    for module, name, value in (
        (migration, "MACOS154_CORE", pins["UngoogledCommit"]),
        (migration, "MACOS154_OVERLAY", pins["UngoogledMacOSCommit"]),
        (arp.patch_selection, "MACOS154_CORE", pins["UngoogledCommit"]),
        (arp.patch_selection, "PLATFORM_COMMITS", {"macos": pins["UngoogledMacOSCommit"]}),
        (arp.patch_selection, "OVERRIDES", {}),
    ):
        monkeypatch.setattr(module, name, value)
    patches = [fixtures.patch()]
    for number in range(1, 224):
        name = f"old/target-{number:03d}.txt"
        fixtures.put(fx.src, name, "base\n")
        patches.append(fixtures.patch(name, "base", "patched"))
    for repo in (fx.previous, fx.repo):
        for name in migration.MACOS154_PRESERVED:
            fixtures.put(repo, name, donor_file(name))
        shutil.copytree(ROOT / arp.patch_selection.OVERRIDE_ROOT,
                        repo / arp.patch_selection.OVERRIDE_ROOT)
        for name in overrides:
            fixtures.put(repo, "patches/" + name, (ROOT / "patches" / name).read_bytes())
        fixtures.series(repo, *patches)
    fixtures.put(fx.previous, SELECTOR, donor_file(SELECTOR))
    target_files(fx.repo)
    monkeypatch.setattr(migration, "MACOS154_DONOR", fixtures.commit(fx.previous))
    identity, selected = arp.patch_selection.select(fx.previous, "macos")
    assert len(selected) == 224
    monkeypatch.setattr(migration, "MACOS154_OLD_IDENTITY", arp._sha(arp._json(identity)))
    fixtures.put(fx.src, BUILD, source_fixture())
    fixtures.put(fx.src, "third_party/devtools-frontend/src/scripts/build/esbuild.js", "prepared bytes\n")
    fx.prepare()
    assert BUILD not in json.loads((fx.src / arp.MARKER).read_bytes())["outputs"]
    return fx


def test_complete_224_to_225_migration_preserves_objects_version_and_prepared_outputs(snapshot154, monkeypatch):
    fx = snapshot154
    before = fixtures.snapshot(fx.src)
    version = arp._snapshot(fx.src, {migration.SOURCE_VERSION})
    reverse = migration._reverse
    observed = []

    def inspect(stage, scratch, patches, program):
        assert not stage.is_relative_to(fx.work)
        assert (stage / BUILD).read_bytes() == before[BUILD][0]
        assert (stage / migration.SOURCE_VERSION).read_bytes() == before[migration.SOURCE_VERSION][0]
        assert b"chrome::CloseAllBrowsersAndQuit();" not in (stage / BUILD).read_bytes()
        assert len(patches) == 224
        reverse(stage, scratch, patches, program)
        assert (stage / "listed.txt").read_bytes() == b"base blocked.test\n"
        assert all((stage / f"old/target-{i:03d}.txt").read_bytes() == b"base\n" for i in range(1, 224))
        observed.append(stage)

    monkeypatch.setattr(migration, "_reverse", inspect)
    result = fx.run()
    assert len(observed) == 1
    assert result["old_patch_count"] == 224 and result["patch_count"] == 225
    assert result["changed_files"] == [BUILD]
    assert result["key"] != result["previous_key"]
    after = fixtures.snapshot(fx.src)
    assert b"chrome::CloseAllBrowsersAndQuit();" in after[BUILD][0]
    assert b"browser->GetWindow()->Close();" not in after[BUILD][0]
    assert after[BUILD][1] > before[BUILD][1]
    for name, value in before.items():
        if name not in (BUILD, migration.READY, arp.MARKER):
            assert after[name] == value
    migration._unchanged(fx.src, version)
    saved = json.loads((fx.src / arp.MARKER).read_bytes())
    assert BUILD in saved["outputs"] and migration.SOURCE_VERSION not in saved["outputs"]
    assert len(saved["identity"]["series"]["patches"]) == 225
    fx.check()


@pytest.mark.parametrize("change", [
    "selector-old", "selector-new", "head", "manifest", "addition", "prefix", "both-prefix",
    "root-series", "script", "preserved", "preserved-both", "source", "version", "receipt",
    "completion", "ready", "arch", "core", "overlay", "old-addition", "extra-addition",
])
def test_transition_failures_leave_full_source_and_receipts_untouched(snapshot154, change):
    fx = snapshot154
    if change in ("selector-old", "selector-new"):
        path = (fx.previous if change == "selector-old" else fx.repo) / SELECTOR
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "head":
        fixtures.put(fx.previous, "new-file", "dirty")
        fixtures.commit(fx.previous)
    elif change in ("manifest", "addition", "script", "preserved", "root-series"):
        name = {"manifest": migration.MACOS154_MANIFEST, "addition": migration.MACOS154_PATCH,
                "script": "build/apply-patches.sh", "preserved": "tools/prepare_restored_build.py",
                "root-series": "patches/series"}[change]
        path = fx.repo / name
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "preserved-both":
        for repo in (fx.previous, fx.repo):
            path = repo / "tools/prepare_restored_build.py"
            path.write_bytes(path.read_bytes() + b"\n")
    elif change in ("prefix", "both-prefix"):
        for repo in ((fx.repo,) if change == "prefix" else (fx.previous, fx.repo)):
            fixtures.put(repo, "patches/0001.patch", fixtures.patch("old/target-001.txt", "base", "other"))
    elif change == "source":
        fixtures.put(fx.src, "old/target-117.txt", "checksum changed\n")
    elif change == "version":
        fixtures.put(fx.src, migration.SOURCE_VERSION, "MAJOR=155\nMINOR=0\nBUILD=8059\nPATCH=39\n")
    elif change in ("receipt", "completion", "ready"):
        name = {"receipt": restore.MARKER, "completion": arp.MARKER, "ready": migration.READY}[change]
        fixtures.put(fx.src, name, "{}\n")
    elif change == "arch":
        fx.arch = "ia32"
    elif change in ("core", "overlay"):
        for repo in (fx.previous, fx.repo):
            path = repo / "build/ungoogled-revisions.psd1"
            old = migration.MACOS154_CORE if change == "core" else migration.MACOS154_OVERLAY
            path.write_text(path.read_text().replace(old, "0" * 40))
    elif change == "old-addition":
        for name in (migration.MACOS154_MANIFEST, migration.MACOS154_PATCH):
            fixtures.put(fx.previous, name, (fx.repo / name).read_bytes())
    else:
        fixtures.put(fx.repo, "patches/chromium154-macos/unexpected.patch", "unexpected\n")
    before = fixtures.snapshot(fx.src)
    with pytest.raises((arp.ApplyError, ValueError, OSError)):
        fx.run()
    assert fixtures.snapshot(fx.src) == before
    assert not (fx.src / migration.TRANSACTION).exists()
    assert not (fx.src / arp.IN_PROGRESS).exists()


def test_new_startup_patch_conflict_preserves_original_tree_and_blocks_retry(snapshot154):
    fx = snapshot154
    fixtures.put(fx.src, BUILD, "not the authenticated patch context\n")
    before = fixtures.snapshot(fx.src)
    with pytest.raises(arp.ApplyError):
        fx.run()
    after = fixtures.snapshot(fx.src)
    assert all(after[name] == value for name, value in before.items())
    assert (fx.src / migration.TRANSACTION).exists()
    assert (fx.src / arp.IN_PROGRESS).exists()
    with pytest.raises(arp.ApplyError, match="in-progress/partial"):
        fx.run()


@pytest.mark.parametrize("change", ["source", "selector", "head", "version", "preserved"])
def test_transition_rechecks_before_publication(snapshot154, monkeypatch, change):
    fx = snapshot154
    before = fixtures.snapshot(fx.src)
    apply = arp.run_apply

    def race(src, *args, **kwargs):
        result = apply(src, *args, **kwargs)
        if Path(src) != fx.src and kwargs.get("check"):
            if change == "source":
                fixtures.put(fx.src, "old/target-117.txt", "concurrent\n")
            elif change == "version":
                fixtures.put(src, migration.SOURCE_VERSION, "MAJOR=155\nMINOR=0\nBUILD=8059\nPATCH=39\n")
            elif change == "head":
                fixtures.put(fx.previous, "new-file", "dirty")
                fixtures.commit(fx.previous)
            else:
                name = SELECTOR if change == "selector" else "tools/prepare_restored_build.py"
                path = fx.repo / name
                path.write_bytes(path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(arp, "run_apply", race)
    with pytest.raises((arp.ApplyError, ValueError)):
        fx.run()
    after = fixtures.snapshot(fx.src)
    for name, value in before.items():
        if change != "source" or name != "old/target-117.txt":
            assert after[name] == value
    assert (fx.src / migration.TRANSACTION).exists()
    assert (fx.src / arp.IN_PROGRESS).exists()
