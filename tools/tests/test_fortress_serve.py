"""fortress-serve (CDP multiplexer) tests against a fake upstream.

No browser needed: a stdlib fake CDP upstream serves /json/version,
/json/list and echoes WebSocket bytes; the tests drive the real proxy code.
"""
import base64
import http.client
import importlib.util
import json
import os
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

_spec = importlib.util.spec_from_file_location(
    "fortress_serve", Path(__file__).resolve().parents[1] / "fortress-serve.py")
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)


# ---------------------------------------------------------------------------
# Fake upstream: HTTP discovery + byte-echo WebSocket
# ---------------------------------------------------------------------------

class _FakeProc:
    pid = 4242
    def poll(self):
        return None
    def terminate(self):
        pass
    def wait(self, timeout=None):
        return 0


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.close_connection = True
            # The relay forwards this header verbatim; the test client does
            # not verify the RFC 6455 accept value.
            self.request.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: c2VjcmV0\r\n\r\n")
            # Echo raw bytes back until the client closes.
            try:
                while True:
                    data = self.request.recv(65536)
                    if not data:
                        break
                    self.request.sendall(data)
            except Exception:
                pass
            return
        if path == "/json/version":
            self._json({"Browser": "fake/1.0", "webSocketDebuggerUrl":
                        f"ws://127.0.0.1:{self.server.server_address[1]}/devtools/browser/fake-uuid"})
        elif path == "/json/list":
            self._json([{"id": "PAGE1", "type": "page",
                         "webSocketDebuggerUrl":
                         f"ws://127.0.0.1:{self.server.server_address[1]}/devtools/page/PAGE1"}])
        else:
            self._json({"error": "not found"}, status=404)

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_fake_upstream():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class _PoolFixture:
    def __init__(self):
        self.upstream = _start_fake_upstream()
        port = self.upstream.server_address[1]
        # A launcher that returns instances pointing at the fake upstream.
        def launch(seed):
            return fs.BrowserInstance(seed, port, _FakeProc(), profile="")
        self.pool = fs.BrowserPool(launch, max_pool=4, idle_timeout=0)
        handler = type("Bound", (fs.ProxyHandler,),
                       {"pool": self.pool, "listen_host": "127.0.0.1"})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server.server_address[1]


_fixture = None


def fixture():
    global _fixture
    if _fixture is None:
        _fixture = _PoolFixture()
    return _fixture


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------

def test_rewrite_ws_urls_tags_seed():
    payload = json.dumps({
        "webSocketDebuggerUrl": "ws://127.0.0.1:9411/devtools/page/ABC",
        "id": "ABC"})
    out = fs.rewrite_ws_urls(payload, "777", "127.0.0.1", 9333)
    url = json.loads(out)["webSocketDebuggerUrl"]
    assert url == "ws://127.0.0.1:9333/devtools/page/ABC?seed=777", url


def test_rewrite_preserves_existing_query():
    payload = '{"w":"ws://127.0.0.1:1/devtools/browser/U?a=1"}'
    out = fs.rewrite_ws_urls(payload, "42", "127.0.0.1", 9500)
    assert json.loads(out)["w"] == "ws://127.0.0.1:9500/devtools/browser/U?a=1&seed=42"


def test_seed_for():
    q = parse_qs("seed=1001")
    assert fs.seed_for(q) == "1001"
    q = parse_qs("fingerprint=abc")
    assert fs.seed_for(q) == "abc"
    s = fs.seed_for(parse_qs(""))
    assert s.isdigit() and int(s) > 0  # random fallback


# ---------------------------------------------------------------------------
# End-to-end proxy tests (fake upstream, real proxy code)
# ---------------------------------------------------------------------------

def _http_get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    r = conn.getresponse()
    return r.status, r.read()


def test_json_version_proxied_and_rewritten():
    f = fixture()
    status, body = _http_get(f.port, "/json/version?seed=31337")
    assert status == 200
    data = json.loads(body)
    assert data["Browser"] == "fake/1.0"
    assert f"ws://127.0.0.1:{f.port}/devtools/browser/fake-uuid?seed=31337" == \
        data["webSocketDebuggerUrl"]


def test_seed_reuses_same_instance():
    f = fixture()
    _http_get(f.port, "/json/version?seed=reuse-me")
    _http_get(f.port, "/json/version?seed=reuse-me")
    assert "reuse-me" in f.pool._browsers


def test_websocket_relay_roundtrip():
    f = fixture()
    # 1. discovery through the proxy
    status, body = _http_get(f.port, "/json/list?seed=ws1")
    assert status == 200
    ws_url = json.loads(body)[0]["webSocketDebuggerUrl"]
    assert ws_url.startswith(f"ws://127.0.0.1:{f.port}/devtools/page/PAGE1?seed=ws1")
    # 2. real WebSocket handshake through the relay
    s = socket.create_connection(("127.0.0.1", f.port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    s.send((f"GET /devtools/page/PAGE1?seed=ws1 HTTP/1.1\r\nHost: x\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    assert b"101" in buf.split(b"\r\n", 1)[0]
    # 3. masked frame in, same bytes echoed back through the relay
    msg = json.dumps({"id": 1, "method": "Runtime.evaluate"}).encode()
    mask = os.urandom(4)
    frame = bytearray([0x81, 0x80 | 126]) + struct.pack(">H", len(msg)) + mask
    frame += bytes(b ^ mask[i % 4] for i, b in enumerate(msg))
    s.send(bytes(frame))
    got = b""
    while len(got) < len(frame):
        chunk = s.recv(65536)
        if not chunk:
            break
        got += chunk
    assert got == bytes(frame), (got, bytes(frame))
    s.close()


def test_unknown_ws_target_404():
    f = fixture()
    s = socket.create_connection(("127.0.0.1", f.port), timeout=10)
    s.send((b"GET /devtools/page/NOPE?seed=x HTTP/1.1\r\nHost: x\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Key: k\r\nSec-WebSocket-Version: 13\r\n\r\n"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    assert b"404" in buf.split(b"\r\n", 1)[0]
