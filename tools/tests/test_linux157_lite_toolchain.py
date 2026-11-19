"""Official lite omissions, exact 3pp restoration and fail-closed checkpoints."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from tools import linux157_source as source
from tools import linux157_toolchain as tools
from tools import linux157_build as build
from tools import linux157_ua as checkpoint


def lite_inputs(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    inputs = {tools.TSGO_PATCH_PATH: tools.TSGO_PATCH_SHA256}
    for name in ("tools/rust/update_rust.py", "tools/clang/scripts/update.py", "DEPS"):
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = ("verified fixture for " + name + "\n").encode()
        path.write_bytes(raw)
        inputs[name] = source.digest(raw)
    return src, {"toolchain_inputs": inputs}


def test_official_blob_identity_not_an_assumed_full_checkout():
    blob = tools.TSGO_PATCH
    assert len(blob) == 2747
    assert source.digest(blob) == "00150dd2e4944be353977639bcd1313786d9f839e84f1564ca983d841dab00de"
    assert hashlib.sha1(b"blob " + str(len(blob)).encode() + b"\0" + blob).hexdigest() == (
        "f43b2d7b72858c118777dfd394d9edc28f6e49fe")
    manifest = source.load_manifest()
    assert manifest["toolchain_inputs"][tools.TSGO_PATCH_PATH] == tools.TSGO_PATCH_SHA256
    assert "tools/linux157_toolchain.py" in source.recipe()["inputs"]


def test_fresh_lite_without_entire_3pp_directory_recovers_exact_patch(tmp_path):
    src, manifest = lite_inputs(tmp_path)
    assert not (src / "third_party/typescript/linux-amd64/3pp").exists()
    with mock.patch.object(tools.urllib.request, "urlopen") as network:
        result = tools.preflight_inputs(src, manifest)
        network.assert_not_called()
    patch = source.regular(src, tools.TSGO_PATCH_PATH)
    assert patch.read_bytes() == tools.TSGO_PATCH
    assert result["restored"] == [tools.TSGO_PATCH_PATH]
    before = patch.stat().st_mtime_ns
    assert tools.preflight_inputs(src, manifest)["restored"] == []
    assert patch.stat().st_mtime_ns == before


@pytest.mark.parametrize("kind", ["modified", "symlink", "dangling", "hardlink", "directory", "parent-link"])
def test_unknown_or_linked_patch_never_overwritten(tmp_path, kind):
    src, manifest = lite_inputs(tmp_path)
    path = src / tools.TSGO_PATCH_PATH
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(tools.TSGO_PATCH)
    if kind == "modified":
        path.write_bytes(tools.TSGO_PATCH + b"changed")
    elif kind == "symlink":
        path.symlink_to(outside)
    elif kind == "dangling":
        path.symlink_to(tmp_path / "missing")
    elif kind == "hardlink":
        path.hardlink_to(outside)
    elif kind == "directory":
        path.mkdir()
    else:
        path.parent.rmdir()
        path.parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        tools.preflight_inputs(src, manifest)
    assert outside.read_bytes() == tools.TSGO_PATCH


def test_all_missing_and_changed_inputs_reported_before_any_repair(tmp_path):
    src, manifest = lite_inputs(tmp_path)
    (src / "tools/rust/update_rust.py").unlink()
    (src / "tools/clang/scripts/update.py").write_text("untrusted")
    (src / "DEPS").unlink()
    with pytest.raises(ValueError) as caught:
        tools.preflight_inputs(src, manifest)
    for name in ("tools/rust/update_rust.py", "tools/clang/scripts/update.py", "DEPS"):
        assert name in str(caught.value)
    assert not (src / tools.TSGO_PATCH_PATH).exists()


def test_readonly_checkpoint_does_not_silently_repair_input(tmp_path):
    src, manifest = lite_inputs(tmp_path)
    result = tools.preflight_inputs(src, manifest, restore=False)
    assert result["restored"] == []
    assert result["recoverable_missing"] == [tools.TSGO_PATCH_PATH]
    assert not (src / tools.TSGO_PATCH_PATH).exists()


def test_unknown_pinned_payload_rejected_before_writing(tmp_path):
    src, manifest = lite_inputs(tmp_path)
    manifest["toolchain_inputs"][tools.TSGO_PATCH_PATH] = "0" * 64
    with pytest.raises(ValueError, match="payload"):
        tools.preflight_inputs(src, manifest)
    assert not (src / tools.TSGO_PATCH_PATH).exists()


@pytest.mark.parametrize("valid", [True, False])
def test_failure_metadata_uses_same_fresh_checkpoint_gate_even_before_gn(tmp_path, valid):
    outcome = {"status": "verified"} if valid else ValueError("incomplete source")
    with mock.patch.object(build, "build", side_effect=ValueError("tool download failed")), \
            mock.patch.object(source, "checkpoint", side_effect=None if valid else outcome,
                              return_value=outcome if valid else None) as gate:
        assert build.main(["--workdir", str(tmp_path), "--arch", "arm64"]) == 1
    gate.assert_called_once_with(tmp_path.resolve(), "arm64", source.ROOT)
    report = json.loads((tmp_path / "linux157-failed.json").read_text())
    assert report["snapshot_valid_for_retry"] is valid
    assert report["runtime_verified"] is False
    assert report["checkpoint_error"] == (None if valid else "incomplete source")
    assert not (tmp_path / "src/out/Chromix/build.ninja").exists()


def test_partial_source_rejected_without_snapshot_admission(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / source.IN_PROGRESS).write_text("interrupted patching")
    with mock.patch.object(source, "validate_route"), mock.patch.object(source, "get_platform") as origin:
        with pytest.raises(ValueError):
            source.checkpoint(tmp_path, "x64")
    origin.assert_not_called()


def test_real_lite_archive_inputs_and_official_tsgo_patch(tmp_path):
    audit = os.environ.get("CHROMIX_LINUX157_LITE_AUDIT")
    package = os.environ.get("CHROMIX_LINUX157_TSGO_ARCHIVE")
    if not audit or not package:
        pytest.skip("requires independently hash-verified official lite audit and tsgo archive")
    audit = Path(audit)
    report = json.loads((audit / "report.json").read_text())
    manifest = source.load_manifest()
    assert report["verified_sha256"] == manifest["source_archive"]["sha256"]
    assert report["verified_bytes"] == manifest["source_archive"]["size"]
    assert report["missing"] == []
    src = tmp_path / "src"
    shutil.copytree(audit / "files", src)
    source.hashes(src, manifest["toolchain_inputs"])
    # Reproduce core's contingent deletion of the whole architecture directory.
    shutil.rmtree(src / "third_party/typescript/linux-amd64")
    recovered = tools.preflight_inputs(src, manifest)
    assert recovered["restored"] == [tools.TSGO_PATCH_PATH]
    source.hashes(src, manifest["toolchain_inputs"])
    package = Path(package)
    assert tools.file_hash(package) == manifest["downloads"]["tsgo"]["sha256"]
    stage = tmp_path / "tsgo"
    tools.unpack(package, stage, "package/")
    program = shutil.which("gpatch") or shutil.which("patch")
    subprocess.run([program, *source.PATCH_OPTIONS, "-i", str(src / tools.TSGO_PATCH_PATH)],
                   cwd=stage, check=True)
    dom = (stage / "lib/lib.dom.d.ts").read_text()
    assert "set innerHTML(html: string|TrustedHTML)" in dom
    assert "set src(url: string|TrustedScriptURL)" in dom


def test_recovery_only_source_never_prunes_or_downloads_again():
    text = (source.ROOT / "tools/linux157_source.py").read_text()
    assert "fresh preparation is disabled" in text
    preparation = text.split("def prepare(", 1)[1].split("def validate_route", 1)[0]
    assert "verify_migrated(work, arch, repo)" in preparation
    assert "prune_binaries.py" not in preparation
    assert "download(" not in preparation


def test_toolchain_missing_inputs_stop_before_any_download(tmp_path):
    src, manifest = lite_inputs(tmp_path)
    (src / "DEPS").unlink()
    with mock.patch.object(tools.platform, "system", return_value="Linux"), \
            mock.patch.object(tools.platform, "machine", return_value="x86_64"), \
            mock.patch.dict(os.environ, {"CDS_CLANG_BUCKET_OVERRIDE": ""}), \
            mock.patch.object(source, "verify_receipt", return_value={
                "stage": "chromix-applied", "target_arch": "x64"}), \
            mock.patch.object(source, "load_manifest", return_value=manifest), \
            mock.patch.object(tools, "download") as download:
        with pytest.raises(ValueError, match="DEPS"):
            tools.prepare(src, "x64")
        download.assert_not_called()


def test_checkpoint_rejects_recoverable_but_not_yet_restored_inputs(tmp_path):
    from tools import patch_selection
    src = tmp_path / "src"
    src.mkdir()
    identity = source.recipe()
    (src / ".chromix-source-ready").write_text("linux157-fresh|x64|" + identity["sha256"])
    receipt = {"stage": "chromix-applied", "target_arch": "x64", "chromix_selection": {}}
    with mock.patch.object(checkpoint, "verify"), \
            mock.patch.object(source, "validate_route"), \
            mock.patch.object(source, "verify_receipt", return_value=receipt), \
            mock.patch.object(source, "get_platform"), \
            mock.patch.object(patch_selection, "select", return_value=({}, [])), \
            mock.patch.object(tools, "preflight_inputs", return_value={
                "recoverable_missing": [tools.TSGO_PATCH_PATH]}) as preflight:
        with pytest.raises(ValueError, match="checkpoint toolchain inputs"):
            source.checkpoint(tmp_path, "x64")
    assert preflight.call_args.kwargs == {"restore": False}
