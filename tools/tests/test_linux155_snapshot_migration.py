"""Exact Linux155 selector transition and complete staged GNU patch migrations."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import subprocess

import pytest

from tools.tests import test_migrate_restored_snapshot as fixtures

migration = fixtures.migration
arp = fixtures.arp
restore = fixtures.restore
ROOT = Path(__file__).resolve().parents[2]
BUILD = "chrome/browser/enterprise/BUILD.gn"
SELECTOR = "tools/patch_selection.py"
HISTORICAL_TARGET = "14535a9f86865fe58b47a9f593f2b925c94ce0fe"
SOURCE_LINE = b'      "signin/managed_profile_creation_controller.cc",\n'


@pytest.fixture(autouse=True)
def exact_historical_runner(tmp_path, monkeypatch):
    return fixtures.historical_runner(tmp_path, monkeypatch, HISTORICAL_TARGET)


def donor_file(name):
    return subprocess.run(["git", "-C", str(ROOT), "show", migration.LINUX155_DONOR + ":" + name],
                          check=True, capture_output=True).stdout


def target_files(repo, selector_ref=HISTORICAL_TARGET):
    # The 225-patch target predates the PNG manifest in later selector commits.
    for name, ref in ((SELECTOR, selector_ref),
                      (migration.LINUX155_MANIFEST, migration.LINUX155_PNG_DONOR),
                      (migration.LINUX155_PATCH, migration.LINUX155_PNG_DONOR)):
        fixtures.put(repo, name, subprocess.run(
            ["git", "-C", str(ROOT), "show", ref + ":" + name],
            check=True, capture_output=True).stdout)


def test_production_policy_accepts_exact_donor_without_executing_it(tmp_path, monkeypatch):
    previous = tmp_path / "previous"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", str(ROOT), str(previous)],
                   check=True, capture_output=True)
    fixtures.git(previous, "checkout", "--detach", migration.LINUX155_DONOR)
    old = arp.patch_selection.select(previous, "linux")
    assert arp._sha(arp._json(old[0])) == migration.LINUX155_OLD_IDENTITY
    target = tmp_path / "reviewed-target"
    shutil.copytree(previous, target, ignore=shutil.ignore_patterns(".git"))
    assert len(old[1]) == 224
    assert arp.patch_selection.preparation_key(previous, "linux", "posix") == (
        "019d1bf9296d7e9b037bc5c34307ebc46cd1b1ba8f4cb31bdd2c80416d625cd5")
    for ref, expected in ((migration.LINUX155_PNG_DONOR, migration.LINUX155_NEW_SELECTOR),
                          ("6bd75f297c714492164cbde4cdf2eba70f3cbb6b", migration.MACOS154_NEW_SELECTOR),
                          ("543885d56bf9159d9d659ea4fc847f84e019ea74", migration.LINUX155_PNG_SELECTOR),
                          (HISTORICAL_TARGET, migration.WINDOWS155_PNG_SELECTOR)):
        target_files(target, ref)
        assert arp._sha((target / SELECTOR).read_bytes()) == expected
        migration._same_inputs(previous, target, "linux", "x64")
        assert len(arp.patch_selection.select(target, "linux")[1]) == 225
    with monkeypatch.context() as current_runner:
        current_runner.setattr(migration, "__file__", str(ROOT / "tools/migrate_restored_snapshot.py"))
        with pytest.raises(arp.ApplyError, match="preserved preparation tooling"):
            migration._same_inputs(previous, target, "linux", "x64")
    with pytest.raises(arp.ApplyError):
        migration._same_inputs(previous, ROOT, "linux", "x64")
    fixtures.put(previous, SELECTOR, donor_file(SELECTOR) + b"\n")
    with pytest.raises(arp.ApplyError, match="selector hash"):
        migration._same_inputs(previous, target, "linux", "x64")


@pytest.fixture
def snapshot155(tmp_path, monkeypatch):
    monkeypatch.setattr(fixtures.Fixture, "platform", "linux")
    fx = fixtures.Fixture(tmp_path, version=migration.LINUX155_VERSION)
    pins = migration._pins(fx.repo, "linux")
    # Synthetic Git objects replace only provenance constants, never verification functions.
    for module, name, value in (
        (migration, "LINUX155_CORE", pins["UngoogledCommit"]),
        (migration, "LINUX155_OVERLAY", pins["UngoogledLinuxCommit"]),
        (arp.patch_selection, "CORE155", pins["UngoogledCommit"]),
        (arp.patch_selection, "PLATFORM155_COMMITS", {"linux": pins["UngoogledLinuxCommit"]}),
        (arp.patch_selection, "OVERRIDES155", {}),
    ):
        monkeypatch.setattr(module, name, value)
    patches = [fixtures.patch()]
    for number in range(1, 224):
        name = f"old/target-{number:03d}.txt"
        fixtures.put(fx.src, name, "base\n")
        patches.append(fixtures.patch(name, "base", "patched"))
    for repo in (fx.previous, fx.repo):
        for name in migration.LINUX155_PRESERVED:
            fixtures.put(repo, name, donor_file(name))
        (repo / arp.patch_selection.OVERRIDE155_ROOT).mkdir(parents=True)
        fixtures.series(repo, *patches)
    fixtures.put(fx.previous, SELECTOR, donor_file(SELECTOR))
    target_files(fx.repo)
    monkeypatch.setattr(migration, "LINUX155_DONOR", fixtures.commit(fx.previous))
    identity, selected = arp.patch_selection.select(fx.previous, "linux")
    assert len(selected) == 224
    monkeypatch.setattr(migration, "LINUX155_OLD_IDENTITY", arp._sha(arp._json(identity)))
    fixtures.put(fx.src, BUILD, b"\n" * 792 + (
        b'      "signin/enterprise_signin_prefs.cc",\n'
        b'      "signin/enterprise_signin_service.cc",\n'
        b'      "signin/enterprise_signin_service_factory.cc",\n'
        b'      "signin/managed_profile_creator.cc",\n'
        b'      "signin/managed_profile_required_navigation_throttle.cc",\n'
        b'      "signin/oidc_authentication_signin_interceptor.cc",\n'))
    fixtures.put(fx.src, "third_party/devtools-frontend/src/scripts/build/esbuild.js", "prepared bytes\n")
    fx.prepare()
    assert BUILD not in json.loads((fx.src / arp.MARKER).read_bytes())["outputs"]
    return fx


def test_complete_224_to_225_migration_preserves_objects_version_and_prepared_outputs(snapshot155, monkeypatch):
    fx = snapshot155
    before = fixtures.snapshot(fx.src)
    version = arp._snapshot(fx.src, {migration.SOURCE_VERSION})
    reverse = migration._reverse
    observed = []

    def inspect(stage, scratch, patches, program):
        assert not stage.is_relative_to(fx.work)
        assert (stage / BUILD).read_bytes() == before[BUILD][0]
        assert (stage / migration.SOURCE_VERSION).read_bytes() == before[migration.SOURCE_VERSION][0]
        assert not (stage / "chrome/browser/enterprise/signin/managed_profile_creation_controller.cc").exists()
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
    assert after[BUILD][0].replace(SOURCE_LINE, b"") == before[BUILD][0]
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
def test_transition_failures_leave_full_source_and_receipts_untouched(snapshot155, change):
    fx = snapshot155
    if change in ("selector-old", "selector-new"):
        path = (fx.previous if change == "selector-old" else fx.repo) / SELECTOR
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "head":
        fixtures.put(fx.previous, "new-file", "dirty")
        fixtures.commit(fx.previous)
    elif change in ("manifest", "addition", "script", "preserved", "root-series"):
        name = {"manifest": migration.LINUX155_MANIFEST, "addition": migration.LINUX155_PATCH,
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
        fixtures.put(fx.src, migration.SOURCE_VERSION, "MAJOR=154\nMINOR=0\nBUILD=8037\nPATCH=97\n")
    elif change in ("receipt", "completion", "ready"):
        name = {"receipt": restore.MARKER, "completion": arp.MARKER, "ready": migration.READY}[change]
        fixtures.put(fx.src, name, "{}\n")
    elif change == "arch":
        fx.arch = "arm64"
    elif change in ("core", "overlay"):
        for repo in (fx.previous, fx.repo):
            path = repo / "build/ungoogled-revisions.psd1"
            old = migration.LINUX155_CORE if change == "core" else migration.LINUX155_OVERLAY
            path.write_text(path.read_text().replace(old, "0" * 40))
    elif change == "old-addition":
        for name in (migration.LINUX155_MANIFEST, migration.LINUX155_PATCH):
            fixtures.put(fx.previous, name, (fx.repo / name).read_bytes())
    else:
        fixtures.put(fx.repo, "patches/chromium155-linux/unexpected.patch", "unexpected\n")
    before = fixtures.snapshot(fx.src)
    with pytest.raises((arp.ApplyError, ValueError, OSError)):
        fx.run()
    assert fixtures.snapshot(fx.src) == before
    assert not (fx.src / migration.TRANSACTION).exists()
    assert not (fx.src / arp.IN_PROGRESS).exists()


def test_new_gn_patch_conflict_preserves_original_tree_and_blocks_retry(snapshot155):
    fx = snapshot155
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
def test_transition_rechecks_before_publication(snapshot155, monkeypatch, change):
    fx = snapshot155
    before = fixtures.snapshot(fx.src)
    apply = arp.run_apply

    def race(src, *args, **kwargs):
        result = apply(src, *args, **kwargs)
        if Path(src) != fx.src and kwargs.get("check"):
            if change == "source":
                fixtures.put(fx.src, "old/target-117.txt", "concurrent\n")
            elif change == "version":
                fixtures.put(src, migration.SOURCE_VERSION, "MAJOR=154\nMINOR=0\nBUILD=8037\nPATCH=97\n")
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


@pytest.mark.parametrize("field,value", [
    (None, None), ("BUILD_PLATFORM", "macos"), ("ARCH", "arm64"),
    ("SNAPSHOT_RUN_ID", "38149695143"), ("SNAPSHOT_STAGE", "3"),
    ("SNAPSHOT_ATTEMPT", "2"), ("SNAPSHOT_ARTIFACT_IDS", "11718904327,1"),
    ("RECOVERY_BRANCH", "main"), ("RECOVERY_BRANCH", "build/other"), ("RECOVERY_EVENT", "push"),
])
def test_generated_resume_mapping_is_exact(field, value):
    from tools import gen_posix_workflow as generator

    env = dict(os.environ, BUILD_PLATFORM="linux", ARCH="x64", SNAPSHOT_RUN_ID="38149695142",
               SNAPSHOT_STAGE="2", SNAPSHOT_ATTEMPT="1", SNAPSHOT_ARTIFACT_IDS="11718904327",
               RECOVERY_BRANCH="build/linux155-stage2-controller-link", RECOVERY_EVENT="workflow_dispatch")
    if field:
        env[field] = value
    body = generator.RESUME_STEPS.split('          if [ "$BUILD_PLATFORM"', 1)[1]
    body = 'if [ "$BUILD_PLATFORM"' + body.split("          python3 tools/validate_posix_snapshot.py", 1)[0]
    body = body.replace("'${{ inputs.arch }}'", '"$ARCH"')
    result = subprocess.run(["bash", "-euc", body + '\nprintf "%s" "$RECOVERY_BRANCH"'],
                            env=env, capture_output=True, text=True, check=True)
    expected = "build/chromium155-donors-20261011" if field is None else env["RECOVERY_BRANCH"]
    assert result.stdout == expected
