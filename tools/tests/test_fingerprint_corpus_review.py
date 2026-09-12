"""All samples here are invented unit fixtures, not a physical-device corpus."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fingerprint_corpus_review as corpus
from test_device_pool import bundle, seal
from test_device_p0 import v2_observation

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)
VERSION = '152.0.7977.82'


def dump(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def sample_manifest(tmp_path):
    root = tmp_path / 'unit-fixture'
    record = bundle(root)
    probe_hash = corpus.pool.file_hash(corpus.PROBE)
    browser = corpus.pool.load_json(root / 'browser.json')
    browser.update(browser_versions=[VERSION] * 3, probe_sha256=probe_hash,
                   observations=[v2_observation() for _ in range(3)])
    dump(root / 'browser.json', browser)
    record['device']['surfaces'] = corpus.pool.stable_observation(browser['observations'][0])
    record['provenance'].update(browser_version=VERSION, probe_sha256=probe_hash)
    record['evidence']['browser']['sha256'] = corpus.pool.file_hash(root / 'browser.json')
    seal(record); dump(root / 'record.json', record)
    manifest = {'schema_version': 1, 'browser_version': VERSION, 'probe_sha256': probe_hash,
                'max_age_days': 30, 'cohorts': [{'id': 'test-cohort', 'os': 'fixture',
                    'architecture': 'fixture', 'min_devices': 1}],
                'samples': [{'path': 'unit-fixture/record.json', 'record_id': record['record_id'],
                    'record_sha256': corpus.pool.file_hash(root / 'record.json'), 'device_id': 'test-machine-only',
                    'cohort_id': 'test-cohort', 'reviewer': 'test-reviewer-not-authenticated',
                    'reviewed_at': '2026-09-11T00:00:00Z', 'expires_at': '2026-10-01T00:00:00Z',
                    # Exercise the schema branch, not a claim about this fixture.
                    'sample_kind': 'physical'}]}
    path = tmp_path / 'review.json'; dump(path, manifest)
    return path, manifest


def test_review_checks_are_read_only_not_hardware_attestation(sample_manifest):
    path, _ = sample_manifest
    files = [p for p in path.parent.rglob('*') if p.is_file()]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
    result = corpus.review(path, now=NOW)
    assert result['status'] == 'passed', result
    assert result['cohorts'][0]['observed_devices'] == 1
    assert result['qualification']['physical_backend_equivalence'] is False
    assert result['qualification']['font_file_to_glyph_binding'] is False
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files} == before


@pytest.mark.parametrize('field,value,match', [
    ('expires_at', '2026-09-11T00:00:00Z', 'expired'),
    ('reviewed_at', '2026-09-13T00:00:00Z', 'future'),
    ('reviewed_at', '2026-09-09T00:00:00Z', 'ordered'),
    ('reviewed_at', '2026-09-11T00:00:00', 'timezone'),
    ('cohort_id', 'missing', 'cohort'), ('record_sha256', 'a' * 64, 'checksum'),
    ('record_id', 'a' * 64, 'different record'), ('reviewer', '', 'metadata'),
    ('path', '../outside.json', 'unsafe path'), ('sample_kind', 'measured', 'metadata'),
])
def test_invalid_review_rejected(sample_manifest, field, value, match):
    path, manifest = sample_manifest
    manifest['samples'][0][field] = value; dump(path, manifest)
    report = corpus.review(path, now=NOW)
    assert report['status'] == 'failed' and match in str(report['errors'])


@pytest.mark.parametrize('kind', ['virtual', 'control', 'fixture'])
def test_control_and_synthetic_samples_cannot_fill_cohort(sample_manifest, kind):
    path, manifest = sample_manifest
    manifest['samples'][0]['sample_kind'] = kind; dump(path, manifest)
    report = corpus.review(path, now=NOW)
    assert report['samples'][0]['status'] == 'excluded'
    assert report['cohorts'][0]['observed_devices'] == 0 and report['status'] == 'failed'


@pytest.mark.parametrize('fault', ['duplicate', 'old', 'version', 'cohort', 'count'])
def test_cohort_version_age_and_duplicate_guards(sample_manifest, fault):
    path, manifest = sample_manifest
    if fault == 'duplicate': manifest['samples'].append(deepcopy(manifest['samples'][0]))
    if fault == 'old': manifest['max_age_days'] = 1
    if fault == 'version': manifest['browser_version'] = '153.0.1.2'
    if fault == 'cohort': manifest['cohorts'][0]['os'] = 'another OS'
    if fault == 'count': manifest['cohorts'][0]['min_devices'] = 2
    dump(path, manifest)
    assert corpus.review(path, now=NOW)['status'] == 'failed'


@pytest.mark.parametrize('field,value', [('probe_sha256', 'a' * 64), ('max_age_days', True),
    ('max_age_days', 0), ('max_age_days', 366), ('browser_version', '152'), ('schema_version', True),
    ('cohorts', []), ('samples', [])])
def test_manifest_is_pinned_and_bounded(sample_manifest, field, value):
    path, manifest = sample_manifest
    manifest[field] = value; dump(path, manifest)
    with pytest.raises(ValueError):
        corpus.review(path, now=NOW)


def test_tampered_browser_evidence_cannot_be_relabelled(sample_manifest):
    path, manifest = sample_manifest
    browser = path.parent / 'unit-fixture/browser.json'
    browser.write_bytes(browser.read_bytes() + b' ')
    assert corpus.review(path, now=NOW)['status'] == 'failed'


def test_cli_retains_failure_diagnostics_and_never_clobbers(tmp_path, capsys):
    manifest, output = tmp_path / 'bad.json', tmp_path / 'report.json'
    dump(manifest, {})
    assert corpus.main([str(manifest), '--output', str(output)]) == 1
    assert json.loads(output.read_text())['status'] == 'failed'
    before = output.read_bytes()
    with pytest.raises(SystemExit):
        corpus.main([str(manifest), '--output', str(output)])
    assert output.read_bytes() == before
