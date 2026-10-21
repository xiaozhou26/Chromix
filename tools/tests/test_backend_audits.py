"""Invented observations test admission; they are not native-browser results."""
import base64
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fingerprint_backend_audit as backend
import fingerprint_media_policy_audit as media
import fingerprint_acceptance as acceptance


def clock_observation(quantum, start=0):
    step = quantum or 10
    origin = 1789390000000 // step * step
    samples = [{'now': start // step * step + i * step, 'origin': origin} for i in range(1, 7)]
    for sample in samples:
        sample.update(wall=origin + sample['now'], event=sample['now'],
                      temporal=str((origin + sample['now']) * 1000000))
    return {'samples': samples, 'timezone': 'America/New_York', 'dst': ['01:59', '03:00', '01:59', '01:00'],
        'offsets': [300, 240, 240, 300], 'locale': {'hourCycle': 'h23', 'numberingSystem': 'latn'},
        'callbacks': {'raf': {'timestamp': samples[-1]['now'], 'now': samples[-1]['now']},
                      'idle': {'remaining': step, 'now': samples[-1]['now'], 'timedOut': False}}}


def float32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def samples_hash(samples):
    return hashlib.sha256(struct.pack('<4096f', *samples)).hexdigest()


def audio_observation(name):
    source = [float32(math.sin(i / 37) * 0.47) for i in range(4096)]
    # Two invented rounding thresholds exercise the evidence validator, not
    # the C++ policy (which has separate compiled-method tests).
    threshold = 0.3 if name in ('isolated', 'restart') else 0.7
    samples = source if name == 'native' else [
        math.copysign(math.floor(abs(v) * 2**20 + threshold) / 2**20, v) for v in source]
    digest = samples_hash(samples)
    return {'samples': samples, 'hash': digest, 'copyHash': digest, 'workletHash': digest,
        'sourceHash': samples_hash(source), 'sampleRate': 44100, 'channels': 1, 'frames': 4096,
        'mutable': True, 'analyserMatches': True,
        'playback': {'inputHash': digest, 'sameSamples': True, 'state': 'running', 'frames': 4096,
                     'sampleRate': 44100, 'baseLatency': 0.01, 'outputLatency': None}}


def backend_report():
    result = {'schema_version': 1, 'status': 'passed', 'errors': [], 'runs': [],
        'browser_sha256': 'a' * 64, 'browser_version': '152.0.7977.82',
        'probe_sha256': backend.launch.pool.file_hash(backend.PROBE),
        'font_asset_sha256': backend.launch.pool.file_hash(backend.AUTHOR_FONT), 'restricted_family': 'Fixture Family'}
    caps = {'uvpaa': {'status': 'observed', 'value': False}, 'keyboard': {'status': 'observed', 'entries': [['KeyA', 'a']]},
            'speech': [{'name': 'Fixture voice', 'lang': 'en-US', 'local': True, 'default': True}],
            'pdf': True, 'plugins': [{'name': 'Fixture PDF viewer', 'filename': 'internal-pdf-viewer',
                                     'types': ['application/pdf']}]}
    for name in backend.CASES:
        mixed = name in ('isolated', 'restart')
        quantum = 0 if name == 'native' else 7 if mixed else 11
        queries = dict.fromkeys(backend.QUERY_KEYS, mixed)
        queries.update(coarse=False, none=not mixed)
        preferences = {'queries': queries, 'styles': deepcopy(queries), 'maxTouchPoints': 5 if mixed else 0,
            'touchEvent': mixed, 'author': {'background': 'rgb(0, 0, 0)' if mixed else 'rgb(3, 7, 11)',
                                          'color': 'rgb(241, 239, 233)'}}
        run = {'name': name, 'launch_args': backend.case_args(name, result['restricted_family'], caps),
            'preferences': preferences, 'preferences_reloaded': deepcopy(preferences),
            'capabilities': deepcopy(caps), 'audio': audio_observation(name),
            'clocks': {key: clock_observation(quantum, {'after_freeze': 200, 'history': 400}.get(key, 0))
                for key in ('window', 'after_freeze', 'history', 'reloaded', 'iframe', 'oopif', 'dedicated', 'shared', 'service')},
            'oopif_observed': True,
            'lifecycle_events': [{'name': event, 'trusted': True, 'timestamp': (quantum or 10) * (10 + 2 * i),
                                 'now': (quantum or 10) * (10 + 2 * i)} for i, event in enumerate(('freeze', 'resume'))],
            'history_events': [{'name': 'pageshow', 'persisted': True}],
            'input_events': [{'name': 'keydown', 'key': 'a', 'trusted': True, 'timestamp': quantum * 3}],
            'fonts': [{'family': family, 'text': text, 'platformFonts': [{'familyName': result['restricted_family'], 'glyphCount': 4}]}
                for family in ('serif', 'sans-serif', 'monospace', 'system-ui') for text in ('Aa09', '中文', '😀', '∑', '\u0378')],
            'local_fonts': {'status': 'observed', 'families': [result['restricted_family']]},
            'author_font': {'status': 'loaded', 'width': 42, 'platformFonts': [{'isCustomFont': True, 'glyphCount': 4}]}}
        if mixed:
            run['input_events'].append({'name': 'pointerdown', 'pointerType': 'touch', 'trusted': True, 'timestamp': 21})
        result['runs'].append(run)
    return result


def test_backend_launch_removes_only_conflicting_defaults():
    assert backend.IGNORED_DEFAULT_ARGS == ['--disable-back-forward-cache',
        '--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4']
    for name in backend.CASES:
        args = backend.case_args(name, 'Fixture Family', {})
        assert '--enable-blink-features=InvertedColors' in args
        assert '--enable-experimental-web-platform-features' not in args
        assert not any(arg.startswith('--blink-settings=') for arg in args)


class FixtureRoot:
    def __init__(self, cleanup_failure=False, failure_at=None, delayed=False, response=None):
        self.calls, self.listeners = [], {}
        self.cleanup_failure = cleanup_failure
        self.failure_at = failure_at
        self.delayed = delayed
        self.response = response
        self.pending = []

    def on(self, name, callback):
        if self.failure_at == 'listener':
            raise RuntimeError('listener failure')
        self.listeners[name] = callback

    def remove_listener(self, name, callback):
        assert self.listeners.pop(name) == callback
        if self.failure_at == 'remove_listener':
            raise RuntimeError('remove_listener failure')

    def detach(self):
        self.calls.append(('detach', {}))
        if self.cleanup_failure:
            raise RuntimeError('root cleanup failure')

    def send(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == self.failure_at:
            raise RuntimeError(method + ' failure')
        if method == 'Target.createBrowserContext':
            return {'browserContextId': 'clean-context'}
        if method == 'Target.createTarget':
            assert params['browserContextId'] == 'clean-context'
            return {'targetId': 'clean-target'}
        if method == 'Target.attachToTarget':
            return {'sessionId': params['targetId']}
        if method == 'Target.sendMessageToTarget':
            message = json.loads(params['message'])
            result = {'result': {'value': 42}} if message['method'] == 'Runtime.evaluate' else {}
            response = self.response if self.response is not None else {'result': result}
            if message['method'] == self.failure_at:
                response = {'error': {'code': -32000, 'message': self.failure_at + ' failure'}}
            event = {'sessionId': params['sessionId'],
                'message': json.dumps({'id': message['id'], **response})}
            if self.delayed:
                self.pending.append(event)
            else:
                self.listeners['Target.receivedMessageFromTarget'](event)
        if method == 'Browser.getVersion' and self.pending:
            self.listeners['Target.receivedMessageFromTarget'](self.pending.pop(0))
        if method == 'Target.disposeBrowserContext' and self.cleanup_failure:
            raise RuntimeError('context cleanup failure')
        return {}

    def new_browser_cdp_session(self):
        return self


@pytest.mark.parametrize('failure', [False, True])
def test_clean_cdp_fixture_has_no_playwright_media_or_focus_owner(failure):
    root = FixtureRoot(cleanup_failure=failure)
    def sample():
        with backend.clean_fixture(root) as (page, actual_root, context):
            assert actual_root is root and context == 'clean-context'
            assert page.evaluate('async value => value', 'owned') == 42
            if failure:
                raise ValueError('primary clock contract failure')
    if failure:
        with pytest.raises(ValueError, match='primary clock contract failure'):
            sample()
    else:
        sample()
    messages = [json.loads(params['message']) for method, params in root.calls
                if method == 'Target.sendMessageToTarget']
    assert [m['method'] for m in messages] == ['Page.enable', 'Runtime.enable',
        'Emulation.setFocusEmulationEnabled', 'Runtime.evaluate']
    assert messages[2]['params'] == {'enabled': False}
    assert messages[3]['params']['userGesture'] is True
    assert messages[3]['params']['awaitPromise'] is True
    assert root.calls[-2:] == [('Target.disposeBrowserContext', {'browserContextId': 'clean-context'}), ('detach', {})]
    assert not root.listeners


@pytest.mark.parametrize('failure', ['Target.createBrowserContext', 'Target.createTarget',
    'Target.attachToTarget', 'listener', 'Page.enable', 'Runtime.enable',
    'Emulation.setFocusEmulationEnabled'])
def test_clean_fixture_disposes_partial_initialization_without_masking_error(failure):
    root = FixtureRoot(cleanup_failure=True, failure_at=failure)
    with pytest.raises(RuntimeError, match=failure + ' failure'):
        with backend.clean_fixture(root):
            pytest.fail('failed initialization must not yield')
    assert root.calls[-1] == ('detach', {})
    disposals = [params for method, params in root.calls if method == 'Target.disposeBrowserContext']
    assert disposals == ([] if failure == 'Target.createBrowserContext' else
                         [{'browserContextId': 'clean-context'}])
    assert not root.listeners


@pytest.mark.parametrize('primary_failure', [False, True])
@pytest.mark.parametrize('failure', ['Target.detachFromTarget', 'remove_listener', 'context'])
def test_clean_fixture_attempts_all_cleanup_and_preserves_primary(failure, primary_failure):
    root = FixtureRoot(cleanup_failure=failure == 'context', failure_at=failure)
    expected = 'primary contract failure' if primary_failure else (
        'context cleanup failure' if failure == 'context' else failure + ' failure')
    with pytest.raises(RuntimeError, match=expected):
        with backend.clean_fixture(root):
            if primary_failure:
                raise RuntimeError('primary contract failure')
    assert root.calls[-3:] == [
        ('Target.detachFromTarget', {'sessionId': 'clean-target'}),
        ('Target.disposeBrowserContext', {'browserContextId': 'clean-context'}), ('detach', {})]
    assert not root.listeners


@pytest.mark.parametrize('primary_failure', [False, True])
def test_oopif_detach_preserves_primary_failure(primary_failure):
    root = FixtureRoot(failure_at='Target.detachFromTarget')
    expected = 'primary clock contract failure' if primary_failure else 'Target.detachFromTarget failure'
    with pytest.raises(RuntimeError, match=expected):
        with backend.FixtureSession(root, 'oopif'):
            if primary_failure:
                raise RuntimeError('primary clock contract failure')
    assert not root.listeners
    assert root.calls[-1] == ('Target.detachFromTarget', {'sessionId': 'oopif'})


@pytest.mark.parametrize(('expression', 'argument', 'wrapped'), [
    ('async value => value', {'owned': '"雪\\n'}, '(async value => value)({"owned": "\\\"\\u96ea\\\\n"})'),
    ('async () => 42', None, '(async () => 42)()'),
    ('() => 42', None, '(() => 42)()'),
    ('Promise.resolve(42)', None, 'Promise.resolve(42)'),
])
def test_fixture_evaluate_awaits_delayed_cdp_response(monkeypatch, expression, argument, wrapped):
    monkeypatch.setattr(backend.time, 'sleep', lambda _: None)
    root = FixtureRoot(delayed=True)
    with backend.FixtureSession(root, 'target') as page:
        page._received({'sessionId': 'unrelated', 'message': '{"id": 1, "result": {}}'})
        assert page.responses == {}
        assert page.evaluate(expression, argument) == 42
    message = json.loads(next(params['message'] for method, params in root.calls
                              if method == 'Target.sendMessageToTarget'))
    assert message['params'] == {'expression': wrapped, 'awaitPromise': True,
                                 'returnByValue': True, 'userGesture': True}
    assert ('Browser.getVersion', {}) in root.calls
    assert not root.pending and not root.listeners


@pytest.mark.parametrize('response', [
    {'error': {'code': -32000, 'message': 'primary clock contract failure'}},
    {'result': {'exceptionDetails': {'text': 'Uncaught (in promise)',
                                   'exception': {'description': 'primary clock contract failure'}}}},
])
def test_fixture_evaluate_propagates_protocol_and_promise_errors(response):
    with backend.FixtureSession(FixtureRoot(response=response), 'target') as page:
        with pytest.raises(RuntimeError, match='primary clock contract failure'):
            page.evaluate('Promise.reject(new Error("primary clock contract failure"))')


@pytest.mark.parametrize('message', ['Execution context was destroyed.',
                                    'Cannot find context with specified id'])
def test_fixture_ready_retries_only_navigation_context_errors(monkeypatch, message):
    monkeypatch.setattr(backend.time, 'sleep', lambda _: None)
    responses = iter([backend.FixtureProtocolError('Runtime.evaluate', {'code': -32000, 'message': message}),
                      False, True])
    with backend.FixtureSession(FixtureRoot(), 'target') as page:
        def evaluate(expression):
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            assert 'location.href === "http://owned/"' in expression
            return result
        monkeypatch.setattr(page, 'evaluate', evaluate)
        page.ready('http://owned/')
        assert list(responses) == []


@pytest.mark.parametrize('error', [RuntimeError('primary JavaScript contract failure'),
    backend.FixtureProtocolError('Runtime.evaluate', {'code': -32000, 'message': 'Target closed'}),
    backend.FixtureProtocolError('Runtime.evaluate', {'code': -32602, 'message': 'Execution context was destroyed.'})])
def test_fixture_wait_does_not_hide_unrelated_errors(monkeypatch, error):
    with backend.FixtureSession(FixtureRoot(), 'target') as page:
        def evaluate(_):
            raise error
        monkeypatch.setattr(page, 'evaluate', evaluate)
        with pytest.raises(RuntimeError) as observed:
            page.wait_for('true')
        assert observed.value is error


@pytest.fixture
def backend_clock(monkeypatch):
    # Keep finite test clocks out of psutil, subprocess and the event loop.
    clock = SimpleNamespace(monotonic=time.monotonic, sleep=time.sleep)
    monkeypatch.setattr(backend, 'time', clock)
    return clock


@pytest.fixture
def snapshot_child():
    with subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                          stdin=subprocess.PIPE) as child:
        try:
            yield child
        finally:
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise


@pytest.mark.parametrize('waiting', [False, True])
def test_fixture_timeout_is_bounded(monkeypatch, waiting, backend_clock):
    ticks = iter([0, 31])
    monkeypatch.setattr(backend_clock, 'monotonic', lambda: next(ticks))
    with backend.FixtureSession(FixtureRoot(), 'target') as page:
        if waiting:
            monkeypatch.setattr(page, 'evaluate', lambda _: False)
            with pytest.raises(TimeoutError, match='fixture condition'):
                page.wait_for('false')
        else:
            page.root.listeners['Target.receivedMessageFromTarget'] = lambda _: None
            with pytest.raises(TimeoutError, match='Runtime.evaluate'):
                page.evaluate('new Promise(() => {})')
            page.root.listeners['Target.receivedMessageFromTarget'] = page.listener


def test_fixture_diagnostics_preserve_commands_and_results(tmp_path):
    roots = [FixtureRoot(delayed=True), FixtureRoot(delayed=True)]
    diagnostics = backend.BackendDiagnostics(tmp_path / 'backend.cdp.jsonl')
    diagnostics.start('native', backend.case_args('native', '', {}))
    for root, trace in zip(roots, (None, diagnostics)):
        with backend.clean_fixture(root, trace) as (page, _, _):
            assert backend.evaluate(page, 'clocks', 'clocks.window') == 42
    assert roots[0].calls == roots[1].calls
    rows = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
    command = next(row for row in rows if row.get('method') == 'Runtime.evaluate')
    assert command['case'] == 'native' and command['phase'] == 'clocks.window'
    assert command['params']['expression'] == '(async method => await chromixBackendProbe[method]())("clocks")'
    assert command['target'] == command['session_id'] == 'clean-target'
    assert any(row['event'] == 'command_completed' and row['sequence'] == command['sequence'] for row in rows)
    assert 'failure' not in diagnostics.current


def test_fixture_timeout_diagnostics_precede_cleanup_and_do_not_retry(
        monkeypatch, tmp_path, backend_clock, snapshot_child):
    diagnostics = backend.BackendDiagnostics(tmp_path / 'backend.cdp.jsonl')
    diagnostics.start('native', [])
    root = FixtureRoot()
    with pytest.raises(TimeoutError, match='CDP fixture timed out: Runtime.evaluate'):
        with backend.clean_fixture(root, diagnostics) as (page, _, _):
            page.phase = 'audio'
            root.listeners['Target.receivedMessageFromTarget'] = lambda _: None
            ticks = iter([0, 1, 31])
            real_monotonic, real_sleep = time.monotonic, time.sleep
            monkeypatch.setattr(backend_clock, 'monotonic', lambda: next(ticks))
            monkeypatch.setattr(backend_clock, 'sleep', lambda _: None)
            assert time.monotonic is real_monotonic and time.sleep is real_sleep
            assert subprocess._time is real_monotonic
            try:
                page.evaluate('chromixBackendProbe.audio()')
            finally:
                root.listeners['Target.receivedMessageFromTarget'] = page.listener
                failure = diagnostics.current['failure']
                assert not any(method == 'Target.disposeBrowserContext' for method, _ in root.calls)
    assert failure['last_command']['phase'] == 'audio'
    assert failure['last_command']['dispatcher_round_trips'] == 1
    assert failure['processes'][0]['pid'] == os.getpid()
    child = next(process for process in failure['processes'] if process['pid'] == snapshot_child.pid)
    assert child['ppid'] == os.getpid()
    assert child['cmdline'] and 'cpu_times' in child and 'memory_info' in child
    assert snapshot_child.poll() is None
    assert 'process_error' not in failure
    assert list(ticks) == []
    messages = [json.loads(params['message']) for method, params in root.calls
                if method == 'Target.sendMessageToTarget']
    assert sum(message['method'] == 'Runtime.evaluate' for message in messages) == 1
    assert sum(method == 'Browser.getVersion' for method, _ in root.calls) == 1
    rows = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
    assert sum(row['event'] == 'failure' for row in rows) == 1
    assert root.calls[-1] == ('detach', {})


def test_diagnostics_record_target_events_and_ignore_other_sessions(tmp_path):
    diagnostics = backend.BackendDiagnostics(tmp_path / 'backend.cdp.jsonl')
    diagnostics.start('native', [])
    with backend.FixtureSession(FixtureRoot(), 'oopif', diagnostics) as page:
        for session in ('unrelated', 'oopif'):
            page._received({'sessionId': session, 'message': json.dumps({
                'method': 'Runtime.exceptionThrown', 'params': {'exceptionDetails': {'text': 'owned'}}})})
    events = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
    assert len(events) == 2
    assert events[-1]['event'] == 'target_event'
    assert events[-1]['target'] == 'oopif'


def test_navigation_protocol_error_is_not_a_terminal_diagnostic_failure(monkeypatch, tmp_path):
    diagnostics = backend.BackendDiagnostics(tmp_path / 'backend.cdp.jsonl')
    diagnostics.start('native', [])
    root = FixtureRoot(response={'error': {'code': -32000, 'message': 'Execution context was destroyed.'}})
    with backend.FixtureSession(root, 'target', diagnostics) as page:
        monkeypatch.setattr(backend.time, 'sleep', lambda _: setattr(root, 'response', None))
        page.wait_for('true')
    assert 'failure' not in diagnostics.current
    rows = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
    assert any(row['event'] == 'command_failed' for row in rows)
    assert not any(row['event'] == 'failure' for row in rows)


def test_diagnostic_io_and_process_errors_do_not_mask_primary_failure(monkeypatch, tmp_path):
    diagnostics = backend.BackendDiagnostics(tmp_path / 'missing' / 'backend.cdp.jsonl')
    diagnostics.start('native', [])
    monkeypatch.setitem(sys.modules, 'psutil', None)
    with pytest.raises(ValueError, match='primary contract failure'):
        with backend.clean_fixture(FixtureRoot(), diagnostics):
            raise ValueError('primary contract failure')
    assert diagnostics.errors
    assert diagnostics.current['failure']['error'] == 'ValueError: primary contract failure'
    assert diagnostics.current['failure']['process_error']


def test_backend_diagnostics_cannot_satisfy_an_incomplete_matrix():
    report = backend_report()
    report['diagnostics'] = {'cases': deepcopy(report['runs'])}
    report['runs'] = []
    assert 'backend launch/restart matrix did not complete' in backend.assess(report)[0]


def test_backend_run_keeps_failed_native_out_of_matrix(monkeypatch, tmp_path):
    from types import SimpleNamespace
    root = FixtureRoot()
    root.version = '155.0.8059.39'
    closed, launches = [], []
    root.close = lambda: closed.append(True)
    def launch(**kwargs):
        launches.append(kwargs)
        return root
    @backend.contextmanager
    def fake_playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    @backend.contextmanager
    def server():
        yield 'http://owned'
    def collect(page, *args):
        page.phase = 'audio'
        page.evaluate('chromixBackendProbe.audio()')
        raise TimeoutError('CDP fixture timed out: Runtime.evaluate')
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=fake_playwright))
    monkeypatch.setattr(backend, 'server', server)
    monkeypatch.setattr(backend, 'collect', collect)
    executable = tmp_path / 'browser'
    executable.write_bytes(b'fixture')
    diagnostics = backend.BackendDiagnostics(tmp_path / 'backend.cdp.jsonl')
    report = backend.run(executable, diagnostics=diagnostics)
    assert report['status'] == 'failed' and report['runs'] == []
    assert report['errors'] == ['TimeoutError: CDP fixture timed out: Runtime.evaluate',
                                'backend launch/restart matrix did not complete']
    assert len(launches) == 1 and closed == [True]
    assert launches[0]['args'] == backend.case_args('native', '', {})
    assert launches[0]['ignore_default_args'] == backend.IGNORED_DEFAULT_ARGS
    case = report['diagnostics']['cases'][0]
    assert case['name'] == 'native' and case['status'] == 'failed'
    assert case['failure']['last_command']['phase'] == 'audio'
    assert json.loads(json.dumps(report))['diagnostics']['trace'] == 'backend.cdp.jsonl'


@pytest.mark.parametrize('debug', [None, '', 'pw:api'])
def test_backend_cli_enables_browser_stderr_without_changing_report(monkeypatch, tmp_path, debug):
    executable = tmp_path / 'browser'
    executable.touch()
    output = tmp_path / 'output' / 'backend_policy.json'
    if debug is None:
        monkeypatch.delenv('DEBUG', raising=False)
    else:
        monkeypatch.setenv('DEBUG', debug)
    report = {'status': 'failed', 'errors': ['primary contract failure'], 'gaps': []}
    def run(browser, headed, diagnostics):
        assert browser == executable and headed is False
        assert os.environ['DEBUG'] == (debug + ',' if debug else '') + 'pw:browser'
        assert diagnostics.path == output.with_suffix('.cdp.jsonl')
        return report
    monkeypatch.setattr(backend, 'run', run)
    assert backend.main(['--browser', str(executable), '--output', str(output)]) == 1
    assert json.loads(output.read_text()) == report
    assert os.environ.get('DEBUG') == debug


def test_freeze_resume_releases_focus_capture_then_restores_visibility(monkeypatch):
    calls = []
    monkeypatch.setattr(backend.time, 'sleep', lambda seconds: calls.append(('sleep', seconds)))
    with backend.FixtureSession(FixtureRoot(), 'target') as page:
        monkeypatch.setattr(page, 'send', lambda method, params: calls.append((method, params)))
        monkeypatch.setattr(page, 'wait_for', lambda expression: calls.append(('wait', expression)))
        page.freeze_resume()
        page.freeze_resume()
    expected = [('Emulation.setFocusEmulationEnabled', {'enabled': False}),
        ('Page.setWebLifecycleState', {'state': 'frozen'}), ('sleep', 0.08),
        ('Page.setWebLifecycleState', {'state': 'active'}),
        ('Emulation.setFocusEmulationEnabled', {'enabled': True}),
        ('wait', 'document.visibilityState === "visible"')]
    assert calls == expected * 2


@pytest.mark.parametrize('interrupted', [False, True])
def test_freeze_resume_cleanup_preserves_interrupt_or_reports_resume_failure(monkeypatch, interrupted):
    calls = []
    with backend.FixtureSession(FixtureRoot(), 'target') as page:
        def send(method, params):
            calls.append((method, params))
            if params == {'state': 'active'}:
                raise RuntimeError('resume failure')
        def sleep(_):
            if interrupted:
                raise KeyboardInterrupt('primary interrupt')
        monkeypatch.setattr(page, 'send', send)
        monkeypatch.setattr(backend.time, 'sleep', sleep)
        with pytest.raises(KeyboardInterrupt if interrupted else RuntimeError,
                           match='primary interrupt' if interrupted else 'resume failure'):
            page.freeze_resume()
    assert calls[-1] == ('Page.setWebLifecycleState', {'state': 'active'})
    assert ('Emulation.setFocusEmulationEnabled', {'enabled': True}) not in calls


@pytest.mark.parametrize('primary_failure', [False, True])
def test_clean_fixture_real_cdp_lifecycle_async_and_cleanup(primary_failure, tmp_path):
    executable = os.environ.get('CHROMIX_BACKEND_TEST_BROWSER')
    if not executable:
        pytest.skip('explicit Chromium executable required for CDP fixture integration')
    from playwright.sync_api import sync_playwright
    with backend.server() as origin, sync_playwright() as pw:
        instance = pw.chromium.launch(executable_path=executable, headless=True, chromium_sandbox=False,
            args=backend.case_args('native', '', {}), ignore_default_args=backend.IGNORED_DEFAULT_ARGS)
        try:
            inspector = instance.new_browser_cdp_session()
            try:
                before = inspector.send('Target.getBrowserContexts')['browserContextIds']
                diagnostics = backend.BackendDiagnostics(tmp_path / 'integration.cdp.jsonl')
                diagnostics.start('native', backend.case_args('native', '', {}))
                def collect():
                    with backend.clean_fixture(instance, diagnostics) as (page, root, context):
                        assert context not in before
                        page.goto(origin + '/')
                        assert page.evaluate('async value => {await new Promise(r => setTimeout(r, 20)); return value;}',
                                             {'owned': [42, '雪']}) == {'owned': [42, '雪']}
                        with pytest.raises(RuntimeError, match='owned rejection'):
                            page.evaluate('async () => {await Promise.resolve(); throw new Error("owned rejection");}')
                        out = backend.collect(page, root, context, origin, 'native')
                        assert set(out['clocks']) == {'window', 'after_freeze', 'history', 'reloaded',
                                                     'iframe', 'oopif', 'dedicated', 'shared', 'service'}
                        for name in ('window', 'after_freeze', 'history', 'reloaded', 'iframe', 'oopif'):
                            assert backend.finite(out['clocks'][name]['callbacks']['raf']['timestamp'])
                        lifecycle = [event for event in out['lifecycle_events'] if event['name'] in ('freeze', 'resume')]
                        assert [event['name'] for event in lifecycle] == ['freeze', 'resume']
                        assert all(event['trusted'] for event in lifecycle)
                        assert any(event['name'] == 'pageshow' and event['persisted'] for event in out['history_events'])
                        assert out['oopif_observed']
                        page.freeze_resume()
                        assert page.evaluate('document.visibilityState') == 'visible'
                        assert backend.finite(backend.evaluate(page, 'clocks')['callbacks']['raf']['timestamp'])
                        if primary_failure:
                            raise ValueError('primary integration failure')
                if primary_failure:
                    with pytest.raises(ValueError, match='primary integration failure'):
                        collect()
                else:
                    collect()
                assert inspector.send('Target.getBrowserContexts')['browserContextIds'] == before
                rows = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
                assert any(row['event'] == 'target_event' for row in rows)
                assert any(row.get('phase') == 'clocks.oopif' for row in rows)
                assert bool(diagnostics.current.get('failure')) is primary_failure
            finally:
                inspector.detach()
        finally:
            instance.close()


def test_backend_malformed_evidence_keeps_primary_collection_error():
    report = backend_report()
    report['errors'] = ['primary clock contract failure']
    report['runs'][1]['clocks'] = None
    errors, _ = backend.assess(report)
    assert errors[0] == 'primary clock contract failure'
    assert errors[1].startswith('malformed backend evidence:')


@pytest.mark.parametrize('fault', ['raf', 'untrusted', 'reversed', 'surrounding', 'css'])
def test_stage9_failures_remain_strict_raw_contracts(fault):
    report = backend_report()
    run = report['runs'][1]
    if fault == 'raf':
        run['clocks']['window']['callbacks']['raf'] = {'timestamp': 288.996, 'now': 294}
    elif fault == 'untrusted':
        run['lifecycle_events'][0]['trusted'] = False
    elif fault == 'reversed':
        run['lifecycle_events'].reverse()
    elif fault == 'surrounding':
        run['lifecycle_events'][0]['now'] = 0
    else:
        run['preferences']['styles']['anyCoarse'] = False
    before = deepcopy(report)
    assert backend.assess(report)[0]
    assert report == before


def test_backend_recomputes_queries_clock_grids_samples_and_restarts():
    assert backend.assess(backend_report()) == ([], [])


@pytest.mark.parametrize('mutate', [
    lambda r: r['runs'].pop(),
    lambda r: r.update(probe_sha256='0' * 64),
    lambda r: r.update(font_asset_sha256='0' * 64),
    lambda r: r.update(restricted_family='Unobserved Font'),
    lambda r: r['runs'][1]['launch_args'].append('--uxr-timer-resolution=0'),
    lambda r: r['runs'][1]['preferences']['styles'].update(dark=False),
    lambda r: r['runs'][1]['preferences'].update(maxTouchPoints=0),
    lambda r: r['runs'][1]['preferences'].update(touchEvent=False),
    lambda r: r['runs'][1]['preferences']['author'].update(background='rgb(3, 7, 11)'),
    lambda r: r['runs'][3]['preferences']['author'].update(color='rgb(0, 0, 0)'),
    lambda r: r['runs'][1]['clocks'].pop('service'),
    lambda r: r['runs'][1].update(oopif_observed=False),
    lambda r: r['runs'][1]['clocks']['oopif']['samples'][0].update(now=0.125),
    lambda r: r['runs'][1]['lifecycle_events'].pop(),
    lambda r: r['runs'][1]['clocks']['history']['samples'][0].update(now=7),
    lambda r: r['runs'][1]['clocks']['shared']['samples'][0].update(now=0.125),
    lambda r: r['runs'][1]['clocks']['dedicated']['samples'][0].update(wall=0),
    lambda r: r['runs'][1]['clocks']['service']['samples'][0].update(temporal='123'),
    lambda r: r['runs'][1]['clocks']['window']['samples'][0].update(temporal=123),
    lambda r: r['runs'][1]['clocks']['iframe'].update(offsets=[300] * 4),
    lambda r: r['runs'][1]['clocks']['window'].update(timezone='UTC'),
    lambda r: r['runs'][1]['clocks']['window']['locale'].update(hourCycle='h12'),
    lambda r: r['runs'][1]['clocks']['window']['callbacks']['raf'].update(timestamp=1.5),
    lambda r: r['runs'][1]['clocks']['window']['callbacks']['idle'].update(remaining=0.123),
    lambda r: r['runs'][1]['clocks']['after_freeze']['samples'][0].update(origin=0),
    lambda r: r['runs'][1]['input_events'].pop(),
    lambda r: r['runs'][1]['input_events'][0].update(trusted=False),
    lambda r: r['runs'][1]['input_events'][0].update(timestamp=1),
    lambda r: r['runs'][1]['audio']['samples'].__setitem__(1, 0.1),
    lambda r: r['runs'][1]['audio'].update(copyHash='0' * 64),
    lambda r: r['runs'][1]['audio'].update(workletHash='0' * 64),
    lambda r: r['runs'][1]['audio'].update(analyserMatches=False),
    lambda r: r['runs'][1]['audio'].update(sourceHash='0' * 64),
    lambda r: r['runs'][1]['audio'].update(mutable=False),
    lambda r: r['runs'][1]['audio']['playback'].update(sameSamples=False),
    lambda r: r['runs'][1]['audio']['playback'].update(baseLatency=-1),
    lambda r: r['runs'][2].update(audio=deepcopy(r['runs'][3]['audio'])),
    lambda r: r['runs'][3].update(audio=deepcopy(r['runs'][1]['audio'])),
    lambda r: r['runs'][1]['fonts'][0]['platformFonts'][0].update(familyName='Host Font'),
    lambda r: r['runs'][1]['fonts'][0]['platformFonts'].clear(),
    lambda r: r['runs'][1]['local_fonts']['families'].append('Host Font'),
    lambda r: r['runs'][1]['author_font']['platformFonts'][0].update(isCustomFont=False),
    lambda r: r['runs'][1]['capabilities']['uvpaa'].update(value=True),
    lambda r: r['runs'][1]['capabilities'].update(speech=[{'name': 'Invented voice'}]),
    lambda r: r['runs'][1]['capabilities'].update(pdf=False),
])
def test_backend_saved_pass_cannot_override_broken_observations(mutate):
    report = backend_report()
    mutate(report)
    assert backend.assess(report)[0]
    assert acceptance.assess_suite('backend_policy', report, 'a' * 64, '152.0.7977.82')[0]


def test_backend_unavailable_optional_features_are_recomputed_gaps():
    report = backend_report()
    for run in report['runs']:
        run['history_events'] = [{'name': 'pageshow', 'persisted': False}]
        run['local_fonts'] = {'status': 'unavailable', 'error': 'NotAllowedError'}
        for clock in run['clocks'].values():
            for sample in clock['samples']:
                sample['temporal'] = None
    errors, gaps = backend.assess(report)
    assert not errors
    assert 'native.bfcache' in gaps and 'isolated.service.Temporal' in gaps
    assert 'other_seed.local_fonts' in gaps


def test_empty_asynchronous_speech_inventory_does_not_attest_backend_absence():
    report = backend_report()
    for run in report['runs']:
        run['capabilities']['speech'] = []
    errors, gaps = backend.assess(report)
    assert not errors
    assert gaps == sorted(name + '.speech_inventory' for name in backend.CASES)


@pytest.mark.parametrize(('feature', 'value'), [
    ('uvpaa', {'status': 'observed', 'value': 'false'}),
    ('uvpaa', {'status': 'unavailable'}),
    ('keyboard', {'status': 'ignored'}),
    ('keyboard', {'status': 'observed', 'entries': 'KeyA=a'}),
    ('keyboard', {'status': 'observed', 'entries': [['KeyA', 'a'], ['KeyA', 'b']]}),
    ('plugins', [{'name': 'PDF', 'filename': 'internal-pdf-viewer', 'types': 'application/pdf'}]),
])
def test_matching_malformed_capability_inventories_do_not_pass(feature, value):
    report = backend_report()
    for run in report['runs']:
        run['capabilities'][feature] = deepcopy(value)
    # Recompute the intentional negative hint so a launch-argument mismatch
    # cannot be the reason this malformed but identical inventory is rejected.
    for run in report['runs']:
        run['launch_args'] = backend.case_args(run['name'], report['restricted_family'], report['runs'][0]['capabilities'])
    assert backend.assess(report)[0]
    assert acceptance.assess_suite('backend_policy', report, 'a' * 64, '152.0.7977.82')[0]


def test_empty_keyboard_inventory_remains_an_observation_gap():
    report = backend_report()
    for run in report['runs']:
        run['capabilities']['keyboard']['entries'] = []
    errors, gaps = backend.assess(report)
    assert not errors
    assert gaps == sorted(name + '.keyboard_inventory' for name in backend.CASES)


def recorded(family='vp8'):
    data = b'owned invented codec fixture'
    return {'status': 'recorded', 'data': base64.b64encode(data).decode(), 'bytes': len(data),
            'mime': 'video/webm;codecs=' + family}


def decoded(frames=False):
    return {'status': 'decoded', **({'frames': [{'width': 64, 'height': 64}]} if frames else {'width': 64, 'height': 64})}


def rejection():
    return {'status': 'rejected', 'error': 'NotSupportedError'}


def media_report():
    report = {'schema_version': 1, 'status': 'passed', 'errors': [], 'runs': [],
        'browser_sha256': 'a' * 64, 'browser_version': '152.0.7977.82',
        'probe_sha256': media.launch.pool.file_hash(media.PROBE)}
    for name, disabled in media.CASES.items():
        observation = {'families': {}, 'apis': dict.fromkeys(('encoder', 'decoder', 'recorder', 'mse'), True),
                       'audio': {'status': 'recorded', 'bytes': 123, 'mime': 'audio/webm;codecs=opus'}}
        for family in media.FAMILIES:
            allowed = family not in disabled
            observation['families'][family] = {**dict.fromkeys(media.API_KEYS, allowed),
                'rtcSend': int(allowed), 'rtcReceive': int(allowed), 'canPlay': 'probably' if allowed else '',
                'encoding': dict.fromkeys(('supported', 'smooth', 'powerEfficient'), allowed),
                'decoding': dict.fromkeys(('supported', 'smooth', 'powerEfficient'), allowed),
                'encoded': {'status': 'encoded', 'config': {'codec': family},
                    'chunks': [{'data': 'eHg=', 'type': 'key', 'timestamp': 0}]} if allowed else rejection(),
                'recorded': recorded(family) if allowed else rejection(),
                'decoded': decoded(True) if allowed else rejection(),
                'playback': decoded() if allowed else rejection(), 'mse': decoded() if allowed else rejection()}
        observation['defaultRecorder'] = rejection() if name == 'disabled' else {**recorded(), 'playback': decoded()}
        observation['rtc'] = rejection() if name == 'disabled' else {
            'status': 'decoded', 'frames': 3, 'bytes': 456, 'codec': 'video/VP8', 'errors': [],
            'formats': [f for f in media.FAMILIES if f not in disabled]}
        report['runs'].append({'name': name, 'launch_args': media.case_args(name), 'observation': observation})
    return report


def test_media_revalidates_native_fixtures_and_actual_codec_rejection():
    assert media.assess(media_report()) == ([], [])


@pytest.mark.parametrize('mutate', [
    lambda r: r['runs'].pop(),
    lambda r: r.update(probe_sha256='0' * 64),
    lambda r: r['runs'][1]['launch_args'].clear(),
    lambda r: r['runs'][0]['observation']['apis'].update(encoder=False),
    lambda r: r['runs'][0]['observation']['families']['vp8']['encoded'].update(chunks=[]),
    lambda r: r['runs'][0]['observation']['families']['vp8']['recorded'].update(data='!'),
    lambda r: r['runs'][0]['observation']['families']['vp8']['recorded'].update(bytes=0),
    lambda r: r['runs'][0]['observation']['families']['vp8']['decoded'].update(frames=[]),
    lambda r: r['runs'][0]['observation']['families']['vp8']['mse'].update(width=32),
    lambda r: r['runs'][1]['observation']['families']['vp8'].update(encoderSupported=True),
    lambda r: r['runs'][1]['observation']['families']['vp8'].update(canPlay='maybe'),
    lambda r: r['runs'][1]['observation']['families']['vp8'].update(rtcReceive=1),
    lambda r: r['runs'][1]['observation']['families']['vp8']['encoding'].update(supported=True),
    lambda r: r['runs'][1]['observation']['families']['vp8']['decoding'].update(smooth=True),
    lambda r: r['runs'][1]['observation']['families']['vp8'].update(encoded={'status': 'encoded', 'chunks': []}),
    lambda r: r['runs'][1]['observation']['families']['vp8'].update(recorded=recorded()),
    lambda r: r['runs'][1]['observation']['families']['vp8'].update(decoded=decoded(True)),
    lambda r: r['runs'][1]['observation']['families']['vp8'].update(playback=decoded()),
    lambda r: r['runs'][1]['observation']['families']['vp8'].update(mse=decoded()),
    lambda r: r['runs'][1]['observation']['families']['vp8']['recorded'].update(error='Error:backend probe timed out'),
    lambda r: r['runs'][1]['observation'].update(defaultRecorder=recorded()),
    lambda r: r['runs'][1]['observation'].update(rtc={'status': 'offered', 'formats': ['vp8']}),
    lambda r: r['runs'][1]['observation']['rtc'].update(error='ReferenceError'),
    lambda r: r['runs'][1]['observation']['audio'].update(bytes=0),
    lambda r: r['runs'][2]['observation']['defaultRecorder'].update(mime='video/webm;codecs=vp9'),
    lambda r: r['runs'][2]['observation']['rtc'].update(codec='video/H264'),
    lambda r: r['runs'][2]['observation']['rtc'].update(frames=0),
    lambda r: r['runs'][2]['observation']['families']['vp8'].update(recorderSupported=False),
])
def test_media_saved_pass_cannot_hide_operation_failures(mutate):
    report = media_report()
    mutate(report)
    assert media.assess(report)[0]
    assert acceptance.assess_suite('media_policy', report, 'a' * 64, '152.0.7977.82')[0]


@pytest.mark.parametrize('key', ('encoding', 'decoding'))
def test_missing_media_capabilities_result_is_explicit_and_does_not_abort_other_checks(key):
    report = media_report()
    for run in report['runs']:
        run['observation']['families']['h264'][key] = None
    errors, gaps = media.assess(report)
    assert any('h264: missing MediaCapabilities result: ' + key in error for error in errors)
    assert not any("NoneType" in error for error in errors)
    assert gaps == []


def test_missing_media_capabilities_retains_disabled_operation_failures_and_gaps():
    report = media_report()
    report['runs'][1]['observation']['families']['h264']['encoding'] = None
    report['runs'][1]['observation']['families']['h264']['encoded'] = {'status': 'encoded', 'config': {'codec': 'h264'},
        'chunks': [{'data': 'eHg=', 'type': 'key', 'timestamp': 0}]}
    errors, gaps = media.assess(report)
    assert any('disabled codec still advertised' in error for error in errors) is False
    assert any('disabled encoded operation was not rejected' in error for error in errors)
    assert any('missing MediaCapabilities result: encoding' in error for error in errors)


def test_missing_media_capabilities_does_not_hide_native_fixture_gaps():
    report = media_report()
    for run in report['runs']:
        row = run['observation']['families']['hevc']
        row['encoding'] = None
        if run['name'] == 'native':
            row.update(**dict.fromkeys(media.API_KEYS, False), canPlay='', rtcSend=0, rtcReceive=0,
                       encoded={'status': 'unavailable'}, recorded={'status': 'unavailable'})
        row.update(decoded={'status': 'unavailable'}, playback={'status': 'unavailable'}, mse={'status': 'unavailable'})
    errors, gaps = media.assess(report)
    assert any('missing MediaCapabilities result: encoding' in error for error in errors)
    assert 'native.hevc.native_encoder' in gaps
    assert 'disabled.hevc.decoded.no_native_fixture' in gaps


def test_missing_hardware_codec_is_a_gap_not_fabricated_operation_coverage():
    report = media_report()
    for run in report['runs']:
        row = run['observation']['families']['hevc']
        if run['name'] == 'native':
            row.update(**dict.fromkeys(media.API_KEYS, False), canPlay='', rtcSend=0, rtcReceive=0,
                       encoded={'status': 'unavailable'}, recorded={'status': 'unavailable'})
        row.update(decoded={'status': 'unavailable'}, playback={'status': 'unavailable'}, mse={'status': 'unavailable'})
    errors, gaps = media.assess(report)
    assert not errors
    assert 'disabled.hevc.decoded.no_native_fixture' in gaps
    assert 'native.hevc.native_encoder' in gaps


def test_native_encode_without_a_native_decode_control_retains_rejection_gap():
    report = media_report()
    native = report['runs'][0]['observation']['families']['hevc']
    native.update(decoderSupported=False, canPlay='', mseSupported=False,
                  decoded=rejection(), playback=rejection(), mse={'status': 'unavailable'})
    errors, gaps = media.assess(report)
    assert not errors
    assert 'disabled.hevc.decoded.no_native_decode_control' in gaps
    assert 'vp8_only.hevc.playback.no_native_decode_control' in gaps


@pytest.mark.parametrize('report', [None, [], {}, {'runs': []}, {'runs': [None]}])
def test_malformed_reports_fail_closed(report):
    assert backend.assess(report)[0]
    assert media.assess(report)[0]


def test_valid_reports_are_rechecked_by_release_gate():
    for name, report in (('backend_policy', backend_report()), ('media_policy', media_report())):
        assert acceptance.assess_suite(name, report, 'a' * 64, '152.0.7977.82') == ([], [])


def test_owned_assets_workers_and_font_responses():
    with backend.server() as origin:
        for path, content in [('/probe.js', backend.PROBE.read_bytes()), ('/author.ttf', backend.AUTHOR_FONT.read_bytes()),
                              ('/audio-worklet.js', backend.WORKLET.encode())]:
            with urllib.request.urlopen(origin + path, timeout=5) as response:
                assert response.read() == content
        for path in backend.WORKERS:
            with urllib.request.urlopen(origin + path, timeout=5) as response:
                assert response.read().startswith(b"importScripts('/probe.js');")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(origin, headers={'Host': 'external.invalid'}), timeout=5)
        assert error.value.code == 403


def test_worklet_tap_crops_exact_frames_without_changing_graph_output(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the fixture worklet')
    script = tmp_path / 'worklet.cjs'
    script.write_text("""const vm=require('node:vm'), assert=require('node:assert/strict');
const messages=[]; let Processor;
const context={currentFrame:0,Float32Array,AudioWorkletProcessor:class {constructor(){this.port={postMessage:m=>messages.push(m)};}},
  registerProcessor:(name,ctor)=>{assert.equal(name,'backend-tap');Processor=ctor;}};
vm.createContext(context);
""" + 'vm.runInContext(' + json.dumps(backend.WORKLET) + ',context);\n' + """
const p=new Processor({processorOptions:{start:64,frames:301}});
for(let block=0;block<4;block++) {
  context.currentFrame=block*128;
  const input=Float32Array.from({length:128},(_,i)=>block*128+i+1), output=new Float32Array(128);
  const alive=p.process([[input]],[[output]]);assert.deepEqual(input,output);
  assert.equal(alive,block<2);
}
const result=new Float32Array(301);
for(const message of messages)result.set(message.samples,message.offset);
assert.equal(messages.reduce((n,m)=>n+m.samples.length,0),301);
assert.deepEqual(result,Float32Array.from({length:301},(_,i)=>i+65));
""", encoding='utf-8')
    subprocess.run([node, str(script)], check=True, timeout=15, capture_output=True, text=True)
