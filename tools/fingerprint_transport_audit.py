#!/usr/bin/env python3
"""Observe real loopback TLS/HTTP2 traffic from an explicit browser executable.

The owned TLS endpoint records ClientHello handshake bytes and decoded HTTP2
SETTINGS/headers. This is server-side wire evidence, not proxy/DNS/QUIC coverage.
"""
from __future__ import annotations
import argparse
import base64
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import socketserver
import ssl
import sys
import tempfile
import threading

from fingerprint_protocols import parse_client_hello, compare

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk/python'))
from chromix import _device_launch as launch
from chromix._device_headers import header_errors

HINTS = ('Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform-Version, Sec-CH-UA-Arch, '
         'Sec-CH-UA-Bitness, Sec-CH-UA-Model, Sec-CH-UA-WoW64')


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            with self.server.tls.wrap_socket(self.request, server_side=True) as connection:
                self.request = connection
                self.handle_http2()
        except (TimeoutError, ConnectionError, ssl.SSLError, OSError):
            # Aborted speculative connections have no request evidence. An
            # actual missing connection/request is rejected by assess().
            pass

    def handle_http2(self):
        from h2.config import H2Configuration
        from h2.connection import H2Connection
        from h2.events import RequestReceived, RemoteSettingsChanged, ConnectionTerminated
        self.request.settimeout(10)
        record = {'alpn': self.request.selected_alpn_protocol(), 'tls': self.request.version(),
                  'cipher': self.request.cipher(), 'settings': [], 'requests': []}
        with self.server.lock:
            self.server.connections.append(record)
        if record['alpn'] != 'h2':
            return
        connection = H2Connection(H2Configuration(client_side=False, header_encoding='utf-8'))
        connection.initiate_connection(); self.request.sendall(connection.data_to_send())
        digest = hashlib.sha256()
        try:
            while True:
                data = self.request.recv(65536)
                if not data:
                    break
                digest.update(data)
                for event in connection.receive_data(data):
                    if isinstance(event, RemoteSettingsChanged):
                        record['settings'].append([[int(k), v.new_value] for k, v in event.changed_settings.items()])
                    elif isinstance(event, RequestReceived):
                        # hpack.HeaderTuple has special construction/deepcopy
                        # semantics; store plain JSON pairs at the wire boundary.
                        headers = [[str(k), str(v)] for k, v in event.headers]
                        record['requests'].append({'stream': event.stream_id, 'headers': headers,
                            'pseudo_order': [k for k, _ in headers if k.startswith(':')]})
                        path = dict(headers).get(':path', '/')
                        if path == '/echo':
                            body = json.dumps({'headers': dict(headers)}).encode()
                            mime = 'application/json'
                        else:
                            body = b'<!doctype html><title>Owned TLS fixture</title>'
                            mime = 'text/html'
                        connection.send_headers(event.stream_id, [(':status', '200'), ('content-type', mime),
                            ('content-length', str(len(body))), ('accept-ch', HINTS), ('cache-control', 'no-store')])
                        connection.send_data(event.stream_id, body, end_stream=True)
                    elif isinstance(event, ConnectionTerminated):
                        return
                response = connection.data_to_send()
                if response:
                    self.request.sendall(response)
        except (TimeoutError, ConnectionError, ssl.SSLError, OSError) as error:
            record['close_reason'] = type(error).__name__
        finally:
            record['application_bytes_sha256'] = digest.hexdigest()


@contextmanager
def endpoint(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Chromix loopback audit')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    certificate, private = directory / 'cert.pem', directory / 'key.pem'
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    # This fixture compares full ClientHellos, not a full handshake with a
    # speculative ticket-based reconnect. Keep PSK extension 41 meaningful in
    # the comparator instead of silently removing it from observed profiles.
    tls.options |= ssl.OP_NO_TICKET
    tls.num_tickets = 0
    tls.set_alpn_protocols(['h2', 'http/1.1']); tls.load_cert_chain(certificate, private)
    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
    # Non-daemon handlers are joined by server_close before report serialization.
    server.daemon_threads = False
    server.connections, server.hellos, server.handshake_errors = [], [], []
    server.lock = threading.Lock()
    server.spki = base64.b64encode(hashlib.sha256(key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).digest()).decode('ascii')
    def message(_connection, direction, _version, content_type, message_type, data):
        if direction == 'read' and int(content_type) == 22 and int(message_type) == 1:
            try:
                hello = parse_client_hello(bytes(data))
                with server.lock:
                    server.hellos.append(hello)
            except ValueError as error:
                with server.lock:
                    server.handshake_errors.append(str(error))
    if not hasattr(tls, '_msg_callback'):
        server.server_close()
        raise RuntimeError('this Python/OpenSSL build cannot capture handshake messages')
    tls._msg_callback = message
    # Handshake only in bounded handler threads. A silent TCP client must not
    # block the accept loop (or shutdown) inside SSLSocket.accept().
    server.tls = tls
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield server, f'https://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


IDENTITY = """async () => {
  const n=navigator, names=['architecture','bitness','platformVersion','model','fullVersionList','wow64'];
  return {userAgent:n.userAgent, language:n.language, languages:Array.from(n.languages),
    userAgentData:n.userAgentData ? {brands:n.userAgentData.brands, mobile:n.userAgentData.mobile,
      platform:n.userAgentData.platform, ...(await n.userAgentData.getHighEntropyValues(names))} : null,
    wire:await (await fetch('/echo',{cache:'no-store'})).json()};
}"""


def _assess(report):
    errors = list(report.get('errors', []))
    hellos, connections = report.get('client_hellos', []), report.get('connections', [])
    if len(hellos) < 2:
        errors.append('fewer than two independently observed ClientHellos')
    if any(41 in hello.get('extensions', []) for hello in hellos):
        errors.append('full-handshake fixture unexpectedly received a PSK resumption offer')
    comparisons = []
    for hello in hellos[1:]:
        comparison = compare(hellos[0], hello); comparisons.append(comparison)
        if comparison['status'] != 'observed_match':
            errors.append('TLS profile changed across fresh browser contexts')
    active = [c for c in connections if c.get('requests')]
    if len(active) < 2 or any(c.get('alpn') != 'h2' or not c.get('settings') for c in active):
        errors.append('missing negotiated HTTP2 SETTINGS/request evidence')
    settings = [c['settings'][0] for c in active if c.get('settings')]
    if settings and any(s != settings[0] for s in settings[1:]):
        errors.append('HTTP2 initial SETTINGS changed across contexts')
    orders = []
    for connection in active:
        for request in connection['requests']:
            headers = request.get('headers', [])
            order = [k for k, _ in headers if k.startswith(':')]
            if (request.get('pseudo_order') != order or len(order) != 4 or
                    set(order) != {':method', ':authority', ':scheme', ':path'} or
                    [k for k, _ in headers[:4]] != order):
                errors.append('invalid HTTP2 pseudo-header order evidence')
            orders.append(order)
    if orders and any(o != orders[0] for o in orders[1:]):
        errors.append('HTTP2 pseudo-header order changed across contexts')
    for observation in report.get('observations', []):
        # Reuse the same JS/server coherence validator as the five-context probe.
        scope = {'identity': {'value': {'ua': observation.get('userAgent'),
                    'languages': observation.get('languages'), 'uaData': observation.get('userAgentData')}},
                 'http': {'status': 'observed', 'value': observation.get('wire', {})}}
        errors.extend(header_errors(scope, require_hints=True))
    if len(report.get('observations', [])) != 2:
        errors.append('missing JS/header observations')
    return errors, comparisons


def assess(report):
    try:
        return _assess(report)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        return ['malformed transport evidence: ' + str(error)], []


def run(browser, headed=False):
    report = {'schema_version': 1, 'browser_sha256': launch.pool.file_hash(browser),
              'collected_at': datetime.now(timezone.utc).isoformat(), 'observations': [], 'errors': [],
              'client_hellos': [], 'connections': [], 'qualification': {
                  'kind': 'owned TLS endpoint observations', 'proxy': 'not_tested', 'dns': 'not_tested',
                  'quic': 'not_tested', 'webrtc_routes': 'not_tested', 'physical_network': 'not_attested',
                  'session_resumption': 'not_tested; server tickets disabled for full-handshake comparison'}}
    try:
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory(prefix='chromix-tls-') as directory:
            with endpoint(Path(directory)) as (server, origin), sync_playwright() as pw:
                # CDP ignoreHTTPSErrors permits navigation but leaves certificate
                # errors that suppress high-entropy Accept-CH persistence. Trust
                # only this ephemeral endpoint key; do not disable validation globally.
                args = [*launch.NATIVE_ARGS, '--ignore-certificate-errors-spki-list=' + server.spki]
                report['launch_args'] = args
                instance = pw.chromium.launch(executable_path=str(browser.resolve()), headless=not headed,
                    chromium_sandbox=True, args=args)
                try:
                    report['browser_version'] = instance.version
                    for _ in range(2):
                        context = instance.new_context(no_viewport=True)
                        try:
                            page = context.new_page(); page.goto(origin, wait_until='load', timeout=30000)
                            observation = page.evaluate(launch.bounded(IDENTITY), None)
                            report['observations'].append(observation)
                        finally:
                            context.close()
                finally:
                    instance.close()
            # endpoint has joined its handlers: the deep copy is a frozen report,
            # not references still being mutated by a socket thread.
            report['client_hellos'] = copy.deepcopy(server.hellos)
            report['connections'] = copy.deepcopy(server.connections)
            report['errors'].extend(server.handshake_errors)
    except Exception as error:
        report['errors'].append(type(error).__name__ + ': ' + str(error))
    if launch.pool.file_hash(browser) != report['browser_sha256']:
        report['errors'].append('browser executable changed')
    report['errors'], report['comparisons'] = assess(report)
    report['status'] = 'failed' if report['errors'] else 'passed'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--headed', action='store_true')
    args = parser.parse_args(argv)
    if not args.browser.is_file() or args.output.exists():
        parser.error('use an existing executable and a new report path')
    report = run(args.browser, args.headed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=True)
    print(json.dumps({'status': report['status'], 'errors': report['errors'], 'output': str(args.output)}))
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
