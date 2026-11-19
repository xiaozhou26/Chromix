"""Real Bash/Ninja control flow for the mac157 post-GN missing-Crubit failure."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHELL = "/bin/bash"
NINJA = shutil.which("ninja")
FAILURE = json.loads((Path(__file__).parent / "fixtures/macos157_postgn_failure.json").read_text())


def script(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def env_for(tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    (tools / "python3").symlink_to(sys.executable)
    env = dict(os.environ, **FAILURE["environment"])
    env.update(PATH=str(tools) + os.pathsep + os.environ["PATH"], CHROMIX_JOBS="4",
               GITHUB_ACTIONS="true", CHROMIX_BUILD_PROFILE="fast")
    return env


def crubit_block():
    text = (ROOT / "build/posix/prepare-restored-tools.sh").read_text()
    start = text.index('  if [ "$(cat "$REPO/CHROMIUM_MACOS_VERSION")" = 157.0.8103.78 ]; then')
    return text[start:text.index('\nelse\n  if [ "$ARCH" = x64 ]', start)]


@pytest.fixture
def native_crubit(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("native Mach-O Crubit probe requires macOS")
    clang = shutil.which("clang")
    if not clang:
        pytest.skip("native compiler required")
    source = tmp_path / "probe.c"
    source.write_text('int main(int argc, char **argv) { return argc == 2 && argv[1][0] == \'-\' ? 0 : 1; }\n')
    output = tmp_path / "native-crubit"
    subprocess.run([clang, str(source), "-o", str(output)], check=True, capture_output=True)
    return output


def crubit_setup(tmp_path, native_crubit):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CHROMIUM_MACOS_VERSION").write_text("157.0.8103.78\n")
    # The implementation under test imports the actual Mach-O header validator.
    (repo / "tools").symlink_to(ROOT / "tools", target_is_directory=True)
    work = tmp_path / "work"
    src = work / "src"
    src.mkdir(parents=True)
    binary = src / "third_party/rust-toolchain/crubit/bin/cc_bindings_from_rs"
    recipe = work / "tooling/ungoogled-chromium-macos/build_crubit.sh"
    script(recipe, '#!/bin/bash\nset -eu\nprintf "crubit-build\\n" >> "$TRACE"\n'
                   'test "${FAIL_CRUBIT:-0}" = 0 || exit 23\n'
                   'mkdir -p "$(dirname "$DEST_CRUBIT")"\ncp "$NATIVE_CRUBIT" "$DEST_CRUBIT"\n')
    harness = script(tmp_path / "prepare.sh", '#!/bin/bash\nset -euo pipefail\n' + crubit_block() +
                     '\nprintf "finish\\n" >> "$TRACE"\n')
    env = env_for(tmp_path)
    env.update(REPO=str(repo), WORK=str(work), SRC=str(src), ARCH=os.uname().machine.replace("x86_64", "x64"),
               TRACE=str(tmp_path / "trace"), NATIVE_CRUBIT=str(native_crubit), DEST_CRUBIT=str(binary))
    return harness, binary, env


def test_missing_crubit_is_built_before_finish_and_native_resume_does_not_rebuild(tmp_path, native_crubit):
    harness, binary, env = crubit_setup(tmp_path, native_crubit)
    kept = Path(env["SRC"]) / "out/Default/obj/kept.o"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"unchanged compiled object")
    before = kept.stat().st_mtime_ns
    for _ in range(2):
        result = subprocess.run([SHELL, str(harness)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    assert Path(env["TRACE"]).read_text().splitlines() == ["crubit-build", "finish", "finish"]
    assert binary.read_bytes() == native_crubit.read_bytes()
    assert kept.stat().st_mtime_ns == before


@pytest.mark.parametrize("failure", ["build", "local", "linked"])
def test_crubit_failure_stays_hard_before_finish(tmp_path, native_crubit, failure):
    harness, binary, env = crubit_setup(tmp_path, native_crubit)
    if failure == "build":
        env["FAIL_CRUBIT"] = "1"
    elif failure == "local":
        env["GITHUB_ACTIONS"] = "false"
    else:
        binary.parent.mkdir(parents=True)
        binary.symlink_to(native_crubit)
    result = subprocess.run([SHELL, str(harness)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    trace = Path(env["TRACE"])
    assert not trace.exists() or "finish" not in trace.read_text()
    if failure != "build":
        assert not trace.exists()


@pytest.mark.parametrize("baseline,missing", [(True, True), (False, True), (False, False)])
def test_postgn_executes_actual_ninja_and_preserves_errexit(tmp_path, baseline, missing):
    if not NINJA:
        pytest.skip("real Ninja required")
    src = tmp_path / "src"
    out = src / "out/Default"
    out.mkdir(parents=True)
    crubit = src / "third_party/rust-toolchain/crubit/bin/cc_bindings_from_rs"
    if not missing:
        script(crubit, '#!/bin/sh\nexit 0\n')
    # Only expensive GN is replaced; dependency traversal and -n failure are real Ninja.
    script(out / "gn", '#!/bin/sh\nprintf "Done. Made 36557 targets from 4994 files in 41088ms\\n"\n')
    (out / "args.gn").write_text(FAILURE["args"])
    (out / "build.ninja").write_text('rule generate\n  command = mkdir -p gen/components/cbor/rust && touch $out\n'
        'build gen/components/cbor/rust/cbor_rust.h: generate ../../third_party/rust-toolchain/crubit/bin/cc_bindings_from_rs\n'
        'build chrome: phony gen/components/cbor/rust/cbor_rust.h\n')
    kept = out / "retained.o"
    kept.write_bytes(b"unchanged")
    before = kept.stat().st_mtime_ns
    if baseline:
        builder = subprocess.check_output(["git", "show", "9449625:build/macos/build.sh"], cwd=ROOT, text=True)
    else:
        builder = (ROOT / "build/macos/build.sh").read_text()
    tail = builder[builder.index('"$OUT/gn" gen'):]
    harness = script(tmp_path / "postgn.sh", '#!/bin/bash\nset -euo pipefail\n' +
        'source "$REPO/build/posix/upstream-cache.sh"\n' + tail)
    env = env_for(tmp_path)
    env.update(REPO=str(ROOT), WORK=str(tmp_path), SRC=str(src), OUT=str(out), ARCH="x64",
               CHROMIX_NINJA=NINJA, CHROMIX_UPSTREAM_CACHE_DIR=str(tmp_path / "cache"))
    result = subprocess.run([SHELL, "-x", str(harness)], env=env, capture_output=True, text=True)
    assert "Made 36557 targets" in result.stdout
    assert kept.stat().st_mtime_ns == before
    if missing:
        assert result.returncode == 1
        plan = (tmp_path / "upstream-cache-plan.log").read_text()
        assert "cc_bindings_from_rs" in plan and "missing and no known rule" in plan
        assert "chromix_build_restored_target" not in result.stderr
        if baseline:
            assert "missing and no known rule" not in result.stderr
        else:
            assert "missing and no known rule" in result.stderr
            assert "macOS Ninja plan failed (exit 1)" in result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert (out / "gen/components/cbor/rust/cbor_rust.h").is_file()
        assert "chromix_build_restored_target macos 4 chrome" in result.stderr


def test_crubit_preparation_precedes_tool_fingerprint_finish():
    source = (ROOT / "build/posix/prepare-restored-tools.sh").read_text()
    assert source.index('verify_tooling(Path(sys.argv[2])') < source.index('build_crubit.sh')
    assert source.index('retrieve_and_unpack_resource.py') < source.index('build_crubit.sh')
    assert source.index('build_crubit.sh') < source.index('--phase finish')


def test_foreign_crubit_is_not_executed_and_is_replaced(tmp_path, native_crubit):
    harness, binary, env = crubit_setup(tmp_path, native_crubit)
    binary.parent.mkdir(parents=True)
    foreign = 0x1000007 if env["ARCH"] == "arm64" else 0x100000C
    binary.write_bytes(struct.pack("<8I", 0xFEEDFACF, foreign, 0, 2, 0, 0, 0, 0))
    binary.chmod(0o755)
    result = subprocess.run([SHELL, str(harness)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "does not match the native host" in result.stderr
    assert Path(env["TRACE"]).read_text().splitlines() == ["crubit-build", "finish"]
    assert binary.read_bytes() == native_crubit.read_bytes()


def test_successful_recipe_without_output_cannot_reach_finish(tmp_path, native_crubit):
    harness, binary, env = crubit_setup(tmp_path, native_crubit)
    recipe = Path(env["WORK"]) / "tooling/ungoogled-chromium-macos/build_crubit.sh"
    recipe.write_text('#!/bin/bash\nexit 0\n')
    result = subprocess.run([SHELL, str(harness)], env=env, capture_output=True, text=True)
    assert result.returncode != 0 and "Crubit executable is missing" in result.stderr
    assert not binary.exists() and not Path(env["TRACE"]).exists()


def test_historical_version_does_not_add_crubit_gate(tmp_path, native_crubit):
    harness, binary, env = crubit_setup(tmp_path, native_crubit)
    (Path(env["REPO"]) / "CHROMIUM_MACOS_VERSION").write_text("156.0.8078.79\n")
    result = subprocess.run([SHELL, str(harness)], env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert Path(env["TRACE"]).read_text().splitlines() == ["finish"]
    assert not binary.exists()
