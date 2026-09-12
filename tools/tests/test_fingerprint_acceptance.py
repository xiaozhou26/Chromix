"""Acceptance orchestration fixtures are not native-browser evidence."""
from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fingerprint_acceptance as audit
from test_fingerprint_runtime_audits import runtime_report, render_report

HASH = 'a' * 64
VERSION = '152.0.7977.82'
INPUTS = {'series_sha256': 'b' * 64, 'patches': [{'path': 'patches/0001.patch', 'sha256': 'c' * 64}]}


def dump(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def receipt(tmp_path):
    source = tmp_path / 'src'; source.mkdir()
    (source / 'file.cc').write_bytes(b'current source\n')
    path = tmp_path / 'source.json'
    value = {'schema_version': 1, 'status': 'verified', 'method': 'reverse-forward-in-scratch',
             'identity': deepcopy(INPUTS), 'patch_count': 1,
             'outputs': {'file.cc': audit.sha256_file(source / 'file.cc')}}
    dump(path, value)
    return path, source, value


def test_current_source_and_producer_receipt(tmp_path):
    path, source, _ = receipt(tmp_path)
    for root, kind in ((source, 'live-source-hashes'), (None, 'producer-receipt-only')):
        result = audit.check_source(path, root, INPUTS, ['file.cc'])
        assert result['check'] == kind and result['sha256'] == audit.sha256_file(path)
    (source / 'file.cc').write_bytes(b'stale')
    with pytest.raises(ValueError, match='source changed'):
        audit.check_source(path, source, INPUTS, ['file.cc'])


@pytest.mark.parametrize('field,value', [
    ('schema_version', True), ('schema_version', 2), ('status', 'failed'), ('method', 'stamp-only'),
    ('identity', []), ('identity', {'series': []}), ('identity', {}), ('patch_count', True),
    ('patch_count', 2), ('outputs', {}), ('outputs', {'file.cc': 'not-a-hash'}),
    ('outputs', {'extra.cc': HASH}), ('outputs', {'file.cc': HASH, 'extra.cc': HASH}),
])
def test_bad_receipt_cannot_open_gate(tmp_path, field, value):
    path, source, data = receipt(tmp_path); data[field] = value; dump(path, data)
    with pytest.raises(ValueError):
        audit.check_source(path, source, INPUTS, ['file.cc'])


@pytest.mark.parametrize('body', ['[]', '{"k":1,"k":2}', '{"x":NaN}', '{"x":Infinity}', '{'])
def test_bad_json_and_nonobject_receipt(tmp_path, body):
    path = tmp_path / 'bad.json'; path.write_text(body, encoding='utf-8')
    with pytest.raises(ValueError):
        audit.check_source(path, None, INPUTS, ['file.cc'])


def test_oversized_report_is_not_loaded(tmp_path):
    path = tmp_path / 'huge.json'
    with path.open('wb') as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match='64 MiB'):
        audit.read_json(path)


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    path, source, _ = receipt(tmp_path)
    args = Namespace(browser=tmp_path / 'chrome', expected_sha256=HASH, expected_version=VERSION,
                     source_report=path, source_root=source, output_dir=tmp_path / 'out',
                     suite_timeout=30, control=False)
    binary = {'path': str(args.browser), 'sha256': HASH, 'size': 42}
    provenance = {'patch_inputs': deepcopy(INPUTS), 'patch_targets': ['file.cc'], 'runner_files': {'fixture': HASH}}
    monkeypatch.setattr(audit, 'binary_identity', lambda _: dict(binary))
    monkeypatch.setattr(audit, 'provenance', lambda: deepcopy(provenance))
    monkeypatch.setattr(audit, 'assess_suite', lambda name, report, *args: (
        [] if isinstance(report, dict) else ['nonobject raw report'], audit.optional_gaps(report)))
    calls, behavior = [], {}

    def command(command, *, log, **kwargs):
        script = Path(command[3]).name
        output = Path(command[command.index('--output') + 1])
        name = output.stem; calls.append(name)
        Path(log).write_text('fixture subprocess output', encoding='utf-8')
        result = {'command': command, 'timed_out': False, 'exit_code': 0, 'cleanup_errors': []}
        if script == 'fingerprint_browser_identity.py':
            dump(output, {**binary, 'version': behavior.get('version', VERSION)})
        elif name in behavior:
            behavior[name](output, result)
        else:
            dump(output, {'status': 'passed'})
        return result

    monkeypatch.setattr(audit, 'run_command', command)
    return args, calls, behavior, binary, provenance


def test_gate_scope_and_all_suites(orchestrator):
    args, calls, _, _, _ = orchestrator
    result = audit.run(args)
    assert result['status'] == 'passed' and result['ci_gate_passed']
    assert not result['full_acceptance']
    assert calls == ['browser', *(name for name, _, _ in audit.SUITES)]
    assert json.loads((args.output_dir / 'acceptance.json').read_text()) == result
    assert all(item['report_sha256'] for item in result['suites'])


def test_control_can_never_pass_ci(orchestrator):
    args, _, _, _, _ = orchestrator
    args.control, args.source_report, args.source_root = True, None, None
    result = audit.run(args)
    assert result['status'] == 'passed' and not result['ci_gate_passed'] and not result['full_acceptance']


@pytest.mark.parametrize('failure', ['launcher', 'missing', 'bad_json', 'nonobject', 'timeout', 'cleanup', 'exit'])
def test_failure_keeps_all_other_diagnostics(orchestrator, failure):
    args, calls, behavior, _, _ = orchestrator
    def fail(output, result):
        if failure == 'launcher':
            raise OSError('fixture launch failed')
        if failure == 'missing':
            return
        if failure == 'bad_json':
            output.write_text('{', encoding='utf-8'); return
        dump(output, [] if failure == 'nonobject' else {'status': 'passed'})
        if failure == 'timeout': result['timed_out'] = True
        if failure == 'cleanup': result['cleanup_errors'] = ['fixture process remained']
        if failure == 'exit': result['exit_code'] = 7
    behavior['device'] = fail
    result = audit.run(args)
    assert result['status'] == 'failed' and not result['ci_gate_passed']
    assert len(result['suites']) == len(audit.SUITES) and calls[-1] == 'render'
    assert any(error.startswith('device:') for error in result['errors'])
    assert (args.output_dir / 'render.json').exists()


@pytest.mark.parametrize('code,gate', [(1, True), (7, False), (None, False)])
def test_optional_gaps_are_incomplete_not_full_acceptance(orchestrator, code, gate):
    args, _, behavior, _, _ = orchestrator
    def unavailable(output, result):
        dump(output, {'status': 'incomplete', 'unavailable': ['webgpu']})
        result['exit_code'] = code
    behavior['render'] = unavailable
    result = audit.run(args)
    assert result['ci_gate_passed'] is gate and not result['full_acceptance']
    assert result['status'] == ('incomplete' if gate else 'failed') and result['gaps']


@pytest.mark.parametrize('mismatch', ['hash', 'version', 'source'])
def test_preflight_mismatch_stops_browser_suites(orchestrator, mismatch):
    args, calls, behavior, _, _ = orchestrator
    if mismatch == 'hash': args.expected_sha256 = 'd' * 64
    if mismatch == 'version': behavior['version'] = '153.0.0.1'
    if mismatch == 'source': args.source_report = None
    result = audit.run(args)
    assert result['errors'] and not result['ci_gate_passed'] and not result['suites']
    assert calls == (['browser'] if mismatch == 'version' else [])


@pytest.mark.parametrize('target', ['binary', 'provenance', 'source'])
def test_concurrent_input_change_fails(orchestrator, target):
    args, _, behavior, binary, provenance = orchestrator
    def changed(output, result):
        dump(output, {'status': 'passed'})
        if target == 'binary': binary['sha256'] = 'd' * 64
        if target == 'provenance': provenance['runner_files']['fixture'] = 'd' * 64
        if target == 'source': (args.source_root / 'file.cc').write_bytes(b'changed after preflight')
    behavior['render'] = changed
    result = audit.run(args)
    assert not result['ci_gate_passed'] and result['status'] == 'failed'
    assert any('changed' in error for error in result['errors'])


def test_raw_runtime_and_render_rechecked():
    runtime = {**runtime_report(), 'browser_sha256': HASH, 'browser_version': VERSION, 'status': 'passed'}
    assert audit.assess_suite('display_backend', runtime, HASH, VERSION) == ([], [])
    runtime['observations']['display_oopif']['geometry']['dpr'] = 1
    assert audit.assess_suite('display_backend', runtime, HASH, VERSION)[0]
    render = {'browser_sha256': HASH, 'browser_version': VERSION, 'status': 'passed',
              'runs': [render_report(), render_report()]}
    assert audit.assess_suite('render', render, HASH, VERSION) == ([], [])
    render['runs'][0]['observed']['exports'][0]['snapshots'] = 0
    assert audit.assess_suite('render', render, HASH, VERSION)[0]


@pytest.mark.parametrize('change', [{'browser_sha256': 'd' * 64}, {'browser_version': '153.0.0.1'},
                                    {'browser_version': None}, {'status': 'failed'}, {'errors': ['injected']}])
def test_raw_report_identity_or_failure_cannot_be_overridden(change):
    value = {'browser_sha256': HASH, 'browser_version': VERSION, 'status': 'passed',
             'runs': [render_report(), render_report()], **change}
    assert audit.assess_suite('render', value, HASH, VERSION)[0]
