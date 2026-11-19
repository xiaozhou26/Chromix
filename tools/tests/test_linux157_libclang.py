"""Exact official Linux libclang inventory, real metagen and post-GN gates."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import tarfile
from unittest import mock

import pytest

from tools import linux157_libclang as libclang
from tools import linux157_postgn as postgn
from tools import linux157_source as source


def mini_manifest():
    raw = b"known binding"
    return {"files": {"bindings/python/clang/cindex.py": {"size": len(raw), "sha256": source.digest(raw)}}}, raw


def test_official_157_package_is_not_156_or_windows_23():
    m = libclang.load()
    assert m["download"]["sha256"] == "1472e8a5cfc4959f94ff18aa5ec864b07991d877995a563eeb4176e344a7fa59"
    assert "Linux_x64/rust-libclang-" in m["download"]["url"]
    assert "-1125-llvmorg-24-" in m["download"]["url"]
    assert len(m["files"]) == 16
    assert "lib/libclang.so" in m["files"]
    assert "bindings/python/clang/__init__.py" in m["files"]


def test_missing_bindings_can_be_identified_without_accepting_tamper(tmp_path):
    manifest, raw = mini_manifest()
    assert libclang.verify_package(tmp_path, manifest, allow_missing=True) == ["bindings/python/clang/cindex.py"]
    with pytest.raises(ValueError, match="missing"):
        libclang.verify_package(tmp_path, manifest)
    path = tmp_path / "bindings/python/clang/cindex.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    assert libclang.verify_package(tmp_path, manifest) == []
    path.write_bytes(raw + b"bad")
    with pytest.raises(ValueError, match="changed"):
        libclang.verify_package(tmp_path, manifest, allow_missing=True)


@pytest.mark.parametrize("attack", ["symlink", "extra", "hardlink", "directory"])
def test_package_rejects_untrusted_layout(tmp_path, attack):
    manifest, raw = mini_manifest()
    p = tmp_path / "bindings/python/clang/cindex.py"
    p.parent.mkdir(parents=True)
    p.write_bytes(raw)
    if attack == "symlink":
        p.unlink();p.symlink_to(tmp_path / "absent")
    elif attack == "extra":
        (tmp_path / "unknown.py").write_bytes(raw)
    elif attack == "hardlink":
        (tmp_path / "hard").hardlink_to(p)
    else:
        p.unlink();p.mkdir()
    with pytest.raises(ValueError):
        libclang.verify_package(tmp_path, manifest, allow_missing=True)


def test_archive_hash_rejected_before_extract(tmp_path):
    path = tmp_path / "archive.tar.xz"
    path.write_bytes(b"bad")
    with pytest.raises(ValueError, match="archive identity"):
        libclang.extract(path, tmp_path / "out", libclang.load())
    assert not (tmp_path / "out").exists()


def test_archive_unknown_members_rejected_even_if_container_checksum_known(tmp_path):
    p = tmp_path / "payload.tar.xz"
    with tarfile.open(p, "w:xz") as t:
        m = tarfile.TarInfo("../escape")
        m.size = 1
        t.addfile(m, io.BytesIO(b"x"))
    manifest = {"download": {"size": p.stat().st_size, "sha256": source.digest(p.read_bytes())}, "files": {}}
    with pytest.raises(ValueError, match="inventory"):
        libclang.extract(p, tmp_path / "out", manifest)
    assert not (tmp_path / "escape").exists()


def test_ninja_generated_input_is_not_an_unpinned_package_exception(tmp_path):
    out = tmp_path / "out/Chromix"
    out.mkdir(parents=True)
    (tmp_path / "script.py").write_text("input")
    path = out / "toolchain.ninja"
    path.write_text('build v8_build_config.json: producer\n'
                    'build gen/v8/metagen/instance-types.h: action | ../../script.py v8_build_config.json phony/v8/config\n')
    result = libclang.check_ninja_inputs(tmp_path)
    assert result["generated_by_ninja"] == ["v8_build_config.json"]
    path.write_text(path.read_text().replace("../../script.py", "../../missing.py"))
    with pytest.raises(ValueError, match="missing.py"):
        libclang.check_ninja_inputs(tmp_path)


def test_real_official_package_and_metagen(tmp_path):
    archive = os.environ.get("CHROMIX_REAL_LIBCLANG_ARCHIVE")
    src = os.environ.get("CHROMIX_REAL_METAGEN_SRC")
    if not archive or not src:
        pytest.skip("requires exact official package and Linux157 source on Linux x64")
    m = libclang.load()
    libclang.extract(Path(archive), tmp_path / "package", m)
    assert libclang.verify_package(tmp_path / "package", m) == []
    report = libclang.prepare(Path(src))
    assert report["probe"]["version"] == m["clang_version"]
    assert report["probe"]["metagen"] == "instance-types-and-layouts-passed"
    assert report["ninja_inputs"]["missing"] == []


def test_postgn_donor_is_latest_and_has_generated_graph():
    expected = postgn.expected("arm64")
    assert expected["run_id"] == 44567785761
    assert expected["artifacts"][0]["id"] == 14554286193
    d = postgn.load()["donors"]["arm64"]
    assert d["files"]["src/out/Chromix/build.ninja"]
    assert d["files"]["src/out/Chromix/.ninja_log"] is None
    assert postgn.expected("x64")["run_id"] == 44567777792
    assert postgn.expected("x64")["artifacts"][0]["id"] == 14554252116


def test_tool_only_delta_cannot_modify_previous_gn_recipe_or_patches():
    old = postgn.load()["donors"]["arm64"]["source_receipt"]["recipe"]
    new = source.recipe()
    postgn.delta(old, new)
    bad = copy.deepcopy(new)
    bad["inputs"]["tools/linux157_checkpoint.py"] = "0" * 64
    with pytest.raises(ValueError, match="only"):
        postgn.delta(old, bad)


@pytest.mark.parametrize("field", ["run_id", "stage", "attempt", "head_sha"])
def test_wrong_postgn_metadata_cannot_touch_source(tmp_path, field):
    m = postgn.expected("arm64")
    m[field] = "wrong" if field == "head_sha" else m[field] + 1
    with pytest.raises(ValueError, match="metadata"):
        postgn.admit(tmp_path, "arm64", m)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_real_postgn_metadata_from_audit(arch):
    audit = os.environ.get("CHROMIX_POSTGN_AUDIT")
    if not audit:
        pytest.skip("requires exact latest GitHub metadata audit")
    root = Path(audit)
    prefix = "x64-" if arch == "x64" else ""
    class Client:
        def get(self, path):
            return json.loads((root / (prefix + "run.json")).read_text())
        def items(self, path, key):
            name = "jobs.json" if key == "jobs" else "artifacts.json"
            return json.loads((root / (prefix + name)).read_text())[key]
    assert postgn.validate_metadata(Client(), arch) == postgn.expected(arch)


def test_wrong_host_rejects_before_package_install(tmp_path):
    manifest = libclang.load()
    (tmp_path / "DEPS").write_text(manifest["download"]["sha256"] + " " + manifest["download"]["url"].rsplit("/", 1)[1])
    with mock.patch.object(source, "hashes"), \
            mock.patch.object(libclang.platform, "system", return_value="Linux"), \
            mock.patch.object(libclang.platform, "machine", return_value="aarch64"), \
            mock.patch.object(libclang.toolchain, "download") as download:
        with pytest.raises(ValueError, match="Linux x64"):
            libclang.install(tmp_path)
        download.assert_not_called()


def test_probe_failure_preserves_inprogress_and_blocks_checkpoint(tmp_path):
    with mock.patch.object(libclang, "install", side_effect=ValueError("checksum failure")):
        with pytest.raises(ValueError, match="checksum"):
            libclang.prepare(tmp_path)
    assert (tmp_path / libclang.IN_PROGRESS).is_file()
    assert not (tmp_path / ".chromix-linux157-libclang.json").exists()
    with pytest.raises(ValueError, match="interrupted"):
        libclang.prepare(tmp_path)
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / libclang.IN_PROGRESS).touch()
    with pytest.raises(ValueError, match="interrupted"):
        postgn.verify(work, "arm64")


@pytest.mark.parametrize("arch", ["x64", "arm64"])
@pytest.mark.parametrize("mutation", ["path", "head", "attempt", "artifact", "expired"])
def test_live_metadata_rejects_other_workflow_or_donor(arch, mutation):
    audit = os.environ.get("CHROMIX_POSTGN_AUDIT")
    if not audit:
        pytest.skip("requires exact metadata audit")
    root = Path(audit);prefix = "x64-" if arch == "x64" else ""
    run = json.loads((root / (prefix + "run.json")).read_text())
    jobs = json.loads((root / (prefix + "jobs.json")).read_text())["jobs"]
    artifacts = json.loads((root / (prefix + "artifacts.json")).read_text())["artifacts"]
    if mutation == "path":run["path"] = ".github/workflows/build-linux-arm64.yml"
    elif mutation == "head":run["head_sha"] = "0" * 40
    elif mutation == "attempt":run["run_attempt"] = 2
    else:
        artifact = next(a for a in artifacts if "-tree-" in a["name"])
        if mutation == "artifact":artifact["digest"] = "sha256:" + "0" * 64
        else:artifact["expired"] = True
    class Client:
        def get(self, path):return run
        def items(self, path, key):return jobs if key == "jobs" else artifacts
    with pytest.raises(ValueError):postgn.validate_metadata(Client(), arch)


def test_libclang_source_identity_checks_all_metagen_modules():
    m = libclang.load()
    for name in ("clang_bootstrap.py", "compile_flags.py", "cpp_hier.py", "extract.py",
                 "instance_types.py", "layout_extract.py", "layout_ir.py", "metagen.py", "harvest-driver.cc"):
        assert "v8/tools/metagen/" + name in m["source_inputs"]
    assert libclang.RESOURCE + "/include/stddef.h" in m["source_inputs"]
    build = (source.ROOT / "tools/linux157_build.py").read_text()
    assert 'env["PATH"] = str(out) + os.pathsep + env["PATH"]' in build
    assert 'env["PYTHONDONTWRITEBYTECODE"] = "1"' in build
    assert "libclang.prepare(src, repo)" in build
