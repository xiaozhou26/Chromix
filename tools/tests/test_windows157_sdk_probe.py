"""SDK probe contracts and binary fixtures; no native installation is simulated as evidence."""
import json
from pathlib import Path
import shutil
import struct
import subprocess

import pytest
import yaml

from tools import windows157_sdk_probe as probe
from tools.tests.test_verify_windows_bundle import pe_fixture

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github/workflows/probe-win157-arm64-sdk.yml'


def member(name, data):
    header = f'{name:<16}{0:<12}{0:<6}{0:<6}{0:<8}{len(data):<10}`\n'.encode()
    return header + data + (b'\n' if len(data) % 2 else b'')


def library(arch, kind='coff'):
    data = bytearray(56 if kind == 'bigobj' else 20)
    if kind in ('import', 'bigobj'):
        struct.pack_into('<HHHH', data, 0, 0, 0xffff, 2 if kind == 'bigobj' else 0, probe.MACHINES[arch])
    else:
        struct.pack_into('<H', data, 0, probe.MACHINES[arch])
    return b'!<arch>\n' + member('/', b'table') + member('//', b'long names') + member('/0', data)


@pytest.mark.parametrize('arch', ['x86', 'x64', 'arm64'])
@pytest.mark.parametrize('kind', ['coff', 'import', 'bigobj'])
def test_library_checks_all_supported_member_types(tmp_path, arch, kind):
    path = tmp_path / 'sdk.lib'
    path.write_bytes(library(arch, kind))
    result = probe.check_library(path, arch)
    assert result['object_machines'] == {f'0x{probe.MACHINES[arch]:04x}': 1}
    assert len(result['sha256']) == 64
    with pytest.raises(ValueError, match='wrong COFF'):
        probe.check_library(path, 'x64' if arch == 'arm64' else 'arm64')


@pytest.mark.parametrize('payload', [b'', b'not a library', b'!<arch>\n', b'!<arch>\nshort',
                                     library('arm64')[:-1], b'!<arch>\n' + member('a.obj/', b'x')])
def test_bad_libraries_fail_closed(tmp_path, payload):
    path = tmp_path / 'sdk.lib'
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        probe.check_library(path, 'arm64')


def fixture_sdk(root):
    header = root / f'Include/{probe.SDK_VERSION}/shared/sdkddkver.h'
    header.parent.mkdir(parents=True)
    header.write_text('#define NTDDI_WIN11_BR 1\n')
    for relative, arch in ((f'bin/{probe.SDK_VERSION}/x64/rc.exe', 'x64'),
                           (f'bin/{probe.SDK_VERSION}/x64/midl.exe', 'x64'),
                           ('Debuggers/x64/dbghelp.dll', 'x64'), ('Debuggers/arm64/dbghelp.dll', 'arm64')):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pe_fixture(arch, dll=path.suffix == '.dll'))
    for arch in probe.MACHINES:
        for component, names in (('um', ('kernel32.lib', 'user32.lib')),
                                 ('ucrt', ('ucrt.lib', 'libucrt.lib'))):
            for name in names:
                path = root / f'Lib/{probe.SDK_VERSION}/{component}/{arch}/{name}'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(library(arch))


@pytest.mark.parametrize('mutation', ['valid', 'host-arm64', 'host-dll', 'debugger-x64', 'lib-x64', 'version', 'header', 'missing'])
def test_full_probe_preserves_failure_report_and_checks_all_files(tmp_path, monkeypatch, mutation):
    sdk = tmp_path / 'sdk'
    fixture_sdk(sdk)
    monkeypatch.setattr(probe.sys, 'platform', 'win32')
    monkeypatch.setattr(probe, 'product_version', lambda path: '10.0.26100.1' if mutation == 'version' else '10.0.28000.1')
    if mutation == 'host-arm64':
        (sdk / f'bin/{probe.SDK_VERSION}/x64/rc.exe').write_bytes(pe_fixture('arm64'))
    elif mutation == 'host-dll':
        (sdk / f'bin/{probe.SDK_VERSION}/x64/rc.exe').write_bytes(pe_fixture('x64', dll=True))
    elif mutation == 'debugger-x64':
        (sdk / 'Debuggers/arm64/dbghelp.dll').write_bytes(pe_fixture('x64', dll=True))
    elif mutation == 'lib-x64':
        (sdk / f'Lib/{probe.SDK_VERSION}/um/arm64/kernel32.lib').write_bytes(library('x64'))
    elif mutation == 'header':
        (sdk / f'Include/{probe.SDK_VERSION}/shared/sdkddkver.h').write_text('// #define NTDDI_WIN11_BR 1')
    elif mutation == 'missing':
        (sdk / f'Lib/{probe.SDK_VERSION}/ucrt/arm64/libucrt.lib').unlink()
    report = tmp_path / 'report.json'
    assert probe.main(['--sdk', str(sdk), '--report', str(report)]) == (0 if mutation == 'valid' else 1)
    data = json.loads(report.read_text())
    assert data['status'] == ('verified' if mutation == 'valid' else 'failed')
    if mutation == 'valid':
        assert len(data['files']) == 17
        assert all(len(record['sha256']) == 64 for record in data['files'])
    else:
        assert data['error']


def test_probe_workflow_is_manual_small_and_always_uploads_whole_diagnostics():
    data = yaml.safe_load(WORKFLOW.read_text())
    assert data['on'] == {'workflow_dispatch': {}}
    assert data['permissions'] == {'contents': 'read'}
    assert set(data['jobs']) == {'probe'}
    job = data['jobs']['probe']
    assert job['if'] == "github.ref == 'refs/heads/main'"
    assert job['runs-on'] == 'windows-2022' and job['timeout-minutes'] == 30
    steps = job['steps']
    assert steps[0]['with']['persist-credentials'] is False
    run = steps[2]
    assert run['timeout-minutes'] == 20 and run['shell'] == 'powershell'
    assert "-Arch arm64 -ChromiumVersion '157.0.8103.78' -Install -InstallTimeoutMinutes 15" in run['run']
    assert run['run'].index('ensure-windows-sdk.ps1') < run['run'].index('windows157_sdk_probe.py')
    assert 'if ($LASTEXITCODE -ne 0)' in run['run'] and 'finally' in run['run']
    assert steps[3]['if'] == '${{ always() }}'
    assert steps[3]['with']['path'] == '${{ runner.temp }}/win157-arm64-sdk/'
    assert steps[3]['with']['if-no-files-found'] == 'error'
    for text in ('download-artifact', 'ci-stage', 'ci-parts', 'continue-on-error', 'workflow_call', 'push:', 'retry'):
        assert text not in WORKFLOW.read_text()


def test_workflow_powershell_parses(tmp_path):
    pwsh = shutil.which('pwsh')
    if not pwsh:
        pytest.skip('PowerShell unavailable')
    source = tmp_path / 'probe.ps1'
    source.write_text(yaml.safe_load(WORKFLOW.read_text())['jobs']['probe']['steps'][2]['run'])
    result = subprocess.run([pwsh, '-NoProfile', '-Command',
                             '$t=$null; $e=$null; [Management.Automation.Language.Parser]::ParseFile('
                             f"'{source}', [ref]$t, [ref]$e) > $null; if ($e) {{ $e; exit 1 }}"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
