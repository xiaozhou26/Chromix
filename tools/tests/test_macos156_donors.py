"""Exact mac156 donor and source replay contracts; not native browser acceptance."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from unittest import mock

import pytest

from tools import apply_restored_patches as arp
from tools import fetch_upstream_cache as fetch
from tools import macos_restored_generators as gen
from tools import macos_runtime as runtime
from tools import platform_pins
from tools import restore_upstream_cache as restore
from tools.tests import test_macos_restored_generators as generator_fixtures
from tools.tests import test_macos_toolchain as toolchain_fixtures
from tools.tests import test_restore_upstream_cache as restore_fixtures
from tools.tests import test_mac154_restore_links as link_fixtures

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/macos156_generators.json").read_text())
NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


@pytest.mark.parametrize("arch,artifact,size", [("x64", 13284623799, 12645326109),
                                               ("arm64", 13269271368, 11005392438)])
def test_exact_attempt_three_successful_donor(arch, artifact, size):
    pin, manifest = fetch.load_manifest("macos", arch, root=ROOT)
    assert (pin["chromium_version"], pin["ungoogled_commit"], pin["head_sha"]) == gen.PINS156
    assert (pin["run_id"], pin["run_attempt"]) == (41595328070, 3)
    assert manifest["run_attempt"] == 3
    assert pin["artifact"]["id"] == artifact
    assert pin["artifact"]["size_in_bytes"] == size
    assert pin["artifact"]["inner_archive"] == "build_src.tar.zst"
    assert pin["source_roots"] == ["src"]
    fetch.validate_metadata(pin, FIXTURE["run"], FIXTURE["artifacts"][arch], now=NOW)
    for field, value in (("run_attempt", 1), ("run_attempt", 3.0), ("run_attempt", None),
                         ("head_sha", "0" * 40), ("head_branch", "master"),
                         ("conclusion", "failure"), ("status", "in_progress"),
                         ("path", ".github/workflows/building.yml")):
        with pytest.raises(fetch.CacheMiss):
            fetch.validate_metadata(pin, dict(FIXTURE["run"], **{field: value}),
                                    FIXTURE["artifacts"][arch], now=NOW)
    for field, value in (("id", 1), ("size_in_bytes", size + 1), ("expired", True),
                         ("digest", "sha256:" + "0" * 64),
                         ("expires_at", "2026-11-07T00:00:00Z")):
        with pytest.raises(fetch.CacheMiss):
            fetch.validate_metadata(pin, FIXTURE["run"],
                                    dict(FIXTURE["artifacts"][arch], **{field: value}), now=NOW)
    identity, _, _ = restore.identities(ROOT, "macos", arch)
    assert identity["run_attempt"] == 3
    for name in (restore.MAC154_ESBUILD_LINK, restore.MAC154_ESBUILD_ALIAS):
        assert restore.is_known_external_link(name, "macos", identity)
        for key in identity:
            invalid = dict(identity)
            invalid.pop(key)
            assert not restore.is_known_external_link(name, "macos", invalid), key
            invalid = dict(identity, **{key: "unreviewed"})
            assert not restore.is_known_external_link(name, "macos", invalid), key
        assert not restore.is_known_external_link(name, "macos", dict(identity, extra=True))
        assert not restore.is_known_external_link(name, "macos", dict(identity, run_attempt=3.0))
        assert not restore.is_known_external_link(name, "linux", identity)


def test_replay_real_pristine_sources_and_ordered_upstream_sections(tmp_path):
    assert (FIXTURE["chromium_version"], FIXTURE["core"], FIXTURE["mac"]) == gen.PINS156
    assert FIXTURE["devtools"] == "8c62f844ad8e0664ff41a633bfc17c472f1c5fd6"
    assert FIXTURE["domain_targets"] == []
    for name, text in FIXTURE["pristine"].items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for entry in FIXTURE["upstream_patches"]:
        assert entry["path"] in FIXTURE["upstream_series"][entry["layer"]].splitlines()
        patch = tmp_path / "upstream.patch"
        patch.write_text(entry["text"])
        result = subprocess.run([shutil.which("gpatch") or "patch", *arp.PATCH_OPTIONS,
                                 "--input", str(patch)], cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not re.search(r"fuzz|FAILED", result.stdout + result.stderr, re.I)
    for name, text in FIXTURE["sources"].items():
        assert (tmp_path / gen.DEVTOOLS / name).read_text() == text
    bindgen = tmp_path / runtime.BINDGEN_WRAPPER
    assert bindgen.read_text() == FIXTURE["upstream_sources"][runtime.BINDGEN_WRAPPER]["text"]
    runtime.repair_bindgen_wrapper(tmp_path, "arm64")
    assert hashlib.sha256(bindgen.read_bytes()).hexdigest() == runtime.BINDGEN_REPAIRS[gen.PINS156[0]]["patched_sha256"]
    runtime.repair_bindgen_wrapper(tmp_path, "arm64")


@pytest.mark.parametrize("version,pins", [("155", gen.PINS155), ("156", gen.PINS156)])
@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_real_generator_preimages_repair_idempotence_and_probe_failure(version, pins, arch):
    helper = generator_fixtures.MacGeneratorsTest()
    helper.setUp()
    try:
        fixture = json.loads((Path(__file__).parent / f"fixtures/macos{version}_generators.json").read_text())
        for name, text in fixture["sources"].items():
            helper.write(gen.DEVTOOLS + "/" + name, text.encode())
        helper.install(arch)
        with mock.patch.object(platform_pins, "load_pins", return_value=dict(zip(
                ("ChromiumVersion", "UngoogledCommit", "UngoogledMacOSCommit"), pins))):
            before = gen.prepare(helper.src, host_arch=arch, repo=helper.work)
            assert before["repair_needed"] and not before["install_needed"]
            with mock.patch.object(gen, "smoke", side_effect=ValueError("native probe failed")):
                with pytest.raises(ValueError, match="native probe failed"):
                    gen.prepare(helper.src, host_arch=arch, repo=helper.work, repair=True)
            for name, text in fixture["sources"].items():
                assert (helper.src / gen.DEVTOOLS / name).read_text() == text
            with mock.patch.object(gen, "smoke", return_value={"fixture": "not a native probe"}):
                after = gen.prepare(helper.src, host_arch=arch, repo=helper.work, repair=True)
                assert not after["repair_needed"]
                assert gen.prepare(helper.src, host_arch=arch, repo=helper.work, repair=True) == after
            for name, digest in fixture["repaired_sha256"].items():
                assert gen.sha256((helper.src / gen.DEVTOOLS / name).read_bytes()) == digest
            path = helper.src / gen.DEVTOOLS / "scripts/build/esbuild.js"
            path.write_bytes(path.read_bytes() + b"unknown")
            with pytest.raises(ValueError, match="unknown macOS generator source"):
                gen.prepare(helper.src, host_arch=arch, repo=helper.work)
    finally:
        helper.doCleanups()


@pytest.mark.parametrize("bad", [("156.0.8078.80", *gen.PINS156[1:]),
                                  (gen.PINS156[0], gen.PINS155[1], gen.PINS156[2]),
                                  (gen.PINS156[0], gen.PINS156[1], gen.PINS155[2])])
def test_generators_never_accept_other_versions_or_mixed_pins(bad):
    with pytest.raises(ValueError, match="exact reviewed pins"):
        gen.source_repairs(bad)


@pytest.mark.parametrize("shell", ["bash", "/bin/bash"])
def test_sdk_265_selection_replaces_stale_sdkroot_and_exports_same_sdk(shell):
    helper = toolchain_fixtures.MacOSToolchainTest()
    helper.BASH = shell
    helper.setUp()
    try:
        helper.version.write_text(gen.PINS156[0] + "\n")
        old = helper.add_xcode("Xcode_26.0.app", sdk="26.0", xcode="26.0")
        helper.add_xcode("Xcode_26.4.app", sdk="26.4", xcode="26.4")
        helper.add_xcode("WrongXcode.app", sdk="26.5", xcode="26.4")
        selected = helper.add_xcode("Xcode_26.5.app", sdk="26.5", xcode="26.5")
        helper.config["current"] = str(old)
        helper.env["SDKROOT"] = "/stale/old.sdk"
        result = helper.run_helper()
        assert result.returncode == 0, result.stderr
        sdk = helper.config["xcodes"][str(selected)]["path"]
        assert helper.github_env.read_text() == f"DEVELOPER_DIR={selected}\nSDKROOT={sdk}\n"
        result = helper.run_helper(github=False, script='source "$1"; select_macos_xcode; printf "%s" "$SDKROOT"')
        assert result.returncode == 0 and result.stdout == sdk
    finally:
        helper.doCleanups()


@pytest.mark.parametrize("sdk,xcode", [("26", "26.5"), ("26.4", "26.5"),
                                       ("26.5", "26.4"), ("26.5", "invalid")])
def test_sdk_265_contract_fails_closed(sdk, xcode):
    helper = toolchain_fixtures.MacOSToolchainTest()
    helper.setUp()
    try:
        helper.version.write_text(gen.PINS156[0] + "\n")
        helper.add_xcode(sdk=sdk, xcode=xcode)
        result = helper.run_helper()
        assert result.returncode != 0
        assert not helper.github_env.exists()
    finally:
        helper.doCleanups()


def test_mac156_preparation_refuses_cold_source_before_fetch_or_selector(tmp_path):
    result = subprocess.run(["bash", str(ROOT / "build/prepare-ungoogled.sh"),
                             str(tmp_path / "work"), "macos", "arm64"],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "cold fallback forbidden" in result.stderr
    assert not (tmp_path / "work/tooling").exists()


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_fetch_uses_attempt_endpoint_and_rejects_failure_before_download(tmp_path, arch):
    pin, _ = fetch.load_manifest("macos", arch, root=ROOT)
    client = mock.Mock()
    client.json.side_effect = [FIXTURE["run"], FIXTURE["artifacts"][arch]]
    client.download.side_effect = fetch.CacheMiss("metadata_verified")
    with mock.patch.object(fetch, "require_space"):
        result = fetch.fetch("macos", arch, tmp_path / "cache", root=ROOT, client=client)
    assert result["reason"] == "metadata_verified"
    base = "/repos/ungoogled-software/ungoogled-chromium-macos/actions"
    assert client.json.call_args_list == [
        mock.call(base + "/runs/41595328070/attempts/3"),
        mock.call(base + f"/artifacts/{pin['artifact']['id']}"),
    ]
    client.download.assert_called_once()
    client = mock.Mock()
    client.json.side_effect = [dict(FIXTURE["run"], conclusion="failure"), FIXTURE["artifacts"][arch]]
    result = fetch.fetch("macos", arch, tmp_path / "failed", root=ROOT, client=client)
    assert result["status"] == "miss" and result["reason"] == "untrusted_run"
    client.download.assert_not_called()


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_full_mac156_restore_keeps_objects_and_exact_external_link_receipts(arch):
    import io
    import tempfile
    helper = restore_fixtures.RestoreUpstreamCacheTest()
    with mock.patch.object(tempfile, "tempdir", str(Path(tempfile.gettempdir()).resolve())):
        helper.setUp()
    try:
        helper.make_cache("macos", arch)
        identity, _, _ = restore.identities(helper.repo, "macos", arch)
        assert identity["run_attempt"] == 3
        archive = io.BytesIO(restore_fixtures.archive_bytes(link_fixtures.links()))
        metadata = fetch.extract_tar(archive, helper.cache / "tree",
                                     fetch.SourceSelection(["src"], platform="macos"))
        helper.result.update(metadata)
        helper.write(helper.cache / "result.json", json.dumps(helper.result))
        result = helper.invoke()
        assert result["status"] == "hit", result
        assert result["receipt"]["identity"] == identity
        assert set(result["receipt"]["external_symlink_paths"]) == {
            link_fixtures.MODULE, link_fixtures.ALIAS}
        assert (helper.work / "src/out/Default/obj/output.o").read_bytes() == b"object"
        assert helper.invoke("verify", cache=False)["receipt"] == result["receipt"]
        marker = helper.work / "src" / restore.MARKER
        receipt = json.loads(marker.read_text())
        receipt["identity"]["run_attempt"] = 1
        marker.write_text(json.dumps(receipt))
        with pytest.raises(restore.Miss):
            restore.verify_restored(helper.work, "macos", arch, repo=helper.repo)
    finally:
        helper.doCleanups()


@pytest.mark.parametrize("arch,host", [("arm64", "x86_64"), ("x64", "arm64")])
def test_native_architecture_is_required_before_any_tool_execution(tmp_path, arch, host):
    import os
    binary = tmp_path / "uname"
    binary.write_text('#!/bin/sh\ncase "$1" in -m) echo ' + host + ';; -s) echo Darwin;; esac\n')
    binary.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    for script, args in (("build/macos/build.sh", [str(tmp_path / "work"), arch]),
                         ("build/posix/prepare-restored-tools.sh", [str(tmp_path / "work"), "macos", arch])):
        result = subprocess.run(["bash", str(ROOT / script), *args], capture_output=True, text=True, env=env)
        assert result.returncode != 0
        assert "native macos" in result.stderr or "native macOS" in result.stderr
        assert not (tmp_path / "work").exists()


@pytest.mark.parametrize("value", [None, 0, -1, True, 3.0, "3"])
def test_manifest_requires_integer_attempt_for_mac156(tmp_path, value):
    for name in ("CHROMIUM_VERSION", "CHROMIUM_LINUX_VERSION", "CHROMIUM_MACOS_VERSION",
                 "CHROMIUM_WINDOWS_VERSION", "build/ungoogled-revisions.psd1", "build/upstream-cache.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    path = tmp_path / "build/upstream-cache.json"
    manifest = json.loads(path.read_text())
    if value is None:
        del manifest["sources"]["macos"]["run_attempt"]
    else:
        manifest["sources"]["macos"]["run_attempt"] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(fetch.CacheMiss):
        fetch.load_manifest("macos", "x64", root=tmp_path)


@pytest.mark.parametrize("attempt", [None, 1])
def test_importer_rejects_missing_or_mismatched_attempt_before_using_donor(attempt):
    from tools.tests import test_import_upstream_cache as fixtures
    import tempfile
    helper = fixtures.ImportUpstreamCacheTest()
    with mock.patch.object(tempfile, "tempdir", str(Path(tempfile.gettempdir()).resolve())):
        helper.setUp()
    try:
        helper.prepare("macos")
        if attempt is None:
            del helper.result["manifest"]["run_attempt"]
        else:
            helper.result["manifest"]["run_attempt"] = attempt
        helper.save_receipt()
        result = helper.run_phase()
        helper.assert_miss(result, "downloader pinned identity mismatch: run_attempt")
    finally:
        helper.doCleanups()
