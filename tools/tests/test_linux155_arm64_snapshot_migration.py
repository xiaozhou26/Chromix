"""Exact ARM64 stage4 migration authenticates both additions without widening x64 policy."""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tools import restored_reuse_evidence as evidence
from tools.tests import test_migrate_restored_snapshot as fixtures

migration = fixtures.migration
arp = fixtures.arp
ROOT = Path(__file__).resolve().parents[2]
SELECTOR = "tools/patch_selection.py"
HISTORICAL_TARGET = "14535a9f86865fe58b47a9f593f2b925c94ce0fe"
BUILD = "chrome/browser/enterprise/BUILD.gn"
PLATFORM_BUILD = "third_party/blink/renderer/platform/BUILD.gn"
SOURCE = migration.LINUX155_PNG_SOURCE
DONOR = "e1263e2a52ea8eb7fa4f6c3a9a1930b92a8afe7e"
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
    assert arp._sha(arp._json(old)) == migration.LINUX155_OLD_IDENTITY
    assert arp._sha((repo / SELECTOR).read_bytes()) == expected
    assert arp.patch_selection.preparation_key(previous, "linux", "posix") == (
        "019d1bf9296d7e9b037bc5c34307ebc46cd1b1ba8f4cb31bdd2c80416d625cd5")
    assert arp.patch_selection.preparation_key(repo, "linux", "posix") != (
        arp.patch_selection.preparation_key(previous, "linux", "posix"))
    migration._same_inputs(previous, repo, "linux", "arm64")
    current, patches = arp.patch_selection.select(repo, "linux")
    assert len(old_patches) == 224 and len(patches) == 226
    assert patches[:-2] == old_patches
    assert current["selection"]["additions"]["schema_version"] == 2
    assert arp.patch_selection.select(repo, "windows") == arp.patch_selection.select(previous, "windows")
    assert len(arp.patch_selection.select(repo, "windows")[1]) == 224
    assert (repo / "patches/series").read_bytes() == donor_file("patches/series")
    for policy, arch in ((migration._linux155_selector_transition, "arm64"),
                         (migration._linux155_png_selector_transition, "arm64"),
                         (migration._linux155_arm64_selector_transition, "x64")):
        with pytest.raises(arp.ApplyError, match="requires linux"):
            policy(previous, repo, "linux", arch)
    with pytest.raises(arp.ApplyError):
        migration._same_inputs(previous, repo, "linux", "x64")
    fixtures.put(repo, "CHROMIUM_MACOS_VERSION", b"154.0.8037.97\n\n")
    with pytest.raises(arp.ApplyError, match="preserved inputs"):
        migration._same_inputs(previous, repo, "linux", "arm64")


@pytest.fixture
def arm64_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(fixtures.Fixture, "platform", "linux")
    fx = fixtures.Fixture(tmp_path, arch="arm64", version=migration.LINUX155_VERSION)
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
    assert source["target_commit"] == "12a501c7abe607552c49d5daaabf29d891dde9ae"
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
    fixtures.put(fx.src, "out/Default/args.gn", 'target_cpu = "arm64"\n'
                 'v8_target_cpu = "arm64"\ntarget_os = "linux"\n'
                 'thin_lto_enable_optimizations = false\n')
    compiler = shutil.which("clang")
    if compiler is None:
        pytest.skip("clang is required for the real ARM64 object fixture")
    subprocess.run([compiler, "--target=aarch64-linux-gnu", "-x", "c", "-c", "-",
                    "-o", str(fx.src / "out/Default/obj/cached.o")],
                   input=b"int cached(void) { return 155; }\n", check=True, capture_output=True)
    fx.receipt()
    fx.prepare()
    assert arp._sha((fx.src / SOURCE).read_bytes()) == source["before"]["sha256"]
    return fx


def test_complete_224_to_226_preserves_compiled_tree(arm64_snapshot, monkeypatch):
    fx = arm64_snapshot
    assert evidence.elf_architecture((fx.src / "out/Default/obj/cached.o").read_bytes()[:64]) == "arm64"
    receipt = fixtures.restore.verify_restored(fx.work, "linux", "arm64", repo=fx.previous)
    assert receipt["original_args"]["assignments"]["v8_target_cpu"] == '"arm64"'
    before = fixtures.snapshot(fx.src)
    immutable = arp._snapshot(fx.src, {migration.SOURCE_VERSION, PLATFORM_BUILD,
                                     "out/Default/.ninja_log", "out/Default/.ninja_deps",
                                     "out/Default/obj/cached.o"})
    reverse = migration._reverse
    observed = []

    def inspect(stage, scratch, patches, program):
        assert not stage.is_relative_to(fx.work)
        assert len(patches) == 224
        assert (stage / migration.SOURCE_VERSION).read_bytes() == before[migration.SOURCE_VERSION][0]
        reverse(stage, scratch, patches, program)
        assert b"managed_profile_creation_controller.cc" not in (stage / BUILD).read_bytes()
        assert (stage / PLATFORM_BUILD).read_bytes() == b"base\n"
        source = json.loads(gzip.decompress((ROOT / SOURCE_FIXTURE).read_bytes()))
        assert (stage / SOURCE).read_text() == source["pristine"]["text"]
        assert all((stage / f"old/target-{number:03d}.txt").read_bytes() == b"base\n"
                   for number in range(3, 224))
        observed.append(stage)

    monkeypatch.setattr(migration, "_reverse", inspect)
    result = fx.run()
    assert len(observed) == 1
    assert result["old_patch_count"] == 224 and result["patch_count"] == 226
    assert result["changed_files"] == [BUILD, SOURCE]
    assert result["previous_key"] != result["key"]
    source = json.loads(gzip.decompress((ROOT / SOURCE_FIXTURE).read_bytes()))
    assert arp._sha((fx.src / SOURCE).read_bytes()) == source["after_sha256"]
    after = fixtures.snapshot(fx.src)
    assert after[SOURCE][1] > before[SOURCE][1]
    assert after[BUILD][1] > before[BUILD][1]
    assert b'"signin/managed_profile_creation_controller.cc"' in after[BUILD][0]
    assert b'"signin/managed_profile_creation_controller.cc"' not in before[BUILD][0]
    for name, value in before.items():
        if name not in (BUILD, SOURCE, migration.READY, arp.MARKER):
            assert after[name] == value
    migration._unchanged(fx.src, immutable)
    assert len(json.loads((fx.src / arp.MARKER).read_bytes())["identity"]["series"]["patches"]) == 226
    fx.check()
    assert evidence.elf_architecture((fx.src / "out/Default/obj/cached.o").read_bytes()[:64]) == "arm64"
    assert arp._sha(after["out/Default/obj/cached.o"][0]) == arp._sha(before["out/Default/obj/cached.o"][0])


@pytest.mark.parametrize("change", [
    "selector-old", "selector-new", "selector-both", "head", "manifest-new", "addition", "profile-addition",
    "prefix", "both-prefix", "series", "script", "preserved-both", "source", "version",
    "receipt", "completion", "ready", "arch", "extra", "pins-both",
    "args-arch", "args-v8", "old-addition", "schema", "ordered-additions", "core", "overlay",
])
def test_fail_closed_before_touching_compiled_tree(arm64_snapshot, change):
    fx = arm64_snapshot
    names = {"selector-old": SELECTOR, "selector-new": SELECTOR,
             "manifest-new": migration.LINUX155_MANIFEST,
             "addition": migration.LINUX155_PNG_PATCH, "profile-addition": migration.LINUX155_PATCH, "series": "patches/series",
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
        fx.arch = "x64"
    elif change in ("args-arch", "args-v8"):
        name = "target_cpu" if change == "args-arch" else "v8_target_cpu"
        path = fx.src / "out/Default/args.gn"
        path.write_text(path.read_text().replace(name + ' = "arm64"', name + ' = "x64"'))
    elif change == "old-addition":
        for name in (migration.LINUX155_MANIFEST, migration.LINUX155_PATCH, migration.LINUX155_PNG_PATCH):
            fixtures.put(fx.previous, name, (fx.repo / name).read_bytes())
    elif change in ("schema", "ordered-additions"):
        path = fx.repo / migration.LINUX155_MANIFEST
        manifest = json.loads(path.read_bytes())
        if change == "schema":
            manifest["schema_version"] = 1
        else:
            manifest["patches"].reverse()
        path.write_text(json.dumps(manifest))
    elif change in ("core", "overlay"):
        for repo in (fx.previous, fx.repo):
            path = repo / "build/ungoogled-revisions.psd1"
            old = migration.LINUX155_CORE if change == "core" else migration.LINUX155_OVERLAY
            path.write_text(path.read_text().replace(old, "0" * 40))
    else:
        fixtures.put(fx.repo, "patches/chromium155-linux/unexpected.patch", "unexpected\n")
    before = fixtures.snapshot(fx.src)
    with pytest.raises((arp.ApplyError, ValueError, OSError)):
        fx.run()
    assert fixtures.snapshot(fx.src) == before
    assert not (fx.src / migration.TRANSACTION).exists()
    assert not (fx.src / arp.IN_PROGRESS).exists()


@pytest.mark.parametrize("change", ["source", "selector", "version", "preserved", "head"])
def test_rechecks_before_publication(arm64_snapshot, monkeypatch, change):
    fx = arm64_snapshot
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
    (None, None), ("BUILD_PLATFORM", "macos"), ("ARCH", "x64"),
    ("BUILD_PROFILE", "release"),
    ("SNAPSHOT_RUN_ID", "38147746378"), ("SNAPSHOT_STAGE", "2"),
    ("SNAPSHOT_ATTEMPT", "2"), ("SNAPSHOT_ARTIFACT_IDS", "11734746823,1"),
    ("RECOVERY_BRANCH", "main"), ("RECOVERY_BRANCH", "build/other"), ("RECOVERY_EVENT", "push"),
])
def test_compiled_checkpoint_branch_mapping_is_exact(field, value):
    from tools import gen_posix_workflow as generator

    env = dict(os.environ, BUILD_PLATFORM="linux", ARCH="arm64", BUILD_PROFILE="fast", SNAPSHOT_RUN_ID="38147746377",
               SNAPSHOT_STAGE="4", SNAPSHOT_ATTEMPT="1", SNAPSHOT_ARTIFACT_IDS="11734746823",
               RECOVERY_BRANCH="build/linux155-arm64-stage4-recovery", RECOVERY_EVENT="workflow_dispatch")
    if field:
        env[field] = value
    body = generator.RESUME_STEPS.split('          if [ "$BUILD_PLATFORM"', 1)[1]
    body = 'if [ "$BUILD_PLATFORM"' + body.split("          python3 tools/validate_posix_snapshot.py", 1)[0]
    body = body.replace("'${{ inputs.arch }}'", '"$ARCH"')
    result = subprocess.run(["bash", "-euc", body + '\nprintf "%s" "$RECOVERY_BRANCH"'],
                            env=env, capture_output=True, text=True, check=True)
    expected = "build/chromium155-donors-20261011" if field is None else env["RECOVERY_BRANCH"]
    assert result.stdout == expected


def test_patch_conflict_preserves_tree_and_blocks_retry(arm64_snapshot, monkeypatch):
    fx = arm64_snapshot
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


@pytest.mark.parametrize("change", [None, "path", "repository", "head_repository", "branch", "sha", "size", "digest"])
def test_compiled_checkpoint_still_authenticates_repository_workflow_path(change):
    from tools import validate_posix_snapshot as snapshot
    from tools.tests.test_validate_posix_snapshot import Client, REPO

    client = Client(platform="linux", arch="arm64")
    client.run.update(id=38147746377, head_sha=DONOR, run_attempt=1,
                      head_branch="build/chromium155-donors-20261011")
    client.jobs[0].update(name="build / linux-arm64 stage 4 (resume compile)", conclusion="failure")
    client.artifacts = [{
        "id": 11734746823, "name": "chromix-linux-arm64-tree-s4-attempt-1-part1",
        "size_in_bytes": 5307317175, "expired": False,
        "digest": "sha256:5f3bc2d96d45cc250f12b5da1a193b40dcf7c15f24e806c8628fb6b14d8c978c",
        "workflow_run": {"id": 38147746377, "head_sha": DONOR},
    }]
    if change == "path":
        client.run["path"] = ".github/workflows/build-linux-x64.yml"
    elif change in ("repository", "head_repository"):
        client.run[change] = {"full_name": "foreign/Chromix"}
    elif change == "branch":
        client.run["head_branch"] = "build/other"
    elif change == "sha":
        client.run["head_sha"] = "a" * 40
        client.artifacts[0]["workflow_run"]["head_sha"] = "a" * 40
    elif change == "size":
        client.artifacts[0]["size_in_bytes"] += 1
    elif change == "digest":
        client.artifacts[0]["digest"] = "sha256:" + "a" * 64

    def validate():
        return snapshot.validate(client, REPO, 38147746377, 4, 1, "arm64", [11734746823],
                                 platform="linux", recovery_branch="build/chromium155-donors-20261011")

    if change:
        with pytest.raises(ValueError, match="identity|artifact"):
            validate()
    else:
        report = validate()
        assert report["head_sha"] == DONOR
        assert report["artifacts"][0]["size_in_bytes"] == 5307317175
