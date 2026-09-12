"""SOCKS GeoIP transport with a loopback protocol peer, no external network."""
import contextlib
import json
import socket
import threading
import time

import pytest

from chromix._network import geoip_http


DATA = {'status':'success', 'timezone':'Asia/Tokyo', 'countryCode':'JP', 'query':'203.0.113.9'}


def read_exact(sock, size):
    result = b''
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise EOFError
        result += part
    return result


def read_until(sock, marker, limit=8192):
    result = b''
    while not result.endswith(marker):
        result += read_exact(sock, 1)
        assert len(result) < limit
    return result


@contextlib.contextmanager
def peer(*, version=5, auth=False, failure=None, fragment=False, bound_type=1):
    server = socket.socket()
    server.bind(('127.0.0.1', 0)); server.listen(1); server.settimeout(3)
    observations, errors, clients = [], [], []

    def serve():
        try:
            client, _ = server.accept(); clients.append(client)
            with client:
                client.settimeout(2)
                def send(value):
                    for part in ([value[i:i+1] for i in range(len(value))] if fragment else [value]):
                        client.sendall(part)
                if failure == 'timeout':
                    time.sleep(0.3); return
                if version == 5:
                    assert read_exact(client, 3) == bytes((5, 1, 2 if auth else 0))
                    if failure == 'method': send(b'\5\xff'); return
                    send(bytes((5, 2 if auth else 0)))
                    if auth:
                        assert read_exact(client, 1) == b'\1'
                        user = read_exact(client, read_exact(client, 1)[0])
                        password = read_exact(client, read_exact(client, 1)[0])
                        observations.append(('auth', user, password))
                        send(b'\1\1' if failure == 'auth' else b'\1\0')
                        if failure == 'auth': return
                    head = read_exact(client, 4)
                    assert head[:3] == b'\5\1\0'
                    length = read_exact(client, 1)[0] if head[3] == 3 else (4 if head[3] == 1 else 16)
                    destination = read_exact(client, length); port = read_exact(client, 2)
                    observations.append(('destination', head[3], destination, int.from_bytes(port, 'big')))
                    if failure == 'refuse': send(b'\5\5\0\1'); return
                    if failure == 'truncated': send(b'\5\0'); return
                    if bound_type == 3: bound = b'\x05proxy'
                    else: bound = b'\0' * (4 if bound_type == 1 else 16)
                    send(bytes((5, 0, 0, bound_type)) + bound + b'\0\0')
                else:
                    head = read_exact(client, 8); assert head[:2] == b'\4\1'
                    user = read_until(client, b'\0')[:-1]
                    destination = read_until(client, b'\0')[:-1] if head[4:8] == b'\0\0\0\1' else head[4:8]
                    observations.append(('v4', destination, user))
                    send(b'\0\x5a\0\0\0\0\0\0')
                request = read_until(client, b'\r\n\r\n')
                observations.append(('http', request))
                body = json.dumps(DATA).encode()
                send(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
        except (EOFError, OSError) as exc:
            if not failure: errors.append(exc)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=serve, daemon=True); thread.start()
    try:
        yield server.getsockname()[1], observations
    finally:
        for client in clients:
            client.close()
        server.close(); thread.join(4)
        assert not thread.is_alive(), 'SOCKS peer did not finish'
        assert not errors, errors


@pytest.mark.parametrize('scheme', ['socks', 'socks5', 'socks5h'])
@pytest.mark.parametrize('auth', [False, True])
@pytest.mark.parametrize('bound_type', [1, 3, 4])
def test_socks5_route_and_authentication(scheme, auth, bound_type):
    with peer(auth=auth, fragment=True, bound_type=bound_type) as (port, seen):
        credentials = 'u%40:p%3A@' if auth else ''
        assert geoip_http(f'{scheme}://{credentials}127.0.0.1:{port}') == ('Asia/Tokyo', 'ja-JP', '203.0.113.9')
    assert ('destination', 3, b'ip-api.com', 80) in seen
    if auth: assert ('auth', b'u@', b'p:') in seen
    request = next(row[1] for row in seen if row[0] == 'http')
    assert request.startswith(b'GET /json/?fields=')
    assert b'Host: ip-api.com\r\n' in request
    assert b'Proxy-Authorization' not in request


@pytest.mark.parametrize('scheme', ['socks4', 'socks4a'])
def test_socks4_destination_encoding(scheme):
    with peer(version=4) as (port, seen):
        assert geoip_http(f'{scheme}://user@127.0.0.1:{port}', 'http://127.0.0.2/json')[-1] == '203.0.113.9'
    assert ('v4', b'127.0.0.2' if scheme == 'socks4a' else b'\x7f\0\0\2', b'user') in seen


@pytest.mark.parametrize('target, kind, address', [
    ('http://192.0.2.1/json', 1, b'\xc0\0\2\1'),
    ('http://[2001:db8::1]/json', 4, bytes.fromhex('20010db8000000000000000000000001')),
])
def test_socks5_ip_literal_types(target, kind, address):
    with peer() as (port, seen):
        assert geoip_http(f'socks5://127.0.0.1:{port}', target)[-1] == '203.0.113.9'
    assert ('destination', kind, address, 80) in seen


@pytest.mark.parametrize('failure', ['method', 'refuse', 'truncated', 'auth', 'timeout'])
def test_socks_failure_does_not_attempt_a_second_route(failure, monkeypatch):
    monkeypatch.setenv('CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS', '0.1')
    connections = []
    original = socket.create_connection
    def connect(address, *args, **kwargs):
        connections.append(address)
        assert address[0] == '127.0.0.1'
        return original(address, *args, **kwargs)
    monkeypatch.setattr(socket, 'create_connection', connect)
    started = time.monotonic()
    with peer(auth=failure == 'auth', failure=failure) as (port, _):
        with pytest.raises(ValueError, match='GeoIP'):
            geoip_http(f'socks5://{"u:p@" if failure == "auth" else ""}127.0.0.1:{port}')
    assert len(connections) == 1
    assert time.monotonic() - started < 1.5
