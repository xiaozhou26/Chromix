"""Regression for Windows run 34614380682's stale restored patch 0042."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'build/windows/update-restored-source.ps1'
SHELL = shutil.which('pwsh') or shutil.which('powershell')
CURRENT = 'std::has_single_bit(static_cast<T>(value.ValueOrDie()))'
OLD = 'std::has_single_bit(value.ValueOrDie())'
RELATIVE = 'third_party/blink/renderer/modules/webgpu/gpu_supported_limits.cc'


def run_migration(tmp_path, content):
    if not SHELL:
        pytest.skip('PowerShell is required for native migration execution')
    source = tmp_path / RELATIVE
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content.encode())
    function = re.search(r'(?ms)^function Update-WebGpuAlignmentValidation \{.*?^\}',
                         SCRIPT.read_text(encoding='utf-8'))[0]
    harness = tmp_path / 'migrate.ps1'
    harness.write_text('param([string]$Src)\n$ErrorActionPreference="Stop"\n' +
                       function + '\nUpdate-WebGpuAlignmentValidation\n', encoding='utf-8')
    command = [SHELL, '-NoProfile', '-NonInteractive', '-File', str(harness), '-Src', str(tmp_path)]
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=30)
    return source, command, result


def body(expression, newline='\n'):
    return newline.join(['// independent cached-source fixture',
                         'base::CheckedNumeric<T> value{limitRawIntegerValue};',
                         'if ((limitName == "minUniformBufferOffsetAlignment" ||',
                         '     limitName == "minStorageBufferOffsetAlignment") &&',
                         f'    !{expression}) {{ return false; }}', ''])


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
@pytest.mark.parametrize('expression', [OLD, 'std::has_single_bit( value.ValueOrDie() )', CURRENT])
def test_cached_form_is_migrated_once_without_rewriting_current_file(tmp_path, expression, newline):
    source, command, result = run_migration(tmp_path, body(expression, newline))
    assert result.returncode == 0, result.stdout + result.stderr
    assert source.read_bytes() == body(CURRENT, newline).encode()
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    again = subprocess.run(command, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=30)
    assert again.returncode == 0, again.stdout + again.stderr
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


@pytest.mark.parametrize('content', [body('some_other_call(value)'), body(OLD) + body(OLD),
                                     body(OLD) + body(CURRENT), body(OLD).replace('CheckedNumeric<T>', 'Other<T>')])
def test_unknown_or_ambiguous_source_fails_without_mutation(tmp_path, content):
    source, _, result = run_migration(tmp_path, content)
    assert result.returncode != 0
    assert source.read_bytes() == content.encode()


def test_migration_and_repository_patch_share_the_checked_conversion():
    patch = next((ROOT / 'patches').glob('0042-*.patch')).read_text()
    script = SCRIPT.read_text(encoding='utf-8')
    assert CURRENT in patch and CURRENT in script
    assert OLD not in patch
    assert script.index('\nUpdate-WebGpuAlignmentValidation\n') < script.index('# The stage-3 cache')
