#!/usr/bin/env python3
"""Collect a local device evidence bundle using an explicitly supplied browser.

Runs two launches of one temporary profile and one independent profile. This is
an observation tool, not a backend emulator. No browser download or media grant.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlsplit
import uuid

import device_pool as pool
import fingerprint_smoke as smoke

ASSET = Path(__file__).resolve().parents[1] / 'sdk/python/chromix/device_probe.js'
WORKER = """
importScripts('/device-probe.js');
const send = port => chromixDeviceProbe().then(
  value => port.postMessage({value}), e => port.postMessage({error:String(e)}));
"""
SCRIPTS = {
    '/device-worker.js':WORKER + 'send(self);',
    '/device-shared.js':WORKER + 'onconnect = e => send(e.ports[0]);',
    '/device-service.js':WORKER + """
      addEventListener('install', e => e.waitUntil(self.skipWaiting()));
      addEventListener('activate', e => e.waitUntil(self.clients.claim()));
      addEventListener('message', e => e.waitUntil(send(e.ports[0])));
    """,
}
CONTEXT_PROBE = r"""async (kind) => {
  let port, worker, registration, timer, channel;
  try {
    if (kind === 'worker') {
      worker = new Worker('/device-worker.js'); port = worker;
    } else if (kind === 'shared_worker') {
      worker = new SharedWorker('/device-shared.js'); port = worker.port;
    } else {
      registration = await navigator.serviceWorker.register('/device-service.js');
      await navigator.serviceWorker.ready;
      const active = registration.active;
      if (!active) throw new Error('service worker did not activate');
      channel = new MessageChannel(); port = channel.port1;
      active.postMessage('probe', [channel.port2]);
    }
    return await new Promise((resolve, reject) => {
      timer = setTimeout(() => reject(new Error(kind + ' timeout')), 15000);
      port.onmessage = e => e.data.error ? reject(new Error(e.data.error)) : resolve(e.data.value);
      port.onmessageerror = () => reject(new Error(kind + ' message error'));
      if (worker) worker.onerror = e => reject(new Error(e.message || kind + ' script error'));
      port.start?.();
    });
  } finally {
    clearTimeout(timer);
    if (kind === 'worker') worker?.terminate(); else port?.close();
    channel?.port2.close();
    if (registration) await registration.unregister();
  }
}"""


class Handler(smoke.LocalHandler):
    def send_header(self, keyword, value):
        if keyword.lower() == 'content-security-policy':
            value = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'"
        super().send_header(keyword, value)

    def do_CONNECT(self):
        # Background browser connections are refused; a peer may close before
        # it reads the rejection. Do not let that obscure probe failures.
        try:
            super().do_CONNECT()
        except (ConnectionError, OSError):
            pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/headers':
            from chromix._device_launch import ProbeHandler
            return ProbeHandler.do_GET(self)
        if path not in SCRIPTS and path != '/device-probe.js':
            return super().do_GET()
        if (self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}'
                or not self.path.startswith('/') or self.path.startswith('//')):
            self.send_error(403)
            return
        with self.server.lock:
            self.server.requests.append({'path':self.path})
        body = (ASSET.read_text(encoding='utf-8') if path == '/device-probe.js' else SCRIPTS[path]).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/javascript; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def server_context():
    server = smoke.LocalServer()
    server.RequestHandlerClass = Handler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


from chromix._device_host import host_inventory


def collect_browser(args):
    binary = smoke.binary_identity(args.browser)
    result = {'binary':binary, 'observations':[], 'external_requests':[],
              'browser_versions':[], 'probe_sha256':pool.file_hash(ASSET),
              'profile_isolation':False, 'headless':not args.headed,
              'engine_provenance':'User supplied executable; hash is not authentication',
              'limitations':['No wire capture or physical backend equivalence proof',
                             'No media permission, capture or playback test',
                             'LocalStorage isolation only; no IndexedDB/Cache isolation test',
                             'fingerprint=off does not disable every historical Chromix patch']}
    with server_context() as server, tempfile.TemporaryDirectory(prefix='chromix-device-') as temp:
        with smoke.load_playwright()() as playwright:
            token = uuid.uuid4().hex
            storage = []
            for index, profile in enumerate(('profile-a', 'profile-a', 'profile-b')):
                switches = smoke.browser_args({'mode':'native'}, server.origin, False)
                result['launch_args'] = switches
                context = playwright.chromium.launch_persistent_context(
                    str(Path(temp) / profile), executable_path=binary['path'],
                    args=switches, headless=not args.headed, chromium_sandbox=True,
                    no_viewport=True, service_workers='allow', timeout=args.timeout_ms)
                try:
                    result['browser_versions'].append(context.browser.version)
                    context.set_default_timeout(args.timeout_ms)
                    def guard(route):
                        if smoke.allowed_url(route.request.url, server.origin):
                            route.continue_()
                        else:
                            result['external_requests'].append(route.request.url)
                            route.abort()
                    context.route('**/*', guard)
                    def block_socket(socket):
                        result['external_requests'].append(socket.url)
                        socket.close()
                    context.route_web_socket('**/*', block_socket)
                    context.on('request', lambda request: result['external_requests'].append(request.url)
                               if not smoke.allowed_url(request.url, server.origin) else None)
                    page = context.new_page()
                    page.goto(server.origin, wait_until='load')
                    storage.append(page.evaluate("localStorage.getItem('chromix-device-marker')"))
                    if index == 0:
                        page.evaluate("token => localStorage.setItem('chromix-device-marker', token)", token)
                    observation = {}
                    for scope, target in [('window', page), ('iframe', page.frame(url=server.origin + '/frame'))]:
                        if target is None:
                            raise ValueError('iframe did not load')
                        target.add_script_tag(url=server.origin + '/device-probe.js')
                        observation[scope] = smoke.evaluate(target, '() => chromixDeviceProbe()', None, args.timeout_ms)
                    for scope in pool.SCOPES[2:]:
                        observation[scope] = smoke.evaluate(page, CONTEXT_PROBE, scope, args.timeout_ms)
                    result['observations'].append(observation)
                finally:
                    context.close()
            result['profile_isolation'] = storage == [None, token, None]
    if pool.file_hash(binary['path']) != binary['sha256']:
        raise ValueError('browser executable changed during collection')
    if len(result['browser_versions']) != 3 or len(set(result['browser_versions'])) != 1:
        raise ValueError('browser version changed or was not observed in every launch')
    if pool.file_hash(ASSET) != result['probe_sha256']:
        raise ValueError('device probe changed during collection')
    return result


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new evidence directory; must not exist')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--timeout-ms', type=smoke.positive_int, default=30000)
    args = parser.parse_args(argv)
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        print(json.dumps({'status':'error', 'message':str(error)}))
        return 1
    try:
        host = host_inventory()
        write_json(args.output / 'host.json', host)
        browser = collect_browser(args)
        write_json(args.output / 'browser.json', browser)
        record = {'schema_version':1, 'kind':'measured', 'provenance':{
            'collector':'chromix-collect-device-v1',
            'collected_at':datetime.now(timezone.utc).isoformat(),
            'browser_sha256':browser['binary']['sha256'],
            'browser_version':browser['browser_versions'][0],
            'probe_sha256':browser['probe_sha256'],
        }, 'device':{'host':host, 'surfaces':pool.stable_observation(browser['observations'][0])},
            'evidence':{name:{'path':name + '.json', 'sha256':pool.file_hash(args.output / (name + '.json'))}
                        for name in ('host', 'browser')},
            'qualification':pool.QUALIFICATION}
        record['record_id'] = pool.digest(record)
        write_json(args.output / 'record.json', record)
        pool.validate_record(record, args.output)
        print(json.dumps({'status':'collected', 'record':str(args.output / 'record.json')}))
        return 0
    except Exception as error:
        failure = {'status':'failed', 'message':str(error)}
        write_json(args.output / 'failure.json', failure)
        print(json.dumps(failure))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
