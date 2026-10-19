"""Exact compiled Linux155 donor migration preserves objects and historical profiles."""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tools.tests import test_migrate_restored_snapshot as fixtures

migration = fixtures.migration
arp = fixtures.arp
ROOT = Path(__file__).resolve().parents[2]
SELECTOR = "tools/patch_selection.py"
HISTORICAL_TARGET = "14535a9f86865fe58b47a9f593f2b925c94ce0fe"
BUILD = "chrome/browser/enterprise/BUILD.gn"
PLATFORM_BUILD = "third_party/blink/renderer/platform/BUILD.gn"
SOURCE = migration.LINUX155_PNG_SOURCE
DONOR = "12a501c7abe607552c49d5daaabf29d891dde9ae"
SOURCE_FIXTURE = "tools/tests/fixtures/linux155_opaque_png_sources.json.gz"


@pytest.fixture(autouse=True)
def exact_historical_runner(tmp_path, monkeypatch):
    return fixtures.historical_runner(tmp_path, monkeypatch, HISTORICAL_TARGET)


def donor_file(name):
    return subprocess.run(["git", "-C", str(ROOT), "show", DONOR + ":" + name],
                          check=True, capture_output=True).stdout


def target_files(repo, ref=HISTORICAL_TARGET):
    for name in (SELECTOR, migration.LINUX155_MANIFEST, migration.LINUX155_PATCH, migration.LINUX155_PNG_PATCH):
        fixtures.put(repo, name, subprocess.run(
            ["git", "-C", str(ROOT), "show", ref + ":" + name],
            check=True, capture_output=True).stdout)


@pytest.mark.parametrize("selector_ref,expected", [
    (HISTORICAL_TARGET, migration.WINDOWS155_PNG_SELECTOR),
    ("543885d56bf9159d9d659ea4fc847f84e019ea74", migration.LINUX155_PNG_SELECTOR),
])
def test_exact_donor_identity_keys_and_isolated_target(tmp_path, selector_ref, expected):
    previous = tmp_path / "previous"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", str(ROOT), str(previous)],
                   check=True, capture_output=True)
    fixtures.git(previous, "checkout", "--detach", DONOR)
    repo = tmp_path / "target"
    shutil.copytree(previous, repo, ignore=shutil.ignore_patterns(".git"))
    target_files(repo, selector_ref)
    old, old_patches = arp.patch_selection.select(previous, "linux")
    assert arp._sha(arp._json(old)) == migration.LINUX155_PNG_OLD_IDENTITY
    assert arp._sha((repo / SELECTOR).read_bytes()) == expected
    for style, key in (
        ("posix", "d3aec8b6637d7582f4ce572533d9ca5ced7c6c5f77f897c56e12e23dd20985ee"),
        ("windows", "25a7ada3eb59ef78d7dab0e16a1ee1c690531b9159a640bb16d5c3f9683b853e"),
    ):
        assert arp.patch_selection.preparation_key(previous, "linux", style) == key
        assert arp.patch_selection.preparation_key(repo, "linux", style) != key
    migration._same_inputs(previous, repo, "linux", "x64")
    current, patches = arp.patch_selection.select(repo, "linux")
    assert len(old_patches) == 225 and len(patches) == 226
    assert patches[:-1] == old_patches
    assert current["selection"]["additions"]["schema_version"] == 2
    assert arp.patch_selection.select(repo, "windows") == arp.patch_selection.select(previous, "windows")
    assert len(arp.patch_selection.select(repo, "windows")[1]) == 224
    assert (repo / "patches/series").read_bytes() == donor_file("patches/series")
    fixtures.put(repo, "CHROMIUM_MACOS_VERSION", b"154.0.8037.97\n\n")
    with pytest.raises(arp.ApplyError, match="preserved inputs"):
        migration._same_inputs(previous, repo, "linux", "x64")


@pytest.fixture
def compiled_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(fixtures.Fixture, "platform", "linux")
    fx = fixtures.Fixture(tmp_path, version=migration.LINUX155_VERSION)
    pins = migration._pins(fx.repo, "linux")
    for module, name, value in (
        (migration, "LINUX155_CORE", pins["UngoogledCommit"]),
        (migration, "LINUX155_OVERLAY", pins["UngoogledLinuxCommit"]),
        (arp.patch_selection, "CORE155", pins["UngoogledCommit"]),
        (arp.patch_selection, "PLATFORM155_COMMITS", {"linux": pins["UngoogledLinuxCommit"]}),
        (arp.patch_selection, "OVERRIDES155", {}),
    ):
        monkeypatch.setattr(module, name, value)
    raw = (ROOT / SOURCE_FIXTURE).read_bytes()
    assert arp._sha(raw) == "5febb57490ec56c67070bc6aff4daf90a41f94073e09e910233ee719c57efbef"
    source = json.loads(gzip.decompress(raw))
    assert source["target_commit"] == DONOR
    assert arp._sha(source["before"]["text"].encode()) == source["before"]["sha256"]
    fixtures.put(fx.src, SOURCE, source["pristine"]["text"])
    patches = [fixtures.patch(), fixtures.patch(PLATFORM_BUILD, "base", "prepared GN"),
               donor_file(source["predecessor"]["path"])]
    fixtures.put(fx.src, PLATFORM_BUILD, "base\n")
    for number in range(3, 224):
        name = f"old/target-{number:03d}.txt"
        fixtures.put(fx.src, name, "base\n")
        patches.append(fixtures.patch(name, "base", "patched"))
    for repo in (fx.previous, fx.repo):
        for name in migration.LINUX155_PRESERVED:
            fixtures.put(repo, name, donor_file(name))
        (repo / arp.patch_selection.OVERRIDE155_ROOT).mkdir(parents=True)
        fixtures.series(repo, *patches)
        fixtures.put(repo, migration.LINUX155_MANIFEST, donor_file(migration.LINUX155_MANIFEST))
        fixtures.put(repo, migration.LINUX155_PATCH, donor_file(migration.LINUX155_PATCH))
    fixtures.put(fx.previous, SELECTOR, donor_file(SELECTOR))
    target_files(fx.repo)
    monkeypatch.setattr(migration, "LINUX155_PNG_DONOR", fixtures.commit(fx.previous))
    identity, selected = arp.patch_selection.select(fx.previous, "linux")
    assert len(selected) == 225
    monkeypatch.setattr(migration, "LINUX155_PNG_OLD_IDENTITY", arp._sha(arp._json(identity)))
    fixtures.put(fx.src, BUILD, b"\n" * 792 + (
        b'      "signin/enterprise_signin_prefs.cc",\n'
        b'      "signin/enterprise_signin_service.cc",\n'
        b'      "signin/enterprise_signin_service_factory.cc",\n'
        b'      "signin/managed_profile_creator.cc",\n'
        b'      "signin/managed_profile_required_navigation_throttle.cc",\n'
        b'      "signin/oidc_authentication_signin_interceptor.cc",\n'))
    fixtures.put(fx.src, "third_party/devtools-frontend/src/scripts/build/esbuild.js", "prepared bytes\n")
    fx.prepare()
    assert arp._sha((fx.src / SOURCE).read_bytes()) == source["before"]["sha256"]
    return fx


def test_complete_225_to_226_preserves_compiled_tree(compiled_snapshot, monkeypatch):
    fx = compiled_snapshot
    before = fixtures.snapshot(fx.src)
    immutable = arp._snapshot(fx.src, {migration.SOURCE_VERSION, BUILD, PLATFORM_BUILD,
                                     "out/Default/.ninja_log", "out/Default/.ninja_deps",
                                     "out/Default/obj/cached.o"})
    reverse = migration._reverse
    observed = []

    def inspect(stage, scratch, patches, program):
        assert not stage.is_relative_to(fx.work)
        assert len(patches) == 225
        assert (stage / migration.SOURCE_VERSION).read_bytes() == before[migration.SOURCE_VERSION][0]
        reverse(stage, scratch, patches, program)
        assert b"managed_profile_creation_controller.cc" not in (stage / BUILD).read_bytes()
        assert (stage / PLATFORM_BUILD).read_bytes() == b"base\n"
        observed.append(stage)

    monkeypatch.setattr(migration, "_reverse", inspect)
    result = fx.run()
    assert len(observed) == 1
    assert result["old_patch_count"] == 225 and result["patch_count"] == 226
    assert result["changed_files"] == [SOURCE]
    assert result["previous_key"] != result["key"]
    source = json.loads(gzip.decompress((ROOT / SOURCE_FIXTURE).read_bytes()))
    assert arp._sha((fx.src / SOURCE).read_bytes()) == source["after_sha256"]
    after = fixtures.snapshot(fx.src)
    assert after[SOURCE][1] > before[SOURCE][1]
    for name, value in before.items():
        if name not in (SOURCE, migration.READY, arp.MARKER):
            assert after[name] == value
    migration._unchanged(fx.src, immutable)
    assert len(json.loads((fx.src / arp.MARKER).read_bytes())["identity"]["series"]["patches"]) == 226
    fx.check()


@pytest.mark.parametrize("change", [
    "selector-old", "selector-new", "selector-both", "head", "manifest-old", "manifest-new", "addition",
    "prefix", "both-prefix", "series", "script", "preserved-both", "source", "version",
    "receipt", "completion", "ready", "arch", "extra", "pins-both",
])
def test_fail_closed_before_touching_compiled_tree(compiled_snapshot, change):
    fx = compiled_snapshot
    names = {"selector-old": SELECTOR, "selector-new": SELECTOR,
             "manifest-old": migration.LINUX155_MANIFEST, "manifest-new": migration.LINUX155_MANIFEST,
             "addition": migration.LINUX155_PNG_PATCH, "series": "patches/series",
             "script": "build/apply-patches.sh"}
    if change in names:
        repo = fx.previous if change.endswith("-old") else fx.repo
        path = repo / names[change]
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "selector-both":
        fixtures.put(fx.previous, SELECTOR, (fx.repo / SELECTOR).read_bytes())
    elif change == "head":
        fixtures.put(fx.previous, "new-file", "dirty")
        fixtures.commit(fx.previous)
    elif change in ("prefix", "both-prefix", "preserved-both", "pins-both"):
        name = {"preserved-both": "tools/prepare_restored_build.py",
                "pins-both": "CHROMIUM_MACOS_VERSION"}.get(change, "patches/0003.patch")
        for repo in ((fx.repo,) if change == "prefix" else (fx.previous, fx.repo)):
            path = repo / name
            path.write_bytes(path.read_bytes() + b"\n")
    elif change in ("source", "version", "receipt", "completion", "ready"):
        name = {"source": "old/target-117.txt", "version": migration.SOURCE_VERSION,
                "receipt": fixtures.restore.MARKER, "completion": arp.MARKER,
                "ready": migration.READY}[change]
        fixtures.put(fx.src, name, "changed\n")
    elif change == "arch":
        fx.arch = "arm64"
    else:
        fixtures.put(fx.repo, "patches/chromium155-linux/unexpected.patch", "unexpected\n")
    before = fixtures.snapshot(fx.src)
    with pytest.raises((arp.ApplyError, ValueError, OSError)):
        fx.run()
    assert fixtures.snapshot(fx.src) == before
    assert not (fx.src / migration.TRANSACTION).exists()
    assert not (fx.src / arp.IN_PROGRESS).exists()


@pytest.mark.parametrize("change", ["source", "selector", "version", "preserved", "head"])
def test_rechecks_before_publication(compiled_snapshot, monkeypatch, change):
    fx = compiled_snapshot
    before = fixtures.snapshot(fx.src)
    apply = arp.run_apply

    def race(src, *args, **kwargs):
        result = apply(src, *args, **kwargs)
        if Path(src) != fx.src and kwargs.get("check"):
            if change == "source":
                fixtures.put(fx.src, "old/target-117.txt", "concurrent\n")
            elif change == "version":
                fixtures.put(src, migration.SOURCE_VERSION, "MAJOR=0\n")
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
    ("SNAPSHOT_RUN_ID", "38204495351"), ("SNAPSHOT_STAGE", "2"),
    ("SNAPSHOT_ATTEMPT", "2"), ("SNAPSHOT_ARTIFACT_IDS", "11733517946,1"),
    ("RECOVERY_BRANCH", "main"), ("RECOVERY_BRANCH", "build/other"), ("RECOVERY_EVENT", "push"),
])
def test_compiled_checkpoint_branch_mapping_is_exact(field, value):
    from tools import gen_posix_workflow as generator

    env = dict(os.environ, BUILD_PLATFORM="linux", ARCH="x64", SNAPSHOT_RUN_ID="38204495350",
               SNAPSHOT_STAGE="1", SNAPSHOT_ATTEMPT="1", SNAPSHOT_ARTIFACT_IDS="11733517946",
               RECOVERY_BRANCH="build/linux155-compiled-png-recovery", RECOVERY_EVENT="workflow_dispatch")
    if field:
        env[field] = value
    body = generator.RESUME_STEPS.split('          if [ "$BUILD_PLATFORM"', 1)[1]
    body = 'if [ "$BUILD_PLATFORM"' + body.split("          python3 tools/validate_posix_snapshot.py", 1)[0]
    body = body.replace("'${{ inputs.arch }}'", '"$ARCH"')
    result = subprocess.run(["bash", "-euc", body + '\nprintf "%s" "$RECOVERY_BRANCH"'],
                            env=env, capture_output=True, text=True, check=True)
    expected = "build/linux155-stage2-controller-link" if field is None else env["RECOVERY_BRANCH"]
    assert result.stdout == expected


def test_patch_conflict_preserves_tree_and_blocks_retry(compiled_snapshot, monkeypatch):
    fx = compiled_snapshot
    reverse = migration._reverse

    def conflict(stage, *args):
        reverse(stage, *args)
        fixtures.put(stage, SOURCE, "not the authenticated patch context\n")

    monkeypatch.setattr(migration, "_reverse", conflict)
    before = fixtures.snapshot(fx.src)
    with pytest.raises(arp.ApplyError):
        fx.run()
    after = fixtures.snapshot(fx.src)
    assert all(after[name] == value for name, value in before.items())
    assert (fx.src / migration.TRANSACTION).exists()
    assert (fx.src / arp.IN_PROGRESS).exists()
    with pytest.raises(arp.ApplyError, match="in-progress/partial"):
        fx.run()


@pytest.mark.parametrize("change", [None, "path", "repository", "head_repository", "branch"])
def test_compiled_checkpoint_still_authenticates_repository_workflow_path(change):
    from tools import validate_posix_snapshot as snapshot
    from tools.tests.test_validate_posix_snapshot import Client, REPO

    client = Client(platform="linux", arch="x64")
    client.run.update(id=38204495350, head_sha=DONOR, run_attempt=1,
                      head_branch="build/linux155-stage2-controller-link")
    client.jobs[0].update(name="build / linux-x64 stage 1 (resume compile)", conclusion="failure")
    client.artifacts = [{
        "id": 11733517946, "name": "chromix-linux-x64-tree-s1-attempt-1-part1",
        "size_in_bytes": 5009170838, "expired": False,
        "digest": "sha256:8b1639700a45bb60759857c9fee6ff0d905d09c4b2e93b0e623278a03643b359",
        "workflow_run": {"id": 38204495350, "head_sha": DONOR},
    }]
    if change == "path":
        client.run["path"] = ".github/workflows/build-linux-arm64.yml"
    elif change in ("repository", "head_repository"):
        client.run[change] = {"full_name": "foreign/Chromix"}
    elif change == "branch":
        client.run["head_branch"] = "build/other"

    def validate():
        return snapshot.validate(client, REPO, 38204495350, 1, 1, "x64", [11733517946],
                                 platform="linux", recovery_branch="build/linux155-stage2-controller-link")

    if change:
        with pytest.raises(ValueError, match="identity"):
            validate()
    else:
        report = validate()
        assert report["head_sha"] == DONOR
        assert report["artifacts"][0]["size_in_bytes"] == 5009170838


def test_windows_selector_keeps_exact_posix_identities_and_historical_hashes(tmp_path):
    import io
    import tarfile
    from tools import patch_selection

    baseline = tmp_path / "pre-windows"
    baseline.mkdir()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive",
        "543885d56bf9159d9d659ea4fc847f84e019ea74"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(baseline, filter="data")
    assert arp._sha((baseline / SELECTOR).read_bytes()) == migration.LINUX155_PNG_SELECTOR
    from tools.tests.test_patch_selection import clone_repo, set_platform_pins
    reviewed = clone_repo(tmp_path / "reviewed")
    set_platform_pins(reviewed, "macos", migration.MACOS154_VERSION,
                      migration.MACOS154_CORE, migration.MACOS154_OVERLAY)
    assert patch_selection.select(reviewed, "linux") == patch_selection.select(ROOT, "linux")
    assert len(patch_selection.select(reviewed, "windows")[1]) == 231
    assert patch_selection.select(ROOT, "macos")[0]["selection"]["version"] == "155.0.8059.39"
    assert len(patch_selection.select(ROOT, "macos")[1]) == 224
    expected = {
        "linux": (226, "446ae87099e7804ab8e7d975ade6b1e75c1080e78b84c0bcf58a2f786b373586"),
        "macos": (225, "82c050b84d7d1cd0cd1424d7afdab3b566d403dd10dbd2ee40054872ae9ca69e"),
    }
    for platform, (count, identity) in expected.items():
        old = patch_selection.select(baseline, platform)
        current = patch_selection.select(reviewed, platform)
        assert current == old
        assert len(current[1]) == count
        assert arp._sha(arp._json(current[0])) == identity
    assert migration.LINUX155_PNG_SUPPORTING_SELECTORS == (
        "7e59710e98b88556c19d5cf53563eee0dc4f94b2cdabed0051adcad68fe11542",
        "4c2392a036f0e3c754ed887b41e67e624d80350f53d0059f7308205d05ca5185",
    )
    assert migration.LINUX155_SUPPORTING_SELECTORS == (
        "9a706c5ec4aca59a1a06154fe7c592746f32c2c28571ee33a7c5d55fd523bb26",
        "3eb4381914588389feec36640cfd70c7838c38d9da2ba0ab34c3e53dfa3281f3",
        *migration.LINUX155_PNG_SUPPORTING_SELECTORS,
    )
    assert migration.MACOS154_SUPPORTING_SELECTORS == (
        "3eb4381914588389feec36640cfd70c7838c38d9da2ba0ab34c3e53dfa3281f3",
        *migration.LINUX155_PNG_SUPPORTING_SELECTORS,
    )
