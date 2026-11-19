"""Probe contracts; POSIX tests explicitly do not claim native Windows evidence."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import windows157_handle_probe as probe


def sample():
    return dict(volume_serial=123, file_id=456, size=4096, last_write=987654321,
                creation=123456789, links=2, attributes=0x20, last_access=555)


def test_stable_identity_retains_volume_file_size_times_links_and_attributes():
    base = sample()
    expected = probe.stable_identity(base)
    for key in probe.IDENTITY_FIELDS:
        changed = dict(base)
        changed[key] = 1 if key == 'links' else changed[key] + 1
        assert probe.stable_identity(changed) != expected, key
    assert probe.stable_identity(dict(base, last_access=999)) == expected
    assert probe.combined(0x12345678, 0x9abcdef0) == 0x123456789abcdef0


@pytest.mark.parametrize('change', [{'attributes': 0x400}, {'attributes': 0x10},
                                    {'links': 3}, {'links': 0}, {'file_id': 0}, {'size': 0}])
def test_invalid_handle_metadata_fails(change):
    with pytest.raises(ValueError): probe.stable_identity(dict(sample(), **change))


def test_probe_workflow_is_small_main_only_and_manual():
    data = yaml.safe_load((ROOT / '.github/workflows/probe-win157-handle-identity.yml').read_text())
    assert data['on'] == {'workflow_dispatch': {}}
    assert data['permissions'] == {'contents': 'read'}
    assert set(data['jobs']) == {'probe'}
    job = data['jobs']['probe']
    assert job['runs-on'] == 'windows-2022' and job['timeout-minutes'] == 10
    assert job['if'] == "github.ref == 'refs/heads/main'"
    assert job['strategy']['matrix']['python'] == ['3.12', '3.13', '3.14']
    steps = job['steps']
    assert steps[0]['with']['persist-credentials'] is False
    assert steps[1]['with']['architecture'] == 'x64'
    assert 'windows157_handle_probe.py' in steps[2]['run']
    assert 'throw' in steps[2]['run']
    assert steps[3]['if'] == '${{ always() }}'
    assert steps[3]['with']['if-no-files-found'] == 'error'
    text = json.dumps(data)
    for forbidden in ('download-artifact', 'restore_windows', 'ci-stage.ps1', 'workflow_run', 'push:', '10GB'):
        assert forbidden not in text


def test_actual_child_mutation_is_not_a_mock(tmp_path):
    path = tmp_path / 'probe.bin'
    path.write_bytes(b'a' * 100)
    result = probe.attempt_mutation(path, 'write')
    assert (result['python'] if sys.platform == 'win32' else result)['blocked'] is False
    assert path.read_bytes()[64:72] == b'MUTATION'
    result = probe.attempt_mutation(path, 'delete')
    assert (result['python'] if sys.platform == 'win32' else result)['blocked'] is False
    assert not path.exists()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX refusal contract')
def test_posix_run_cannot_be_misrepresented_as_native(tmp_path):
    report = tmp_path / 'report.json'
    result = subprocess.run([sys.executable, str(ROOT / 'tools/windows157_handle_probe.py'),
                             '--directory', str(tmp_path / 'probe'), '--report', str(report)],
                            text=True, capture_output=True)
    assert result.returncode == 1
    data = json.loads(report.read_text())
    assert data['status'] == 'failed' and data['native_windows'] is False
    assert 'native Windows' in data['error']['message']
    assert not (tmp_path / 'probe').exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='requires real native Windows filesystem and Win32 handles')
def test_native_probe_identity_private_copy_and_mutation_denial(tmp_path):
    report = tmp_path / 'report.json'
    code = probe.main(['--directory', str(tmp_path / 'native'), '--report', str(report)])
    data = json.loads(report.read_text())
    assert code == 0, data
    assert data['status'] == 'passed' and data['same_api_comparison'] == 'passed'
    assert len(data['mutations']) == 4
    assert all(probe.denial_verified(item) and item['unchanged_after_attempt'] for item in data['mutations'])
    assert data['unlocked_write']['python']['blocked'] is False
    assert data['unlocked_write']['win32']['bytes_written'] == 8
    assert data['private_copy']['win32']['links'] == 1
    assert data['before']['held_win32']['links'] == 2
    assert data['production_private_copy']['status'] == 'passed'
    assert data['production_atomic_install']['status'] == 'published'
    assert len(data['negative_cases']) == 3


def test_real_native_failed_reports_confirm_same_api_identity_and_crt_errno_loss():
    import gzip
    import hashlib
    data = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/windows157_handle_native.json.gz').read_bytes()))
    assert len(data['native_reports']) == 3
    for entry in data['native_reports'].values():
        assert hashlib.sha256(entry['text'].encode()).hexdigest() == entry['sha256']
        report = json.loads(entry['text'])
        before = report['before']
        assert before['path_stat']['st_mode'] == 33279
        assert before['fstat']['st_mode'] == 33206
        assert probe.stable_identity(before['held_win32']) == probe.stable_identity(before['path_win32'])
        assert probe.stable_identity(before['held_win32']) == probe.stable_identity(before['alias_win32'])
        mutation = report['mutations'][0]
        assert mutation['blocked'] and mutation['errno'] == 13 and mutation['winerror'] is None
        assert mutation['exception'] == 'PermissionError'
        # Historical Python denial alone is not enough: require new raw Win32 corroboration.
        raw = {'attempted': True, 'blocked': True, 'winerror': 32}
        assert probe.denial_verified({'python': mutation, 'win32': raw})
        assert not probe.denial_verified({'python': mutation, 'win32': dict(raw, winerror=5)})
        assert not probe.denial_verified({'python': mutation, 'win32': dict(raw, blocked=False)})


def test_production_uses_same_api_for_held_path_and_alias():
    import inspect
    source = inspect.getsource(probe.production.private_cargo_output)
    assert 'stream_identity(source)' in source
    assert 'validate_locked_identity(source, path, aliases, before)' in source
    assert 'file_identity(path.stat())' not in source
    assert 'file_identity(os.fstat' not in source
    assert probe.handle_info is probe.production.windows_handle_info
    assert probe.stable_identity is probe.production.windows_stable_identity
