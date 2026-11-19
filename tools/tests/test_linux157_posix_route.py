"""Linux157 fresh staged routing without altering historical donor contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import mock

import pytest
import yaml

from tools import linux157_source as source
from tools import linux157_build as build
from tools import linux157_postgn as checkpoint

ROOT = Path(__file__).resolve().parents[2]


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@pytest.mark.parametrize("arch", ["x64", "arm64"])
@pytest.mark.parametrize("version", ["157.0.8103.78", "156.0.8078.79"])
def test_real_stage_selects_exact_fresh_or_historical_builder(tmp_path, arch, version):
    repo, work, binaries = tmp_path / "repo", tmp_path / "work", tmp_path / "bin"
    binaries.mkdir()
    stage = repo / "build/posix/ci-stage.sh"
    put(stage, (ROOT / "build/posix/ci-stage.sh").read_text())
    put(repo / "CHROMIUM_LINUX_VERSION", version + "\n")
    put(work / "src/.chromix-source-ready", "fixture")
    put(repo / "build/build.sh", '#!/bin/sh\necho legacy >> "$CALL_LOG"\nexit 17\n')
    put(repo / "build/prepare-ungoogled.sh", '#!/bin/sh\necho legacy-prepare >> "$CALL_LOG"\nexit 99\n')
    put(repo / "tools/linux157_source.py", '''import os,sys
phase=sys.argv[sys.argv.index('--phase')+1]
with open(os.environ['CALL_LOG'],'a') as f:f.write(phase+':'+sys.argv[sys.argv.index('--arch')+1]+'\\n')
assert phase in ('route','checkpoint')
''')
    put(repo / "tools/linux157_build.py", '''import os,sys
with open(os.environ['CALL_LOG'],'a') as f:f.write('fresh:'+sys.argv[sys.argv.index('--arch')+1]+'\\n')
raise SystemExit(17)
''')
    put(binaries / "timeout", '#!/bin/sh\nshift 5\nexec "$@"\n')
    log = tmp_path / "calls"
    result = subprocess.run(['bash', str(stage), '--platform', 'linux', '--arch', arch,
                             '--workdir', str(work)], capture_output=True, text=True,
                            env=dict(os.environ, PATH=str(binaries)+os.pathsep+os.environ['PATH'],
                                     CHROMIX_USE_UPSTREAM_CACHE='0', CHROMIX_BUILD_PROFILE='fast',
                                     CHROMIX_LINUX157_PRE_GN_RECOVERY='1' if version.startswith('157') else '0',
                                     CALL_LOG=str(log)), timeout=15)
    assert result.returncode == 1
    assert 'build script failed' in result.stderr
    expected = [f'route:{arch}', f'checkpoint:{arch}', f'fresh:{arch}'] if version.startswith('157') else ['legacy']
    assert log.read_text().splitlines() == expected


def test_fresh_build_really_calls_prepare_then_official_toolchain(tmp_path):
    (tmp_path / "src").mkdir()
    calls = []
    with mock.patch.object(build.toolchain.platform, 'system', return_value='Linux'), \
            mock.patch.object(build.toolchain.platform, 'machine', return_value='x86_64'), \
            mock.patch.dict(os.environ, {'CHROMIX_USE_UPSTREAM_CACHE':'0', 'CHROMIX_UPSTREAM_CACHE_DIR':''}), \
            mock.patch.object(build.source, 'prepare', side_effect=lambda *a: calls.append('source')), \
            mock.patch.object(build.toolchain, 'prepare', side_effect=ValueError('official tool provisioning sentinel')) as tools:
        with pytest.raises(ValueError, match='official tool provisioning sentinel'):
            build.build(tmp_path, 'arm64')
        tools.assert_called_once_with(tmp_path / 'src', 'arm64', build.source.ROOT)
    assert calls == ['source']


@pytest.mark.parametrize('arch', ['x64', 'arm64'])
def test_exact_route_rejects_donor_mode_before_build(arch):
    with mock.patch.dict(os.environ, {'CHROMIX_USE_UPSTREAM_CACHE':'1'}):
        with pytest.raises(ValueError, match='fresh mode'):
            source.validate_route(ROOT, arch)
    with mock.patch.dict(os.environ, {'CHROMIX_USE_UPSTREAM_CACHE':'0', 'CHROMIX_UPSTREAM_CACHE_DIR':''}):
        assert source.validate_route(ROOT, arch)['review'] == {
            'path': source.MANIFEST, 'sha256': source.MANIFEST_SHA256}


def test_native_runtime_and_macos_integrity_contracts_remain():
    stage = (ROOT / 'build/posix/ci-stage.sh').read_text()
    assert 'finish_macos_compile_release()' in stage
    assert 'substr($0, 67) == asset' in stage
    assert 'ARM64 bundle compiled and checksum/ELF verified; native runtime verification is still required' in stage
    workflow = yaml.safe_load((ROOT / '.github/workflows/build-posix-github.yml').read_text())
    gate = workflow['jobs']['verify-linux-arm64']
    assert gate['runs-on'] == 'ubuntu-24.04-arm'
    assert any('Native ARM64 fingerprint' in step.get('name','') for step in gate['steps'])
    for index in range(1,9):
        steps = workflow['jobs'][f'posix-{index}']['steps']
        guard = next(s for s in steps if s.get('name')=='Validate exact Linux157 fresh source route')
        assert "inputs.platform == 'linux'" in guard['if']
        assert '157.0.8103.78' in guard['if']
        failed = next(s for s in steps if s.get('id')=='linux_runtime_checkpoint')
        assert '--phase checkpoint' in failed['run']
        assert 'tools/verify_linux_checkpoint.py' in failed['run']
        assert 'CHROMIUM_VERSION' in failed['run']


@pytest.mark.parametrize('gate_code', [0, 19])
def test_generated_failed_fresh_checkpoint_calls_new_gate_before_snapshot(tmp_path, gate_code):
    workflow = yaml.safe_load((ROOT / '.github/workflows/build-posix-github.yml').read_text())
    step = next(s for s in workflow['jobs']['posix-1']['steps'] if s.get('id') == 'linux_runtime_checkpoint')
    script = step['run'].replace("${{ inputs.arch }}", 'arm64')
    work = tmp_path / 'chromix-build'
    (work / 'src').mkdir(parents=True)
    (tmp_path / 'chromix-logs').mkdir()
    log = tmp_path / 'calls'
    put(tmp_path / 'tools/linux157_source.py', f'''import os,sys
assert '--phase' in sys.argv and 'checkpoint' in sys.argv
assert os.environ['CHROMIX_USE_UPSTREAM_CACHE']=='0'
with open(os.environ['CALL_LOG'],'a') as f:f.write('fresh-checkpoint\\n')
raise SystemExit({gate_code})
''')
    put(tmp_path / 'tools/verify_linux_checkpoint.py', "raise SystemExit('wrong historical validator')\n")
    put(tmp_path / 'build/posix/ci-parts.sh', '#!/bin/sh\necho snapshot >> "$CALL_LOG"\n')
    result = subprocess.run(['bash', '-c', script], cwd=tmp_path, capture_output=True, text=True,
                            env=dict(os.environ, RUNNER_TEMP=str(tmp_path), CALL_LOG=str(log),
                                     GITHUB_OUTPUT=str(tmp_path / 'outputs'), CHROMIUM_VERSION=source.VERSION))
    assert result.returncode == gate_code, result.stderr
    assert log.read_text().splitlines() == (['fresh-checkpoint', 'snapshot'] if gate_code == 0 else ['fresh-checkpoint'])
    if gate_code:
        assert not (tmp_path / 'outputs').exists()
    else:
        assert (tmp_path / 'outputs').read_text() == 'upload_snapshot=true\n'


def test_recipe_binds_lite_payload_and_keeps_gitlink_override_separate():
    recipe = source.recipe(ROOT)
    assert recipe['inputs'][source.LITE_PATH] == source.LITE_SHA256
    assert recipe['platform_core_gitlink'] != recipe['core_override']
    text = (ROOT / 'tools/linux157_source.py').read_text()
    assert 'fresh preparation is disabled' in text
    assert 'get_platform(*paths, repo)' in text
    assert 'never rewrite the baseline gitlink' in text


def test_checkpoint_requires_exact_ready_selection_and_provenance(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    identity = source.recipe(ROOT)
    (src / '.chromix-source-ready').write_text('linux157-fresh|arm64|' + identity['sha256'] + '\n')
    selection = {'selection': {'fixture': '157'}}
    receipt = {'stage': 'chromix-applied', 'target_arch': 'arm64', 'chromix_selection': selection}
    from tools import patch_selection
    with mock.patch.object(checkpoint, 'verify'), \
            mock.patch.object(source, 'validate_route', return_value=identity), \
            mock.patch.object(source, 'verify_receipt', return_value=receipt), \
            mock.patch.object(source, 'get_platform', return_value=identity) as origin, \
            mock.patch.object(build.toolchain, 'preflight_inputs', return_value={'recoverable_missing': []}), \
            mock.patch.object(patch_selection, 'select', return_value=(selection, [])):
        result = source.checkpoint(tmp_path, 'arm64')
        assert result['runtime_verified'] is False
        origin.assert_called_once()
        with pytest.raises(ValueError, match='changed'):
            source.checkpoint(tmp_path, 'x64')
        (src / '.chromix-domain-substitution-in-progress').touch()
        with pytest.raises(ValueError, match='changed'):
            source.checkpoint(tmp_path, 'arm64')


def test_generated_workflow_matches_generator():
    path = ROOT / '.github/workflows/build-posix-github.yml'
    original = path.read_bytes()
    subprocess.run([sys.executable, str(ROOT / 'tools/gen_posix_workflow.py')], cwd=ROOT, check=True)
    assert path.read_bytes() == original
