"""Exact Linux157 downstream identities and authenticated platform replay."""
from __future__ import annotations

import ast
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from unittest import mock

import pytest

from tools import linux157_source as source
from tools import linux157_toolchain as tools
from tools import platform_pins
from tools import fetch_upstream_cache as cache

ROOT = source.ROOT
FIXTURE_PATH = ROOT / "tools/tests/fixtures/linux157_platform_sources.json.gz"
FIXTURE_RAW = gzip.decompress(FIXTURE_PATH.read_bytes())
assert hashlib.sha256(FIXTURE_RAW).hexdigest() == "48b030b7ac3a4023270c6268602e8c6856fe13b44afd17b6d32e701225b1711f"
FIXTURE = json.loads(FIXTURE_RAW)


def clone_recipe(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "build/linux/157.0.8103.78", root / "build/linux/157.0.8103.78")
    (root / "tools").mkdir()
    lite = root / source.LITE_PATH
    lite.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / source.LITE_PATH, lite)
    for name in ("linux157_source.py", "linux157_toolchain.py", "linux157_build.py",
                 "linux157_gn.py", "linux157_checkpoint.py"):
        shutil.copy2(ROOT / "tools" / name, root / "tools" / name)
    return root


def fixture_tree(root):
    src = root / "src"
    tooling = root / "platform"
    for name, text in FIXTURE["sources"].items():
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for name, text in FIXTURE["upstream_patches"].items():
        path = tooling / "patches" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (tooling / "patches/series").write_text(FIXTURE["upstream_series"])
    for name, text in FIXTURE["platform_inputs"].items():
        path = tooling / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return src, tooling


def test_exact_downstream_pin_not_a_new_platform_tag():
    pins = platform_pins.load_pins(ROOT, "linux")
    manifest = source.validate_pins(pins)
    assert pins["ChromiumVersion"] == "157.0.8103.78"
    assert pins["UngoogledLinuxVersion"] == "156.0.8078.79-1"
    assert manifest["platform_core_gitlink"] != manifest["core_commit"]
    assert manifest["official_platform_tag"] is None
    identity = source.recipe()
    assert identity["core_override"] == source.CORE
    assert identity["platform_ref"] == source.BASE_REF
    for arch in ("x64", "arm64"):
        with pytest.raises(cache.CacheMiss, match="source_unavailable"):
            cache.load_manifest("linux", arch, root=ROOT)


@pytest.mark.parametrize("field", ["ChromiumVersion", "UngoogledVersion", "UngoogledCommit",
                                   "UngoogledLinuxVersion", "UngoogledLinuxCommit"])
def test_no_arbitrary_override_exception(field):
    pins = platform_pins.load_pins(ROOT, "linux")
    pins[field] += "0"
    with pytest.raises(ValueError, match="pins"):
        source.validate_pins(pins)


@pytest.mark.parametrize("kind", ["manifest", "patch", "extra", "symlink", "directory-link"])
def test_manifest_and_inventory_fail_closed(tmp_path, kind):
    repo = clone_recipe(tmp_path)
    manifest = source.load_manifest(repo)
    patch = repo / manifest["patches"][0]["path"]
    if kind == "manifest":
        path = repo / source.MANIFEST
        path.write_bytes(path.read_bytes() + b"\n")
    elif kind == "patch":
        patch.write_bytes(patch.read_bytes() + b"changed")
    elif kind == "extra":
        (patch.parent / "extra.patch").write_text("unreviewed")
    elif kind == "symlink":
        original = repo / "original.patch"
        patch.rename(original)
        patch.symlink_to(original)
    else:
        directory = patch.parent
        original = directory.with_name("linked")
        directory.rename(original)
        directory.symlink_to(original)
    with pytest.raises(ValueError):
        source.load_manifest(repo)


def test_real_all_platform_patches_apply_in_order_with_exact_inputs(tmp_path):
    src, tooling = fixture_tree(tmp_path)
    manifest = source.load_manifest()
    assert len(manifest["patches"]) == 9
    result = source.apply_platform(src, tooling)
    assert [x["patch"] for x in result["platform_patches"]] == [x["path"] for x in manifest["patches"]]
    source.hashes(src, manifest["final_outputs"])
    assert not (src / source.IN_PROGRESS).exists()
    assert source.verify_receipt(src)["upstream_cache"] is False
    for name in manifest["final_outputs"]:
        if name.endswith(".py"):
            ast.parse((src / name).read_text())
    assert 'GetHostSysrootPlatform())}' in (src / "tools/rust/build_rust.py").read_text()
    assert 'use_typescript_go' in (src / "third_party/devtools-frontend/src/scripts/build/typescript/typescript_vars.gni").read_text()
    web_package = (src / "components/web_package/BUILD.gn").read_text()
    assert "signed_web_bundles_rust_bindings" in web_package
    assert "web_bundle_parser_factory.cc" in web_package
    blink = (src / "third_party/blink/renderer/platform/BUILD.gn").read_text()
    assert '"fonts:ift_patcher_bindings"' in blink
    assert '"//third_party/node:check_version"' in (src / "third_party/node/node.gni").read_text()
    with pytest.raises(ValueError, match="already"):
        source.apply_platform(src, tooling)


def test_mismatch_leaves_failure_snapshot_and_no_success_receipt(tmp_path):
    src, tooling = fixture_tree(tmp_path)
    (src / "google_apis/api_key_cache.cc").write_text("unknown")
    with mock.patch.object(subprocess, "check_output", return_value="GNU patch 2.8"), \
            mock.patch.object(subprocess, "run") as run:
        with pytest.raises(ValueError, match="source identity"):
            source.apply_platform(src, tooling)
        run.assert_not_called()
    assert (src / source.IN_PROGRESS).is_file()
    assert (src / ".chromix-linux157-failure.json").is_file()
    assert not (src / source.RECEIPT).exists()


def test_changed_old_platform_blob_is_never_accepted(tmp_path):
    src, tooling = fixture_tree(tmp_path)
    path = tooling / "patches" / source.load_manifest()["patches"][1]["upstream_path"]
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="upstream platform patch"):
        source.apply_platform(src, tooling)
    assert not (src / source.IN_PROGRESS).exists()


def test_gitlink_override_checks_actual_commits(tmp_path):
    src, tooling = fixture_tree(tmp_path)
    core = tmp_path / "core"
    core.mkdir()
    (core / "chromium_version.txt").write_text(source.VERSION)
    (core / "revision.txt").write_text("1")
    def git(path, *args):
        if args == ("rev-parse", "HEAD"):
            return source.CORE if path == core else source.PLATFORM
        if args[0] in ("status", "ls-files"):
            return ""
        return "160000 commit " + source.BASE_CORE + "\tungoogled-chromium"
    original_hashes = source.hashes
    def hashes(root, expected):
        if root != core:
            original_hashes(root, expected)
    with mock.patch.object(source, "git", side_effect=git), mock.patch.object(source, "hashes", side_effect=hashes):
        assert source.get_platform(core, tooling)["core_override"] == source.CORE
    with mock.patch.object(source, "git", return_value="wrong"):
        with pytest.raises(ValueError, match="checkout"):
            source.get_platform(core, tooling)


def test_native_gate_rejects_macos_before_execution(tmp_path):
    with mock.patch.object(tools.platform, "system", return_value="Darwin"), \
            mock.patch.object(subprocess, "run") as run:
        with pytest.raises(ValueError, match="Linux x64"):
            tools.prepare(tmp_path, "arm64")
        run.assert_not_called()


@pytest.mark.parametrize("name,kind", [("../escape", "file"), ("/absolute", "file"),
                                       ("package/link", "symlink"), ("package/hard", "hardlink")])
def test_unsafe_tool_archive_rejected(tmp_path, name, kind):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo(name)
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "/tmp/escape"
        elif kind == "hardlink":
            info.type = tarfile.LNKTYPE
            info.linkname = "../escape"
        output.addfile(info, io.BytesIO())
    with pytest.raises(ValueError):
        tools.unpack(archive, tmp_path / "out")


def test_download_identity_and_partial_collision_fail_closed(tmp_path):
    raw = b"official bytes"
    pin = {"url": "https://example.invalid/exact", "size": len(raw),
           "sha256": hashlib.sha256(raw).hexdigest()}
    dest = tmp_path / "archive"
    with mock.patch.object(tools.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
        assert tools.download(pin, dest).read_bytes() == raw
    dest.write_bytes(b"changed")
    with pytest.raises(ValueError, match="does not match"):
        tools.download(pin, dest)


def test_no_shared_pipeline_or_restored_generator_route():
    text = (ROOT / "tools/linux157_build.py").read_text()
    assert 'runtime_verified": False' in text
    assert "upstream_cache" not in (ROOT / "tools/linux157_toolchain.py").read_text().split("def prepare", 1)[0]
    assert "linux_restored_generators" not in text
    assert "prepare-restored-tools" not in text
    assert "snapshot_valid_for_retry" in text
