#!/usr/bin/env python3
"""Native display, lifecycle and explicit virtual-media backend integration audit."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk/python'))
from chromix import _device_launch as launch

PROBE = Path(__file__).with_name('fingerprint_runtime_probe.js')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
            self.send_error(403)
            return
        content = b'<!doctype html><meta charset="utf-8"><title>Runtime audit</title><body>owned loopback fixture</body>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'max-age=60')
        self.send_header('Permissions-Policy', 'camera=(), microphone=()' if self.path == '/denied'
                         else 'camera=(self), microphone=(self), window-management=(self)')
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass


@contextmanager
def server():
    instance = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=instance.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{instance.server_port}'
    finally:
        instance.shutdown(); instance.server_close(); worker.join(timeout=5)


def _assess(report):
    errors = list(report.get('errors', []))
    observations = report.get('observations', {})
    for name in ('display_initial', 'display_resized', 'display_scaled', 'screens',
                 'display_oopif', 'timing', 'lifecycle', 'audio', 'media_denied', 'media_capture', 'media_restart'):
        if not isinstance(observations.get(name), dict):
            errors.append('missing required observation: ' + name)
    for name, size in (('display_initial', (800, 600)), ('display_resized', (1024, 768)),
                       ('display_scaled', (1024, 768))):
        value = observations.get(name, {})
        window = value.get('window', {})
        if (window.get('innerWidth'), window.get('innerHeight')) != size:
            errors.append(name + ': viewport resize not applied')
        if value.get('screen', {}).get('width') != 1920 or value.get('screen', {}).get('height') != 1080:
            errors.append(name + ': emulated screen changed with viewport')
        if value.get('dpr') != 1.25:
            errors.append(name + ': DPR mismatch')
        if value.get('css') != dict.fromkeys(('width', 'deviceWidth', 'deviceHeight', 'resolution'), True):
            errors.append(name + ': CSS/JS mismatch')
        layout, viewport = value.get('layout', {}), value.get('viewport') or {}
        if any(not close(layout.get(k), v, 1) for k, v in zip(('width', 'height'), size)):
            errors.append(name + ': actual layout mismatch')
        scale = 1.5 if name == 'display_scaled' else 1
        if not close(viewport.get('scale'), scale, 0.001) or any(
                not close(viewport.get(k), v / scale, 2) for k, v in zip(('width', 'height'), size)):
            errors.append(name + ': visual viewport mismatch')
    screens = observations.get('screens', {})
    if screens.get('status') != 'observed' or len(screens.get('screens', [])) != 1:
        errors.append('explicit single-screen emulation did not produce one observable screen')
    elif any(s.get('width') != 1920 or s.get('height') != 1080 or s.get('dpr') != 1.25 or
             s.get('primary') is not True for s in screens['screens']):
        errors.append('ScreenDetails does not match the effective screen/DPR')
    child = observations.get('display_oopif', {})
    if (child.get('is_oopif') is not True or child.get('geometry', {}).get('screen') !=
            observations.get('display_resized', {}).get('screen') or child.get('geometry', {}).get('dpr') != 1.25):
        errors.append('OOPIF screen/DPR propagation mismatch or unobserved process boundary')
    geometry = child.get('geometry', {})
    if ((geometry.get('window', {}).get('innerWidth'), geometry.get('window', {}).get('innerHeight')) != (300, 150) or
            any(not close(geometry.get('layout', {}).get(k), v, 1) for k, v in (('width', 300), ('height', 150))) or
            geometry.get('css') != dict.fromkeys(('width', 'deviceWidth', 'deviceHeight', 'resolution'), True)):
        errors.append('OOPIF layout/CSS scaling mismatch')
    if report.get('configuration_mode') == 'launch-backend' and any(
            observations.get(name, {}).get('screen', {}).get('availHeight') != 1040
            for name in ('display_initial', 'display_resized', 'display_scaled')):
        errors.append('launch backend work-area bounds were not applied')
    restart = observations.get('media_restart', {})
    errors.extend(media_identity_errors(restart))
    media = observations.get('media_capture', {})
    settings, frame = media.get('settings', {}), media.get('frame', {})
    visible, decoded, live = frame.get('visible', {}), media.get('decoded', {}), media.get('live', {})
    if not media.get('mime') or not finite(media.get('bytes'), positive=True) or not media.get('playable'):
        errors.append('missing usable media recording/decoder evidence')
    for dim in ('width', 'height'):
        display_key, coded_key = 'display' + dim.title(), 'coded' + dim.title()
        cap = media.get('capabilities', {}).get(dim, {})
        if (not finite(settings.get(dim), positive=True) or not finite(cap.get('min')) or
                not finite(cap.get('max')) or not cap['min'] <= settings[dim] <= cap['max'] or
                settings[dim] != frame.get(display_key) or settings[dim] != live.get(dim)):
            errors.append('media track/frame/capability mismatch: ' + dim)
        if (not finite(visible.get(dim), positive=True) or not finite(frame.get(coded_key), positive=True) or
                visible[dim] > frame[coded_key] or decoded.get(dim) != visible[dim]):
            errors.append('media encoded/decoded pixel mismatch: ' + dim)
    denied = observations.get('media_denied', {})
    if denied.get('error') not in ('NotAllowedError', 'SecurityError'):
        errors.append('media permission denial not enforced')
    if any(d.get('deviceId') or d.get('groupId') or d.get('label') for d in denied.get('devices', [])):
        errors.append('denied document exposed media identity')
    audio = observations.get('audio', {})
    if (audio.get('sampleRate') != 44100 or audio.get('frames') != 4096 or audio.get('channels') != 1
            or audio.get('mutable') is not True or not finite(audio.get('peak'), positive=True)):
        errors.append('missing audio graph/mutable-buffer evidence')
    timing = observations.get('timing', {})
    if timing.get('dst') != ['01:59', '03:00', '01:59', '01:00']:
        errors.append('DST boundary mismatch')
    samples = timing.get('samples', [])
    if len(samples) != 6 or any(not all(finite(s.get(k)) for k in ('raf', 'now', 'wall', 'origin')) for s in samples):
        errors.append('missing timing samples')
    elif (any(b['raf'] < a['raf'] or b['now'] < a['now'] for a, b in zip(samples, samples[1:])) or
          any(s['now'] + 1 < s['raf'] for s in samples) or len({s['origin'] for s in samples}) != 1):
        errors.append('inconsistent monotonic clock samples')
    lifecycle = observations.get('lifecycle', {})
    if (not finite(lifecycle.get('after')) or not finite(lifecycle.get('before')) or
            lifecycle['after'] < lifecycle['before']):
        errors.append('monotonic clock regressed across freeze/resume')
    return sorted(set(errors))


def finite(value, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (not positive or value > 0)


def close(value, expected, tolerance):
    return finite(value) and abs(value - expected) <= tolerance


def media_identity_errors(restart):
    records = [restart.get(k) for k in ('original', 'same_document', 'reloaded', 'other_origin')]
    if any(not isinstance(rows, list) or not rows or any(
            not isinstance(row, list) or len(row) != 3 or not all(isinstance(v, str) for v in row)
            for row in rows) for rows in records):
        return ['missing media identity observations']
    original, repeated, reloaded, other = records
    stable = lambda rows: sorted((r[0], r[1]) for r in rows)
    ids = lambda rows: {r[1] for r in rows if r[1] and r[1] not in ('default', 'communications')}
    groups = lambda rows: {r[2] for r in rows if r[2]}
    errors = []
    # media_devices_util.cc deliberately derives group_id_salt per document;
    # deviceId is the origin/profile identity, groupId is not a restart identity.
    if sorted(original) != sorted(repeated) or stable(original) != stable(reloaded):
        errors.append('media same-document/reload identity contract failed')
    if not ids(original) or not ids(other) or ids(original) & ids(other):
        errors.append('media device IDs are not origin-isolated')
    if not groups(original) or not groups(reloaded) or groups(original) & groups(reloaded):
        errors.append('media group IDs did not rotate with the document')
    if groups(original) & groups(other):
        errors.append('media group IDs are not origin-isolated')
    return errors


def assess(report):
    try:
        return _assess(report)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        return ['malformed runtime evidence: ' + str(error)]


def run(browser, headed=False, persona_backend=False):
    report = {'schema_version': 1, 'collected_at': datetime.now(timezone.utc).isoformat(),
              'browser_sha256': launch.pool.file_hash(browser), 'probe_sha256': launch.pool.file_hash(PROBE),
              'observations': {}, 'errors': [], 'media_backend': 'explicit Chromium fake-device backend',
              'qualification': {'physical_media': 'not_tested', 'wire': 'not_collected',
                                'bfcache': 'not_observed', 'native_build': 'requires external build provenance'}}
    report['configuration_mode'] = 'launch-backend' if persona_backend else 'native-cdp'
    args = [*launch.NATIVE_ARGS, '--use-fake-device-for-media-stream', '--site-per-process']
    if persona_backend:
        args.remove('--fingerprint=off')
        args.extend(['--uxr-screen-width=1920', '--uxr-screen-height=1080',
            '--uxr-viewport-width=800', '--uxr-viewport-height=600',
            '--uxr-device-pixel-ratio=1.25', '--uxr-screen-avail-height=1040',
            '--uxr-screen-orientation=landscape-primary'])
    report['launch_args'] = args
    try:
        from playwright.sync_api import sync_playwright
        with server() as origin, server() as other, sync_playwright() as pw:
            instance = pw.chromium.launch(executable_path=str(browser.resolve()), headless=not headed,
                chromium_sandbox=True, args=args, ignore_default_args=['--disable-back-forward-cache'])
            try:
                report['browser_version'] = instance.version
                context = instance.new_context(**({'no_viewport': True} if persona_backend else {
                    'viewport': {'width': 800, 'height': 600},
                    'screen': {'width': 1920, 'height': 1080}, 'device_scale_factor': 1.25}))
                context.add_init_script(path=str(PROBE))
                page = context.new_page(); page.goto(origin)
                out = report['observations']
                session = context.new_cdp_session(page)
                def collect(name, method):
                    try:
                        out[name] = page.evaluate('async name => await chromixRuntimeProbe[name]()', method)
                    except Exception as error:
                        report['errors'].append(name + ': ' + str(error))
                collect('display_initial', 'geometry')
                # page.set_viewport_size intentionally resets the emulated
                # screen in Playwright. Exercise a fixed screen with one native
                # atomic metrics update instead of blaming that reset on Blink.
                session.send('Emulation.setDeviceMetricsOverride', {'width': 1024, 'height': 768,
                    'deviceScaleFactor': 1.25, 'mobile': False, 'screenWidth': 1920, 'screenHeight': 1080})
                collect('display_resized', 'geometry')
                cross_site = other.replace('127.0.0.1', 'localhost') + '/frame'
                with page.expect_event('frameattached') as attached:
                    page.evaluate('''url => {const f=document.createElement('iframe'); f.src=url;
                      f.style.cssText='border:0;width:300px;height:150px'; document.body.append(f);}''', cross_site)
                frame = attached.value
                frame.wait_for_url(cross_site)
                # A committed OOPIF can still have its pre-layout 0x0 widget.
                # Wait for the first real visual-properties delivery, not a
                # fixed sleep or a hand-written screen getter override.
                frame.wait_for_function('innerWidth > 0 && innerHeight > 0')
                targets = session.send('Target.getTargets')['targetInfos']
                out['display_oopif'] = {'geometry': frame.evaluate('() => chromixRuntimeProbe.geometry()'),
                    'is_oopif': any(t['type'] == 'iframe' and t['url'] == cross_site for t in targets)}
                page.evaluate('document.querySelector("iframe").remove()')
                target = session.send('Target.getTargetInfo')['targetInfo']
                session.send('Browser.grantPermissions', {'permissions': ['windowManagement'],
                    'origin': origin, 'browserContextId': target['browserContextId']})
                collect('screens', 'screens')
                session.send('Emulation.setPageScaleFactor', {'pageScaleFactor': 1.5})
                collect('display_scaled', 'geometry')
                session.send('Emulation.setPageScaleFactor', {'pageScaleFactor': 1})
                collect('timing', 'timing'); collect('audio', 'audio')
                before = page.evaluate('performance.now()')
                session.send('Page.setWebLifecycleState', {'state': 'frozen'})
                try:
                    time.sleep(0.15)
                finally:
                    session.send('Page.setWebLifecycleState', {'state': 'active'})
                out['lifecycle'] = {'before': before, 'after': page.evaluate('performance.now()'),
                                    'events': page.evaluate('chromixRuntimeProbe.lifecycle')}
                page.goto(origin + '/next'); page.go_back(wait_until='commit')
                events = page.evaluate('chromixRuntimeProbe.lifecycle')
                if any(e.get('name') == 'pageshow' and e.get('persisted') is True for e in events):
                    report['qualification']['bfcache'] = 'observed'
                out['lifecycle']['history_events'] = events
                page.goto(origin + '/denied'); collect('media_denied', 'deniedMedia')
                context.grant_permissions(['camera', 'microphone'], origin=origin)
                page.goto(origin); collect('media_capture', 'media')
                original = page.evaluate('async () => (await navigator.mediaDevices.enumerateDevices()).map(d=>[d.kind,d.deviceId,d.groupId])')
                same_document = page.evaluate('async () => (await navigator.mediaDevices.enumerateDevices()).map(d=>[d.kind,d.deviceId,d.groupId])')
                page.reload()
                repeated = page.evaluate('async () => (await navigator.mediaDevices.enumerateDevices()).map(d=>[d.kind,d.deviceId,d.groupId])')
                context.grant_permissions(['camera', 'microphone'], origin=other)
                page.goto(other)
                separated = page.evaluate('async () => (await navigator.mediaDevices.enumerateDevices()).map(d=>[d.kind,d.deviceId,d.groupId])')
                out['media_restart'] = {'original': original, 'same_document': same_document,
                    'reloaded': repeated, 'other_origin': separated,
                    'lifetime': 'deviceId: origin/profile; groupId: document (not restart-stable)'}
                context.clear_permissions(); context.close()
            finally:
                instance.close()
    except Exception as error:
        report['errors'].append(type(error).__name__ + ': ' + str(error))
    if launch.pool.file_hash(browser) != report['browser_sha256']:
        report['errors'].append('browser executable changed')
    report['errors'] = assess(report)
    report['status'] = 'failed' if report['errors'] else 'passed'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--persona-backend', action='store_true',
                        help='require native launch-time display flags; a stock browser is expected to fail')
    args = parser.parse_args(argv)
    if args.output.exists() or not args.browser.is_file():
        parser.error('use an existing executable and a new report path')
    report = run(args.browser, args.headed, args.persona_backend)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=True)
    print(json.dumps({'status': report['status'], 'errors': report['errors'], 'output': str(args.output)}))
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
