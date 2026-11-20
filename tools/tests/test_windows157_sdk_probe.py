"""SDK probe contracts and binary fixtures; no native installation is simulated as evidence."""
import json
import ctypes
from types import SimpleNamespace
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
NATIVE = json.loads((ROOT / 'tools/tests/fixtures/windows157-sdk-44805916373.json').read_text())
NATIVE_MIDL = NATIVE['binary_records'][1]


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
    expected_flags = {} if kind == 'bigobj' else {('import_type_info' if kind == 'import' else 'coff_characteristics') + ':0x0000': 1}
    assert result['member_flags'] == expected_flags
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
    monkeypatch.setattr(probe, 'product_version', lambda path: (
        '8.1.628.1' if path.name == 'midl.exe' else
        '10.0.26100.1' if mutation == 'version' else '10.0.28000.2705'))
    # Synthetic PE bytes exercise the file walker; exact MIDL identity is tested with native evidence below.
    midl_calls = []
    monkeypatch.setattr(probe, 'check_midl_identity', lambda sdk, path, record: midl_calls.append(record.copy()))
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
        assert len(midl_calls) == 1 and midl_calls[0]['product_version'] == '8.1.628.1'
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


def test_native_midl_mapping_matches_unmodified_msi_and_binary_records():
    assert NATIVE['installation']['exit_code'] == 0
    assert NATIVE['installation']['sdk_release'] == probe.SDK_RELEASE
    assert NATIVE['installation']['installer_sha256_actual'].lower() == probe.MIDL_IDENTITY['bundle_sha256']
    for key in ('machine', 'size', 'sha256', 'product_version'):
        assert NATIVE_MIDL[key] == probe.MIDL_IDENTITY[key]
    lines = {item['line']: item['text'] for item in NATIVE['msi_log']['lines']}
    assert probe.MIDL_IDENTITY['msi_component'] in lines[865]
    assert NATIVE_MIDL['path'] in lines[865]
    assert 'FileSize=154056' in lines[1626] and 'Version=8.1.628.1' in lines[1626]
    assert probe.MIDL_IDENTITY['msi_version'] in lines[2735]
    assert probe.MIDL_IDENTITY['msi_product'] in lines[2857]


@pytest.mark.parametrize('mutation', ['valid', 'version8', 'version10', 'hash', 'size', 'machine', 'path',
                                      'registration', 'signature'])
def test_exact_native_midl_identity_cannot_be_generalized(tmp_path, monkeypatch, mutation):
    sdk = tmp_path / 'sdk'
    path = sdk / probe.MIDL_IDENTITY['relative']
    record = dict(NATIVE_MIDL)
    changes = {'version8': ('product_version', '8.1.628.2'), 'version10': ('product_version', '10.0.28000.2705'),
               'hash': ('sha256', '0' * 64), 'size': ('size', 154057), 'machine': ('machine', 0xaa64)}
    if mutation in changes:
        key, value = changes[mutation]
        record[key] = value
    if mutation == 'path':
        path = sdk / 'bin/10.0.28000.0/arm64/midl.exe'
    calls = []
    def native_check(name):
        def check(path):
            calls.append(name)
            if mutation == name:
                raise ValueError('injected native check failure')
            return {'fixture': name}
        return check
    monkeypatch.setattr(probe, 'midl_registration', native_check('registration'))
    monkeypatch.setattr(probe, 'microsoft_signature', native_check('signature'))
    if mutation == 'valid':
        probe.check_midl_identity(sdk, path, record)
        assert calls == ['registration', 'signature']
        assert record['sdk_bundle'] == '10.0.28000.2705'
    else:
        with pytest.raises(ValueError):
            probe.check_midl_identity(sdk, path, record)
        if mutation not in ('registration', 'signature'):
            assert calls == []


@pytest.mark.parametrize('mutation', ['valid', 'untrusted', 'unsigned', 'wrong-cn', 'wrong-org', 'no-cert'])
def test_midl_signature_requires_valid_microsoft_certificate(tmp_path, monkeypatch, mutation):
    signature = dict(status='Valid', subject='CN=Microsoft Corporation, O=Microsoft Corporation, C=US', thumbprint='fixture')
    signature.update({'untrusted': {'status': 'NotTrusted'}, 'unsigned': {'status': 'NotSigned'},
                      'wrong-cn': {'subject': 'CN=Microsoft Corporation Malware, O=Microsoft Corporation'},
                      'wrong-org': {'subject': 'CN=Microsoft Corporation, O=Other'},
                      'no-cert': {'thumbprint': None}}.get(mutation, {}))
    monkeypatch.setenv('SystemRoot', str(tmp_path))
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=json.dumps(signature))
    monkeypatch.setattr(probe.subprocess, 'run', run)
    path = tmp_path / 'sdk/midl.exe'
    if mutation == 'valid':
        assert probe.microsoft_signature(path) == signature
    else:
        with pytest.raises(ValueError):
            probe.microsoft_signature(path)
    args, kwargs = calls[0]
    assert 'Get-AuthenticodeSignature -LiteralPath $env:CHROMIX_SDK_PROBE_FILE' in args[-1]
    assert kwargs['env']['CHROMIX_SDK_PROBE_FILE'] == str(path)
    assert kwargs['check'] and kwargs['timeout'] == 60


@pytest.mark.parametrize('mutation', ['valid', 'state', 'code', 'version', 'path'])
def test_msi_registration_binds_product_component_version_and_path(tmp_path, monkeypatch, mutation):
    path = tmp_path / 'sdk/bin/10.0.28000.0/x64/midl.exe'
    def component(product, component_id, buffer, length):
        assert product == probe.MIDL_IDENTITY['msi_product']
        assert component_id == probe.MIDL_IDENTITY['msi_component']
        buffer.value = str(path) + ('-other' if mutation == 'path' else '')
        return 2 if mutation == 'state' else 3
    def product_info(product, key, buffer, length):
        assert product == probe.MIDL_IDENTITY['msi_product'] and key == 'VersionString'
        buffer.value = '10.1.26100.1' if mutation == 'version' else '10.1.28000.2705'
        return 1605 if mutation == 'code' else 0
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: SimpleNamespace(
        MsiGetComponentPathW=component, MsiGetProductInfoW=product_info), raising=False)
    if mutation == 'valid':
        result = probe.midl_registration(path)
        assert result['component_path'] == str(path) and result['install_state'] == 3
    else:
        with pytest.raises(ValueError):
            probe.midl_registration(path)
