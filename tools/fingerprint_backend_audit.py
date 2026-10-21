#!/usr/bin/env python3
"""Exercise launch preferences, clocks, audio and restricted fonts in a supplied browser.

The owned fixtures inspect computed styles, actual audio samples and native font
records. They do not attest physical input/audio devices or cross-platform pixels.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import struct
import threading
import time
from urllib.parse import urlsplit

from device_p0_audit import FONT_NODES, font_sample_errors, launch

PROBE = Path(__file__).with_name('fingerprint_backend_probe.js')
AUTHOR_FONT = Path(__file__).resolve().parents[1] / 'assets/fonts/Arial-Regular.ttf'
CASES = ('native', 'isolated', 'restart', 'other_seed')
QUERY_KEYS = ('dark', 'contrast', 'forced', 'motion', 'transparency', 'inverted',
              'fine', 'coarse', 'none', 'anyFine', 'anyCoarse', 'hover')
BASE_ARGS = [arg for arg in launch.NATIVE_ARGS
             if arg not in ('--fingerprint=off', '--uxr-disable-fingerprint-noise')]
BASE_ARGS += ['--uxr-timezone=America/New_York', '--uxr-languages=en-US',
              '--enable-blink-features=InvertedColors',
              '--autoplay-policy=no-user-gesture-required', '--site-per-process']
IGNORED_DEFAULT_ARGS = ['--disable-back-forward-cache',
    '--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4']
WORKERS = {
    '/clock-dedicated.js': "onmessage=async()=>postMessage(await chromixBackendProbe.clocks());",
    '/clock-shared.js': "onconnect=e=>{const p=e.ports[0];p.onmessage=async()=>p.postMessage(await chromixBackendProbe.clocks());p.start();};",
    '/clock-service.js': "oninstall=e=>e.waitUntil(skipWaiting());onactivate=e=>e.waitUntil(clients.claim());onmessage=e=>e.waitUntil(chromixBackendProbe.clocks().then(v=>e.ports[0].postMessage(v)));",
}
WORKLET = r"""registerProcessor('backend-tap', class extends AudioWorkletProcessor {
  constructor(options) { super(); this.start=options.processorOptions.start; this.frames=options.processorOptions.frames; }
  process(inputs, outputs) {
    const input=inputs[0]?.[0], output=outputs[0]?.[0];
    if (output) { output.fill(0); if(input) output.set(input); }
    const lo=Math.max(currentFrame,this.start), hi=Math.min(currentFrame+128,this.start+this.frames);
    if(hi>lo) {
      const samples=new Float32Array(hi-lo);
      if(input) samples.set(input.subarray(lo-currentFrame,hi-currentFrame));
      this.port.postMessage({offset:lo-this.start,samples},[samples.buffer]);
    }
    return currentFrame+128<this.start+this.frames;
  }
});"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        content_type = 'text/javascript; charset=utf-8'
        if path == '/probe.js':
            body = PROBE.read_bytes()
        elif path in WORKERS:
            body = ("importScripts('/probe.js');" + WORKERS[path]).encode()
        elif path == '/audio-worklet.js':
            body = WORKLET.encode()
        elif path == '/author.ttf':
            body, content_type = AUTHOR_FONT.read_bytes(), 'font/ttf'
        elif path in ('/', '/child', '/next'):
            body = b'<!doctype html><meta charset="utf-8"><title>Backend audit</title><script src="/probe.js"></script><body>Owned backend fixture</body>'
            content_type = 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'max-age=60')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextmanager
def server():
    instance = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{instance.server_port}'
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def case_args(name, family, native_capabilities):
    if name == 'native':
        return [*BASE_ARGS, '--uxr-audio-render=native', '--uxr-timer-resolution=0']
    mixed = name in ('isolated', 'restart')
    uvpaa = native_capabilities.get('uvpaa', {}).get('value', False)
    return [*BASE_ARGS,
        '--uxr-audio-render=isolated', '--uxr-audio-seed=' + ('12345' if mixed else '67890'),
        '--uxr-timer-resolution=' + ('7' if mixed else '11'),
        '--uxr-font-policy=restricted', '--uxr-font-whitelist=' + family,
        '--uxr-color-scheme=' + ('dark' if mixed else 'light'),
        '--uxr-preferred-contrast=' + ('more' if mixed else 'no-preference'),
        '--uxr-forced-colors=' + ('active' if mixed else 'none'),
        '--uxr-reduced-motion=' + str(mixed).lower(),
        '--uxr-reduced-transparency=' + str(mixed).lower(),
        '--uxr-inverted-colors=' + str(mixed).lower(),
        '--uxr-pointer=' + ('fine' if mixed else 'none'),
        '--uxr-hover=' + ('hover' if mixed else 'none'),
        '--uxr-max-touch-points=' + ('5' if mixed else '0'),
        '--uxr-keyboard-layout=native', '--uxr-plugins=chrome', '--uxr-voices=windows',
        '--uxr-webauthn-uvpaa=' + str(not uvpaa).lower()]


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def on_grid(value, quantum):
    return finite(value) and (not quantum or abs(value - round(value / quantum) * quantum) < 0.001)


def clock_errors(value, quantum, window=False):
    errors = []
    samples = value['samples']
    if len(samples) != 6:
        errors.append('clock sample count mismatch')
    for sample in samples:
        if any(not on_grid(sample[k], quantum) or sample[k] < 0 for k in ('wall', 'now', 'origin', 'event')):
            errors.append('clock is nonfinite, negative or outside the launch grid')
        if abs(sample['wall'] - sample['origin'] - sample['now']) > 1000 + 2 * quantum:
            errors.append('wall and monotonic clock origins disagree')
        if sample['temporal'] is not None:
            text = sample['temporal']
            if not isinstance(text, str) or not text.isascii() or not text.isdecimal():
                errors.append('invalid Temporal timestamp')
            elif quantum and int(text) % (quantum * 1000000):
                errors.append('Temporal is outside the launch grid')
    if (not samples or len({s['origin'] for s in samples}) != 1 or
            any(b['now'] < a['now'] for a, b in zip(samples, samples[1:])) or
            samples[-1]['now'] <= samples[0]['now']):
        errors.append('monotonic clock failed to advance with a stable origin')
    if (value['timezone'] != 'America/New_York' or value['dst'] != ['01:59', '03:00', '01:59', '01:00'] or
            value['offsets'] != [300, 240, 240, 300]):
        errors.append('timezone / spring or autumn DST mismatch')
    if value['locale'].get('hourCycle') != 'h23' or value['locale'].get('numberingSystem') != 'latn':
        errors.append('Intl Unicode locale extensions were lost')
    if window:
        raf, idle = value['callbacks']['raf'], value['callbacks']['idle']
        if (any(not on_grid(raf[k], quantum) for k in ('timestamp', 'now')) or
                raf['timestamp'] > raf['now'] + max(1, quantum)):
            errors.append('animation frame clock mismatch')
        if (not on_grid(idle['remaining'], quantum) or not 0 <= idle['remaining'] <= 50 or
                not on_grid(idle['now'], quantum) or type(idle['timedOut']) is not bool):
            errors.append('idle callback clock mismatch')
    return errors


def preferences_errors(value, name):
    errors = []
    queries, styles = value['queries'], value['styles']
    if set(queries) != set(QUERY_KEYS) or any(type(v) is not bool for v in queries.values()) or queries != styles:
        errors.append('media queries and computed CSS disagree')
    if name != 'native':
        mixed = name in ('isolated', 'restart')
        expected = {key: mixed for key in QUERY_KEYS}
        expected.update(coarse=False, none=not mixed)
        if queries != expected or value['maxTouchPoints'] != (5 if mixed else 0) or value['touchEvent'] is not mixed:
            errors.append('launch preferences / mixed input were not applied')
        original = {'background': 'rgb(3, 7, 11)', 'color': 'rgb(241, 239, 233)'}
        unchanged = all(value['author'][key] == color for key, color in original.items())
        if unchanged is mixed:
            errors.append('forced colors did not affect actual author styles')
    return errors


def audio_errors(value):
    errors = []
    samples = value['samples']
    if (len(samples) != 4096 or any(not finite(v) or abs(v) > 1 for v in samples) or
            (value['sampleRate'], value['channels'], value['frames']) != (44100, 1, 4096)):
        return ['missing bounded mono audio samples']
    digest = hashlib.sha256(struct.pack('<4096f', *samples)).hexdigest()
    if any(value[key] != digest for key in ('hash', 'copyHash', 'workletHash')):
        errors.append('audio buffer, copy and worklet hashes disagree with samples')
    if value['mutable'] is not True or value['analyserMatches'] is not True:
        errors.append('mutable buffer or analyser contract failed')
    if not 0.2 < math.sqrt(sum(v * v for v in samples) / len(samples)) < 0.5:
        errors.append('audio waveform is silent or outside fixture bounds')
    playback = value['playback']
    if (playback['inputHash'] != digest or playback['sameSamples'] is not True or
            playback['state'] != 'running' or playback['frames'] != 4096 or playback['sampleRate'] != 44100):
        errors.append('realtime graph did not receive the rendered samples')
    if any(v is not None and (not finite(v) or v < 0) for v in (playback['baseLatency'], playback['outputLatency'])):
        errors.append('invalid native audio latency')
    return errors


def ascii_lower(text):
    return text.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))


def capability_errors(value):
    errors = []
    for feature in ('uvpaa', 'keyboard'):
        result = value[feature]
        if result['status'] == 'unavailable':
            if not isinstance(result.get('error'), str) or not result['error']:
                errors.append(feature + ': unavailable capability lacks an API error')
        elif result['status'] != 'observed':
            errors.append(feature + ': unknown capability observation status')
        elif feature == 'uvpaa':
            if type(result.get('value')) is not bool:
                errors.append('UVPAA result is not a boolean')
        else:
            entries = result['entries']
            if (not isinstance(entries, list) or any(not isinstance(entry, list) or len(entry) != 2 or
                    any(not isinstance(part, str) for part in entry) for entry in entries) or
                    len({entry[0] for entry in entries}) != len(entries)):
                errors.append('invalid native keyboard layout inventory')
    plugins = value['plugins']
    if not isinstance(plugins, list) or any(not isinstance(plugin, dict) or
            not all(isinstance(plugin.get(key), str) for key in ('name', 'filename')) or
            not isinstance(plugin.get('types'), list) or
            any(not isinstance(mime, str) for mime in plugin['types']) for plugin in plugins):
        errors.append('invalid native plugin inventory')
    return errors


def assess(report):
    try:
        return _assess(report)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError, struct.error) as error:
        primary = report.get('errors', []) if isinstance(report, dict) else []
        return [*primary, 'malformed backend evidence: ' + str(error)], []


def _assess(report):
    errors, gaps = list(report.get('errors', [])), []
    if (type(report.get('schema_version')) is not int or report.get('schema_version') != 1 or
            report.get('probe_sha256') != launch.pool.file_hash(PROBE) or
            report.get('font_asset_sha256') != launch.pool.file_hash(AUTHOR_FONT)):
        errors.append('backend probe or font fixture identity mismatch')
    runs = report['runs']
    if [r['name'] for r in runs] != list(CASES):
        return [*errors, 'backend launch/restart matrix did not complete'], gaps
    baseline = runs[0]
    family = report['restricted_family']
    if not isinstance(family, str) or not family or ',' in family or not any(
            f['familyName'] == family for s in baseline['fonts'] for f in s['platformFonts']):
        errors.append('restricted family was not observed in the native inventory')
    for run in runs:
        name = run['name']
        prefix = lambda rows: errors.extend(name + ': ' + row for row in rows)
        if run['launch_args'] != case_args(name, family, baseline['capabilities']):
            prefix(['unexpected launch arguments'])
        quantum = 0 if name == 'native' else 11 if name == 'other_seed' else 7
        prefix(preferences_errors(run['preferences'], name))
        prefix(preferences_errors(run['preferences_reloaded'], name))
        expected_scopes = ('window', 'after_freeze', 'history', 'reloaded', 'iframe', 'oopif', 'dedicated', 'shared', 'service')
        if set(run['clocks']) != set(expected_scopes):
            prefix(['clock context matrix did not complete'])
        for scope, value in run['clocks'].items():
            prefix([scope + ': ' + e for e in clock_errors(value, quantum, scope not in ('dedicated', 'shared', 'service'))])
            if all(s['temporal'] is None for s in value['samples']):
                gaps.append(name + '.' + scope + '.Temporal')
        before, after = run['clocks']['window']['samples'], run['clocks']['after_freeze']['samples']
        if after[0]['origin'] != before[0]['origin'] or after[0]['now'] < before[-1]['now']:
            prefix(['clock regressed across freeze/resume'])
        lifecycle = [e for e in run['lifecycle_events'] if e['name'] in ('freeze', 'resume')]
        if ([e['name'] for e in lifecycle] != ['freeze', 'resume'] or
                any(e['trusted'] is not True or not on_grid(e['timestamp'], quantum) or
                    not on_grid(e['now'], quantum) for e in lifecycle)):
            prefix(['actual freeze/resume events were not observed on the clock grid'])
        elif not before[-1]['now'] <= lifecycle[0]['now'] <= lifecycle[1]['now'] <= after[0]['now']:
            prefix(['freeze/resume event clocks disagree with surrounding samples'])
        if run['oopif_observed'] is not True:
            prefix(['cross-site frame process boundary was not observed'])
        restored = any(e['name'] == 'pageshow' and e['persisted'] is True for e in run['history_events'])
        if not restored:
            gaps.append(name + '.bfcache')
        else:
            history = run['clocks']['history']['samples'][0]
            if history['origin'] != before[0]['origin'] or history['now'] < after[-1]['now']:
                prefix(['BFCache restore reset or regressed the document clock'])
        for event in run['input_events']:
            if event['trusted'] is not True or not on_grid(event['timestamp'], quantum):
                prefix(['input event was untrusted or outside the clock grid'])
        if name in ('isolated', 'restart') and not any(e['name'] == 'pointerdown' and e['pointerType'] == 'touch' for e in run['input_events']):
            prefix(['mixed input touch dispatch was not observed'])
        if not any(e['name'] == 'keydown' and e['key'] == 'a' for e in run['input_events']):
            prefix(['keyboard input dispatch was not observed'])
        prefix(audio_errors(run['audio']))
        if name != 'native':
            samples = run['audio']['samples']
            if (any(v * 2**20 != round(v * 2**20) for v in samples) or
                    max(abs(a - b) for a, b in zip(samples, baseline['audio']['samples'])) > 2**-20):
                prefix(['isolated audio exceeded the bounded sample grid'])
        if run['audio']['sourceHash'] != baseline['audio']['hash']:
            prefix(['audio source differs from the native control'])
        prefix(font_sample_errors(run['fonts']))
        for sample in run['fonts']:
            fonts = sample['platformFonts']
            if sample['text'] == 'Aa09' and not any(f['glyphCount'] > 0 for f in fonts):
                prefix(['installed font pool failed to render Latin text'])
            if name != 'native' and any(ascii_lower(f['familyName']) != ascii_lower(family)
                                        for f in fonts if f['glyphCount'] > 0):
                prefix(['resolved font escaped the restricted pool'])
        author = run['author_font']
        if author['status'] != 'loaded' or not finite(author['width']) or author['width'] <= 0 or not any(
                f['isCustomFont'] is True and f['glyphCount'] > 0 for f in author['platformFonts']):
            prefix(['downloaded author font stopped rendering'])
        local = run['local_fonts']
        if local['status'] == 'unavailable':
            gaps.append(name + '.local_fonts')
        elif (local['status'] != 'observed' or not isinstance(local['families'], list) or
                any(not isinstance(f, str) or not f for f in local['families'])):
            prefix(['invalid local font inventory'])
        elif name != 'native' and any(ascii_lower(f) != ascii_lower(family) for f in local['families']):
            prefix(['local font enumeration escaped the restricted pool'])
        caps = run['capabilities']
        prefix(capability_errors(caps))
        if any(caps[key] != baseline['capabilities'][key] for key in ('uvpaa', 'keyboard', 'pdf', 'plugins')):
            prefix(['public capability hints changed native capability inventory'])
        speech, native_speech = caps['speech'], baseline['capabilities']['speech']
        if not isinstance(speech, list) or any(not isinstance(v, dict) or
                not all(isinstance(v.get(key), str) for key in ('name', 'lang')) or
                not all(type(v.get(key)) is bool for key in ('local', 'default')) for v in speech):
            prefix(['invalid native speech inventory'])
        elif not speech or not native_speech:
            # An empty early inventory cannot prove that asynchronous platform
            # voice discovery completed, or that no synthesis backend exists.
            gaps.append(name + '.speech_inventory')
        elif speech != native_speech:
            prefix(['public voice hints changed the observed native inventory'])
        if type(caps['pdf']) is not bool or caps['pdf'] != any('application/pdf' in p['types'] for p in caps['plugins']):
            prefix(['PDF viewer and plugin inventory disagree'])
        for feature in ('uvpaa', 'keyboard'):
            if caps[feature]['status'] == 'unavailable':
                gaps.append(name + '.' + feature)
        if caps['keyboard']['status'] == 'observed' and not caps['keyboard']['entries']:
            gaps.append(name + '.keyboard_inventory')
    hashes = [r['audio']['hash'] for r in runs]
    if hashes[1] != hashes[2] or hashes[1] in (hashes[0], hashes[3]):
        errors.append('audio seed isolation / same-seed restart contract failed')
    return sorted(set(errors)), sorted(set(gaps))


class BackendDiagnostics:
    """Sidecar evidence only; never contributes observations to the acceptance matrix."""
    def __init__(self, path):
        self.path = path
        self.cases = []
        self.current = None
        self.errors = []

    def record(self, event, **details):
        row = {'at': datetime.now(timezone.utc).isoformat(),
               'case': self.current['name'] if self.current else None, 'event': event, **details}
        try:
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row, ensure_ascii=True) + '\n')
        except OSError as error:
            message = str(error)
            if message not in self.errors:
                self.errors.append(message)

    def start(self, name, args):
        self.current = {'name': name, 'launch_args': args, 'status': 'launching'}
        self.cases.append(self.current)
        self.record('launch', args=args)

    def failure(self, error):
        if self.current is None or 'failure' in self.current:
            return
        failure = {'error': type(error).__name__ + ': ' + str(error),
                   'last_command': self.current.get('last_command')}
        self.current.update(status='failed', failure=failure)
        # Snapshot before fixture disposal and browser.close() remove descendants.
        try:
            import psutil
            processes = []
            for process in [psutil.Process(), *psutil.Process().children(recursive=True)]:
                try:
                    processes.append(process.as_dict(attrs=['pid', 'ppid', 'name', 'status',
                        'cmdline', 'create_time', 'cpu_times', 'memory_info']))
                except psutil.Error as process_error:
                    processes.append({'pid': process.pid, 'error': str(process_error)})
            failure['processes'] = processes
        except Exception as diagnostic_error:
            failure['process_error'] = str(diagnostic_error)
        self.record('failure', **failure)


class FixtureProtocolError(RuntimeError):
    def __init__(self, method, error):
        super().__init__(method + ': ' + json.dumps(error))
        self.code = error.get('code')
        self.message = error.get('message')


class FixtureSession:
    """CDP target session in a context Playwright does not own or emulate."""
    def __init__(self, root, target, diagnostics=None):
        self.diagnostics = diagnostics
        self.target = target
        self.phase = 'initialization'
        self.root = root
        self.responses = {}
        self.sequence = 0
        self.session_id = root.send('Target.attachToTarget', {'targetId': target, 'flatten': False})['sessionId']
        self.listener = self._received
        root.on('Target.receivedMessageFromTarget', self.listener)

    def _received(self, event):
        if event['sessionId'] == self.session_id:
            message = json.loads(event['message'])
            if 'id' in message:
                self.responses[message['id']] = message
            elif self.diagnostics and message.get('method') in (
                    'Runtime.exceptionThrown', 'Runtime.executionContextCreated',
                    'Runtime.executionContextDestroyed', 'Runtime.executionContextsCleared',
                    'Inspector.targetCrashed', 'Page.frameNavigated', 'Page.frameStoppedLoading'):
                self.diagnostics.record('target_event', target=self.target,
                    session_id=self.session_id, message=message)

    def send(self, method, params=None):
        self.sequence += 1
        sequence = self.sequence
        command = {'target': self.target, 'session_id': self.session_id,
                   'sequence': sequence, 'phase': self.phase, 'method': method, 'params': params or {},
                   'dispatcher_round_trips': 0}
        if self.diagnostics:
            self.diagnostics.current['last_command'] = command
            self.diagnostics.record('command', **command)
        try:
            self.root.send('Target.sendMessageToTarget', {'sessionId': self.session_id,
                'message': json.dumps({'id': sequence, 'method': method, 'params': params or {}})})
            deadline = time.monotonic() + 30
            while sequence not in self.responses:
                if time.monotonic() >= deadline:
                    raise TimeoutError('CDP fixture timed out: ' + method)
                # A protocol round trip pumps Playwright's synchronous event dispatcher.
                self.root.send('Browser.getVersion')
                command['dispatcher_round_trips'] += 1
                time.sleep(0.01)
            response = self.responses.pop(sequence)
            if 'error' in response:
                raise FixtureProtocolError(method, response['error'])
            if self.diagnostics:
                self.diagnostics.record('command_completed', target=self.target,
                    session_id=self.session_id, sequence=sequence)
            return response.get('result', {})
        except Exception as error:
            if self.diagnostics:
                self.diagnostics.record('command_failed', **command, error=str(error))
                if isinstance(error, TimeoutError):
                    self.diagnostics.failure(error)
            raise

    def evaluate(self, expression, argument=None):
        if argument is not None:
            expression = '(' + expression + ')(' + json.dumps(argument) + ')'
        elif expression.lstrip().startswith(('()', 'async ()')):
            expression = '(' + expression + ')()'
        result = self.send('Runtime.evaluate', {'expression': expression,
            'awaitPromise': True, 'returnByValue': True, 'userGesture': True})
        if 'exceptionDetails' in result:
            raise RuntimeError('fixture JavaScript: ' + json.dumps(result['exceptionDetails']))
        return result['result'].get('value')

    def wait_for(self, expression):
        deadline = time.monotonic() + 30
        while True:
            try:
                if self.evaluate(expression):
                    return
            except FixtureProtocolError as error:
                # Navigation may replace the default execution context mid-poll.
                if error.code != -32000 or error.message not in (
                        'Execution context was destroyed.',
                        'Cannot find context with specified id'):
                    raise
            if time.monotonic() >= deadline:
                raise TimeoutError('fixture condition: ' + expression)
            time.sleep(0.01)

    def ready(self, url):
        self.wait_for('location.href === ' + json.dumps(url) +
                      ' && document.readyState === "complete" && !!globalThis.chromixBackendProbe')

    def goto(self, url):
        self.phase = 'navigation'
        response = self.send('Page.navigate', {'url': url})
        if response.get('errorText'):
            raise RuntimeError('fixture navigation: ' + response['errorText'])
        self.ready(url)

    def freeze_resume(self):
        self.phase = 'freeze_resume'
        self.send('Emulation.setFocusEmulationEnabled', {'enabled': False})
        self.send('Page.setWebLifecycleState', {'state': 'frozen'})
        failed = False
        try:
            time.sleep(0.08)
        except BaseException:
            failed = True
            raise
        finally:
            try:
                self.send('Page.setWebLifecycleState', {'state': 'active'})
            except Exception:
                if not failed:
                    raise
        # CDP active unfreezes without undoing WasHidden; RAF needs visibility.
        self.send('Emulation.setFocusEmulationEnabled', {'enabled': True})
        self.wait_for('document.visibilityState === "visible"')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.detach()
        except Exception:
            if exc_type is None:
                raise

    def detach(self):
        try:
            self.root.remove_listener('Target.receivedMessageFromTarget', self.listener)
        finally:
            self.root.send('Target.detachFromTarget', {'sessionId': self.session_id})


@contextmanager
def clean_fixture(instance, diagnostics=None):
    root = instance.new_browser_cdp_session()
    context_id = None
    page = None
    failed = False
    try:
        context_id = root.send('Target.createBrowserContext', {'disposeOnDetach': True})['browserContextId']
        target = root.send('Target.createTarget', {'url': 'about:blank', 'browserContextId': context_id})['targetId']
        page = FixtureSession(root, target, diagnostics)
        page.send('Page.enable')
        page.send('Runtime.enable')
        # No Playwright page session has installed media, touch or focus overrides.
        page.send('Emulation.setFocusEmulationEnabled', {'enabled': False})
        yield page, root, context_id
    except BaseException as error:
        failed = True
        if diagnostics:
            diagnostics.failure(error)
        raise
    finally:
        cleanup_errors = []
        cleanup = [page.detach] if page is not None else []
        if context_id is not None:
            cleanup.append(lambda: root.send('Target.disposeBrowserContext', {'browserContextId': context_id}))
        for close in [*cleanup, root.detach]:
            try:
                close()
            except Exception as error:
                cleanup_errors.append(error)
        if cleanup_errors and not failed:
            raise cleanup_errors[0]


def evaluate(page, method, phase=None):
    page.phase = phase or method
    return page.evaluate('async method => await chromixBackendProbe[method]()', method)


def collect(page, root, context_id, origin, name):
    session = page
    out = {'name': name, 'clocks': {}}
    out['preferences'] = evaluate(page, 'preferences')
    page.evaluate("document.querySelector('#backend-input').focus()")
    session.send('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'a', 'code': 'KeyA',
                                          'text': 'a', 'windowsVirtualKeyCode': 65})
    session.send('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': 'a', 'code': 'KeyA',
                                          'windowsVirtualKeyCode': 65})
    if name in ('isolated', 'restart'):
        session.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': 30, 'y': 30}]})
        session.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
    out['input_events'] = page.evaluate('backendInputEvents')
    out['clocks']['window'] = evaluate(page, 'clocks', 'clocks.window')
    session.freeze_resume()
    out['clocks']['after_freeze'] = evaluate(page, 'clocks', 'clocks.after_freeze')
    out['lifecycle_events'] = page.evaluate('chromixBackendProbe.lifecycle')
    history = session.send('Page.getNavigationHistory')
    entry = history['entries'][history['currentIndex']]['id']
    page.goto(origin + '/next')
    session.send('Page.navigateToHistoryEntry', {'entryId': entry})
    page.ready(origin + '/')
    out['clocks']['history'] = evaluate(page, 'clocks', 'clocks.history')
    out['history_events'] = page.evaluate('chromixBackendProbe.lifecycle')
    page.evaluate('globalThis.backendReloadMarker = true')
    session.send('Page.reload')
    page.wait_for('!globalThis.backendReloadMarker && document.readyState === "complete"'
                  ' && !!globalThis.chromixBackendProbe')
    out['clocks']['reloaded'] = evaluate(page, 'clocks', 'clocks.reloaded')
    out['preferences_reloaded'] = evaluate(page, 'preferences')
    page.evaluate("() => {const f=document.createElement('iframe'); f.src='/child'; document.body.append(f);}")
    page.wait_for("!!document.querySelector('iframe')?.contentWindow.chromixBackendProbe")
    page.phase = 'clocks.iframe'
    out['clocks']['iframe'] = page.evaluate("document.querySelector('iframe').contentWindow.chromixBackendProbe.clocks()")
    page.evaluate("document.querySelector('iframe').remove()")
    cross_site = origin.replace('127.0.0.1', 'localhost') + '/child'
    page.evaluate("url => {const f=document.createElement('iframe'); f.src=url; document.body.append(f);}", cross_site)
    deadline = time.monotonic() + 30
    while True:
        targets = root.send('Target.getTargets')['targetInfos']
        target = next((t for t in targets if t['type'] == 'iframe' and t['url'] == cross_site
                       and t.get('browserContextId') == context_id), None)
        if target:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError('cross-site frame process boundary was not observed')
        time.sleep(0.01)
    with FixtureSession(root, target['targetId'], page.diagnostics) as frame:
        frame.ready(cross_site)
        out['clocks']['oopif'] = evaluate(frame, 'clocks', 'clocks.oopif')
        out['oopif_observed'] = True
    page.evaluate("document.querySelector('iframe').remove()")
    out['clocks'].update(evaluate(page, 'workers'))
    out['audio'] = evaluate(page, 'audio')
    out['capabilities'] = evaluate(page, 'capabilities')
    session.send('DOM.enable')
    session.send('CSS.enable')
    page.phase = 'fonts'
    out['fonts'] = page.evaluate(FONT_NODES)
    document = session.send('DOM.getDocument')['root']['nodeId']
    nodes = session.send('DOM.querySelectorAll', {'nodeId': document, 'selector': '#p0-fonts > span'})['nodeIds']
    if len(nodes) != len(out['fonts']):
        raise ValueError('font source node count differs')
    for sample, node in zip(out['fonts'], nodes):
        sample['platformFonts'] = session.send('CSS.getPlatformFontsForNode', {'nodeId': node})['fonts']
    page.evaluate("document.querySelector('#p0-fonts').remove()")
    root.send('Browser.grantPermissions', {'permissions': ['localFonts'], 'origin': origin,
                                          'browserContextId': context_id})
    out['local_fonts'] = evaluate(page, 'localFonts')
    out['author_font'] = evaluate(page, 'authorFont')
    document = session.send('DOM.getDocument')['root']['nodeId']
    node = session.send('DOM.querySelector', {'nodeId': document, 'selector': '#backend-author-font'})['nodeId']
    out['author_font']['platformFonts'] = session.send('CSS.getPlatformFontsForNode', {'nodeId': node})['fonts']
    return out


def run(browser, headed=False, diagnostics=None):
    report = {'schema_version': 1, 'collected_at': datetime.now(timezone.utc).isoformat(),
        'browser_sha256': launch.pool.file_hash(browser), 'probe_sha256': launch.pool.file_hash(PROBE),
        'font_asset_sha256': launch.pool.file_hash(AUTHOR_FONT), 'runs': [], 'restricted_family': '', 'errors': [],
        'qualification': {'scope': 'launch/process policy; independent browser restarts',
            'physical_input': 'not_attested; CDP event dispatch', 'physical_audio': 'not_attested; muted graph tap',
            'font_file_equivalence': 'not_attested', 'cross_platform_rendering': 'not_attested'}}
    try:
        from playwright.sync_api import sync_playwright
        with server() as origin, sync_playwright() as pw:
            capabilities = {}
            for name in CASES:
                args = case_args(name, report['restricted_family'], capabilities)
                if diagnostics:
                    diagnostics.start(name, args)
                instance = pw.chromium.launch(executable_path=str(browser.resolve()), headless=not headed,
                    chromium_sandbox=True, args=args, ignore_default_args=IGNORED_DEFAULT_ARGS)
                try:
                    if report.setdefault('browser_version', instance.version) != instance.version:
                        raise ValueError('browser version changed across restarts')
                    with clean_fixture(instance, diagnostics) as (page, root, context_id):
                        page.goto(origin + '/')
                        observation = collect(page, root, context_id, origin, name)
                        observation['launch_args'] = args
                        report['runs'].append(observation)
                        if diagnostics:
                            diagnostics.current['status'] = 'collected'
                            diagnostics.record('collected')
                        if name == 'native':
                            capabilities = observation['capabilities']
                            sample = next(s for s in observation['fonts'] if s['family'] == 'sans-serif' and s['text'] == 'Aa09')
                            report['restricted_family'] = next(f['familyName'] for f in sample['platformFonts']
                                if f['glyphCount'] > 0 and f['familyName'] and ',' not in f['familyName'])
                except Exception as error:
                    if diagnostics:
                        diagnostics.failure(error)
                    raise
                finally:
                    if diagnostics:
                        diagnostics.record('browser_close_started')
                    try:
                        instance.close()
                        if diagnostics:
                            diagnostics.record('browser_close_completed')
                    except Exception as error:
                        report['errors'].append('browser close: ' + str(error))
    except Exception as error:
        if diagnostics:
            diagnostics.failure(error)
        report['errors'].append(type(error).__name__ + ': ' + str(error))
    if launch.pool.file_hash(browser) != report['browser_sha256']:
        report['errors'].append('browser executable changed')
    if diagnostics:
        report['diagnostics'] = {'trace': diagnostics.path.name, 'cases': diagnostics.cases,
                                 'errors': diagnostics.errors}
    report['errors'], report['gaps'] = assess(report)
    report['status'] = 'failed' if report['errors'] else 'incomplete' if report['gaps'] else 'passed'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--headed', action='store_true')
    args = parser.parse_args(argv)
    if not args.browser.is_file() or args.output.exists():
        parser.error('use an existing executable and a new report path')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    trace = args.output.with_suffix('.cdp.jsonl')
    if trace.exists():
        parser.error('use a new CDP trace path')
    # Playwright forwards browser stderr, launch PID and exit status to the suite log.
    debug = os.environ.get('DEBUG')
    os.environ['DEBUG'] = ','.join(filter(None, (debug, 'pw:browser')))
    try:
        report = run(args.browser, args.headed, BackendDiagnostics(trace))
    finally:
        if debug is not None:
            os.environ['DEBUG'] = debug
        else:
            os.environ.pop('DEBUG', None)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=True)
    print(json.dumps({'status': report['status'], 'errors': report['errors'], 'gaps': report['gaps']}))
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
