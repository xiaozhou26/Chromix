"""Official clang/libc++ binding and exact Linux157 pre-GN recovery gates."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
from unittest import mock

import pytest

from tools import linux157_gn as gn
from tools import linux157_checkpoint as checkpoint
from tools import linux157_source as source


def elf(path, machine=b"\x3e\x00"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF\x02\x01" + b"\0" * 12 + machine)
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("system,machine", [("Darwin", "x86_64"), ("Linux", "aarch64"), ("Linux", "i686")])
def test_wrong_host_rejects_before_tool_execution(tmp_path, system, machine):
    with mock.patch.object(gn.platform, "system", return_value=system), \
            mock.patch.object(gn.platform, "machine", return_value=machine), \
            mock.patch.object(subprocess, "run") as run:
        with pytest.raises(ValueError, match="Linux x64"):
            gn.host_environment(tmp_path, {})
        run.assert_not_called()


def host_tree(root):
    for name in gn.INPUTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    for name in gn.BINARIES:
        elf(root / "third_party/llvm-build/Release+Asserts/bin" / name)


def test_wrong_compiler_architecture_is_rejected(tmp_path):
    host_tree(tmp_path)
    compiler = tmp_path / "third_party/llvm-build/Release+Asserts/bin/clang"
    elf(compiler, b"\xb7\x00")
    with mock.patch.object(gn.platform, "system", return_value="Linux"), \
            mock.patch.object(gn.platform, "machine", return_value="x86_64"), \
            mock.patch.object(gn.tools, "file_hash", side_effect=lambda p: gn.INPUTS.get(p.relative_to(tmp_path).as_posix(), "bad")), \
            mock.patch.object(subprocess, "check_output") as run:
        with pytest.raises(ValueError, match="x86_64 ELF"):
            gn.host_environment(tmp_path, {})
        run.assert_not_called()


def test_official_environment_cannot_inherit_mixed_stdlib_flags(tmp_path):
    host_tree(tmp_path)
    original = {"PATH": "/usr/bin", "CC": "gcc", "CXX": "g++", "CXXFLAGS": "-target aarch64",
                "CPATH": "/usr/include/c++/12", "LDFLAGS": "-lstdc++"}
    def digest(path):
        return gn.INPUTS.get(path.relative_to(tmp_path).as_posix(), gn.BINARIES.get(path.name))
    with mock.patch.object(gn.platform, "system", return_value="Linux"), \
            mock.patch.object(gn.platform, "machine", return_value="x86_64"), \
            mock.patch.object(gn.tools, "file_hash", side_effect=digest), \
            mock.patch.object(subprocess, "check_output", return_value="clang version 24.0.0git fixture"):
        clean, identity = gn.host_environment(tmp_path, original)
    assert original["CXX"] == "g++"
    assert clean["CXX"].endswith("/clang++")
    assert clean["CXXFLAGS"] == "-isystem" + str(tmp_path / "third_party/llvm-libc/src")
    assert "CPATH" not in clean and "LDFLAGS" not in clean
    assert identity["stdlib"] == "Chromium157 in-tree libc++/libc++abi"


def test_real_official_host_toolchain(tmp_path):
    src = os.environ.get("CHROMIX_REAL_GN_SRC")
    if not src:
        pytest.skip("requires exact restored Linux157 checkpoint on Linux x64")
    env, identity = gn.host_environment(Path(src), dict(os.environ, CXX="wrong-clang", CXXFLAGS="-target aarch64"))
    assert identity["version"].startswith("clang version 24.0.0git")
    assert env["CXXFLAGS"].endswith("third_party/llvm-libc/src")


@pytest.mark.parametrize("arch,run,artifact", [("x64", 44547858334, 14542955675), ("arm64", 44547869671, 14544060048)])
def test_only_latest_stage1_artifact_tuple(arch, run, artifact):
    value = checkpoint.expected(arch)
    assert value["run_id"] == run
    assert value["artifacts"][0]["id"] == artifact
    assert value["stage"] == value["attempt"] == 1
    assert value["head_sha"] == "e2fb7d2129c47d941e341ae8e25f503b07527bfe"
    donor = checkpoint.load()["donors"][arch]
    assert donor["files"]["src/out/Chromix/build.ninja"] is None
    assert donor["files"]["src/out/Chromix/.ninja_log"] is None
    assert donor["source_receipt"]["recipe"]["sha256"] == "0c7aa841f0fafff1e7d03a765c3693f00de1ccb2d40786f2bb5ecc425c20933c"


def test_recipe_delta_only_reviewed_host_tools():
    old = checkpoint.load()["donors"]["x64"]["source_receipt"]["recipe"]
    from tools import linux157_postgn
    new = linux157_postgn.load()["donors"]["x64"]["source_receipt"]["recipe"]
    checkpoint.validate_recipe_delta(old, new)
    changed = copy.deepcopy(new)
    changed["inputs"]["tools/linux157_toolchain.py"] = "0" * 64
    with pytest.raises(ValueError, match="delta"):
        checkpoint.validate_recipe_delta(old, changed)
    changed = copy.deepcopy(new)
    changed["core_commit"] = "0" * 40
    with pytest.raises(ValueError, match="identity"):
        checkpoint.validate_recipe_delta(old, changed)


def test_patch_changes_cannot_hide_in_recipe_migration():
    old = checkpoint.load()["donors"]["x64"]["source_receipt"]["chromix_selection"]
    new = copy.deepcopy(old)
    new["selection"]["downstream"]["recipe"] = source.recipe()
    checkpoint.compatible_selection(old, new)
    new["patches"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="226 patches"):
        checkpoint.compatible_selection(old, new)


def test_wrong_metadata_rejected_without_source_access(tmp_path):
    metadata = checkpoint.expected("x64")
    metadata["run_id"] = 44541471654
    with pytest.raises(ValueError, match="metadata"):
        checkpoint.admit(tmp_path, "x64", metadata)
    assert not list(tmp_path.iterdir())


def test_pre_gn_gate_rejects_any_compilation_output(tmp_path):
    (tmp_path / "build.ninja").write_text("already generated")
    with pytest.raises(ValueError, match="unexpectedly contains"):
        checkpoint.verify_files(tmp_path, {"build.ninja": None})


def test_no_warning_suppression_or_third_fresh_source_route():
    text = (source.ROOT / "tools/linux157_gn.py").read_text()
    assert "-Wno" not in text and "-Werror=false" not in text
    build = (source.ROOT / "tools/linux157_build.py").read_text()
    assert 'target_gn.write_text' not in build
    assert 'ua_recovery.verify(work, arch, repo)' in build
    assert "fresh fallback forbidden" in build


@pytest.mark.parametrize("mutation", ["patch", "version", "added", "removed"])
def test_recipe_delta_rejects_nonhelper_changes(mutation):
    old = checkpoint.load()["donors"]["arm64"]["source_receipt"]["recipe"]
    new = copy.deepcopy(source.recipe())
    if mutation == "patch":
        key = next(k for k in new["inputs"] if k.endswith(".patch"))
        new["inputs"][key] = "f" * 64
    elif mutation == "version":
        new["chromium_version"] = "157.0.8103.79"
    elif mutation == "added":
        new["inputs"]["tools/arbitrary.py"] = "f" * 64
    else:
        new["inputs"].pop("tools/linux157_toolchain.py")
    with pytest.raises(ValueError):
        checkpoint.validate_recipe_delta(old, new)


def test_download_evidence_required_before_tree_admission(tmp_path):
    with pytest.raises(ValueError, match="checkpoint-download"):
        checkpoint.admit(tmp_path, "arm64", checkpoint.expected("arm64"))
    assert not (tmp_path / "linux157-pre-gn-origin").exists()


def test_no_fresh_build_allowed_when_checkpoint_absent(tmp_path):
    from tools import linux157_build as build
    with mock.patch.object(build.toolchain.platform, "system", return_value="Linux"), \
            mock.patch.object(build.toolchain.platform, "machine", return_value="x86_64"), \
            mock.patch.dict(os.environ, {"CHROMIX_USE_UPSTREAM_CACHE":"0", "CHROMIX_UPSTREAM_CACHE_DIR":""}), \
            mock.patch.object(source, "prepare") as prepare:
        with pytest.raises(ValueError, match="fresh fallback forbidden"):
            build.build(tmp_path, "x64")
        prepare.assert_not_called()


@pytest.mark.parametrize("arch", ["x64", "arm64"])
@pytest.mark.parametrize("field", ["run_id", "attempt", "stage", "head_sha", "artifact", "job_id"])
def test_metadata_tuple_cannot_select_another_checkpoint(tmp_path, arch, field):
    metadata = checkpoint.expected(arch)
    if field == "artifact":
        metadata["artifacts"][0]["digest"] = "sha256:" + "0" * 64
    elif field == "head_sha":
        metadata[field] = "0" * 40
    else:
        metadata[field] += 1
    with pytest.raises(ValueError, match="metadata"):
        checkpoint.admit(tmp_path, arch, metadata)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name", ["args.gn", "build.ninja", ".ninja_log", ".ninja_deps"])
def test_gn_host_directory_never_rewrites_chromium_state(tmp_path, name):
    src = tmp_path / "src"
    out = src / "out/Chromix"
    out.mkdir(parents=True)
    protected = out / name
    protected.write_text("protected target/PGO/cache")
    before = (protected.read_bytes(), protected.stat().st_mtime_ns)
    elf(out / "gn")
    with mock.patch.object(gn, "host_environment", return_value=({}, {})), \
            mock.patch.object(gn, "gn_version", return_value=gn.VERSION), \
            mock.patch.object(gn.tools, "file_hash", return_value="25684acf0d11a3de5365c881b051f0c111e894e5d5a74898f81c2b04db25801b"):
        assert gn.prepare(src, out)["reused"] is True
    assert (protected.read_bytes(), protected.stat().st_mtime_ns) == before


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_exact_arg_migration_preserves_pgo_and_all_other_lines(tmp_path, arch):
    audit = os.environ.get("CHROMIX_GN_DONOR_LOGS")
    if not audit:
        pytest.skip("requires downloaded exact donor log artifact")
    donor = Path(audit) / arch / "logs/chromix-build/src/out/Chromix/args.gn"
    original = donor.read_bytes()
    backup = tmp_path / "linux157-pre-gn-origin/args.gn"
    backup.parent.mkdir()
    backup.write_bytes(original)
    target = tmp_path / "src/out/Chromix/args.gn"
    target.parent.mkdir(parents=True)
    target.write_bytes(original)
    checkpoint.prepare_args(tmp_path, arch)
    expected = original
    for line in (b"rust_prebuilt_stdlib = false\n", b"use_chromium_rust_toolchain = true\n",
                 b"v8_use_metagen_instance_types=false\n"):
        expected = expected.replace(line, b"")
    assert target.read_bytes() == expected
    assert b"chrome_pgo_phase=0" in expected or b"chrome_pgo_phase = 0" in expected
    before = target.stat().st_mtime_ns
    checkpoint.prepare_args(tmp_path, arch)
    assert target.stat().st_mtime_ns == before
    target.write_bytes(expected.replace(b"chrome_pgo_phase=0", b"chrome_pgo_phase=2").replace(
        b"chrome_pgo_phase = 0", b"chrome_pgo_phase = 2"))
    with pytest.raises(ValueError, match="three reviewed"):
        checkpoint.prepare_args(tmp_path, arch)
