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
    assert result['blocked'] is False and path.read_bytes()[64:72] == b'MUTATION'
    result = probe.attempt_mutation(path, 'delete')
    assert result['blocked'] is False and not path.exists()


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
    assert all(item['blocked'] and item['winerror'] == 32 for item in data['mutations'])
    assert data['unlocked_write']['blocked'] is False
    assert data['private_copy']['win32']['links'] == 1
    assert data['before']['held_win32']['links'] == 2
    assert data['production_private_copy']['status'] in ('passed', 'failed')
