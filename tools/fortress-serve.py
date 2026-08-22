#!/usr/bin/env python3
"""
fortress-serve — multi-seed CDP front-end for Fortress (cloakserve-style).

One listening port, many isolated browser instances. Each connection picks a
persona by seed::

    # browser A (persona seed 1001)            # browser B (seed 1002)
    connect_over_cdp("http://127.0.0.1:9333/?seed=1001")
    connect_over_cdp("http://127.0.0.1:9333/?seed=1002")

Each seed gets its OWN browser process: its own --fingerprint seed, canvas /
audio noise, profile directory, and debug port. This server then proxies the
CDP HTTP discovery endpoints (/json*) and relays WebSocket frames byte-for-
byte, rewriting webSocketDebuggerUrl to point back through itself with the
seed attached. Client libraries (Playwright / Puppeteer / browser-use) work
unchanged against the single endpoint.

    python tools/fortress-serve.py --bundle ./tilion-fortress [--port 9333]
                                   [--max-pool 8] [--idle-timeout 900]
                                   [--proxy http://user:pass@host:port]

Stdlib only: HTTP via ThreadingHTTPServer, WebSocket via socket relay.
"""
from __future__ import annotations
import argparse
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

DEFAULT_MAX_POOL = 8
DEFAULT_IDLE_TIMEOUT = 900  # seconds; 0 disables reaping


# ---------------------------------------------------------------------------
# URL rewriting (pure, unit-testable)
# ---------------------------------------------------------------------------

def rewrite_ws_urls(payload: str, seed: str, listen_host: str, listen_port: int) -> str:
    """Point webSocketDebuggerUrl entries at this server, tagged with the seed.

    Rewrites only inside JSON string values, so ids/paths stay intact.
    """
    upstream_re = re.compile(r"(ws://)[^/]+(/devtools/[^\"\\]+)")
    def sub(m: re.Match) -> str:
        path = m.group(2)
        sep = "&" if "?" in path else "?"
        return f"{m.group(1)}{listen_host}:{listen_port}{path}{sep}seed={seed}"
    return upstream_re.sub(sub, payload)


def seed_for(parsed_query: dict) -> str:
    vals = parsed_query.get("seed") or parsed_query.get("fingerprint")
    if vals and vals[0].strip():
        return vals[0].strip()
    return str(int.from_bytes(os.urandom(4), "big"))


# ---------------------------------------------------------------------------
# Browser pool
# ---------------------------------------------------------------------------

class BrowserInstance:
    def __init__(self, seed: str, port: int, proc: subprocess.Popen, profile: str):
        self.seed, self.port, self.proc, self.profile = seed, port, proc, profile
        self.last_used = time.monotonic()
        self.target_cache: dict[str, int] = {}  # ws path -> upstream port

    def http(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=body)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def stop(self):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.proc.terminate()
                self.proc.wait(timeout=10)
        except Exception:
            pass
        shutil.rmtree(self.profile, ignore_errors=True)


class BrowserPool:
    """seed -> BrowserInstance, bounded by max_pool with idle reaping."""

    def __init__(self, launcher, max_pool: int = DEFAULT_MAX_POOL,
                 idle_timeout: float = DEFAULT_IDLE_TIMEOUT):
        self.launcher = launcher          # callable(seed) -> BrowserInstance
        self.max_pool = max_pool
        self.idle_timeout = idle_timeout
        self._lock = threading.Lock()
        self._browsers: dict[str, BrowserInstance] = {}
        self._ports = iter(range(9340, 9340 + 10_000))
        self._stop = threading.Event()
        if idle_timeout:
            threading.Thread(target=self._reaper, daemon=True).start()

    def get(self, seed: str) -> BrowserInstance:
        with self._lock:
            b = self._browsers.get(seed)
            if b and b.proc.poll() is None:
                b.last_used = time.monotonic()
                return b
            b = self.launcher(seed)
            self._browsers[seed] = b
            # Evict the least-recently-used idle instance beyond the cap.
            while len(self._browsers) > self.max_pool:
                oldest = min(self._browsers.values(), key=lambda x: x.last_used)
                self._browsers.pop(oldest.seed, None)
                threading.Thread(target=oldest.stop, daemon=True).start()
            return b

    def find_by_target(self, ws_path: str) -> BrowserInstance | None:
        """Locate the upstream that owns a /devtools/<type>/<id> path."""
        needle = ws_path.split("?", 1)[0]
        with self._lock:
            for port, b in [(b.port, b) for b in self._browsers.values()]:
                if needle in b.target_cache:
                    return b
        # Not cached: ask each live upstream for its target list.
        with self._lock:
            snapshot = list(self._browsers.values())
        for b in snapshot:
            try:
                _, body = b.http("GET", "/json/list")
                for t in json.loads(body):
                    url = (t.get("webSocketDebuggerUrl") or "")
                    path = url.split("127.0.0.1", 1)[-1]
                    path = path[path.find("/devtools"):] if "/devtools" in path else path
                    b.target_cache[path] = b.port
                if needle in b.target_cache:
                    b.last_used = time.monotonic()
                    return b
            except Exception:
                continue
        return None

    def next_port(self) -> int:
        return next(self._ports)

    def _reaper(self):
        while not self._stop.wait(30):
            now = time.monotonic()
            with self._lock:
                stale = [b for b in self._browsers.values()
                         if now - b.last_used > self.idle_timeout]
                for b in stale:
                    self._browsers.pop(b.seed, None)
            for b in stale:
                sys.stderr.write(f"[fortress-serve] reaping idle seed={b.seed}\n")
                b.stop()

    def shutdown(self):
        self._stop.set()
        with self._lock:
            browsers = list(self._browsers.values())
            self._browsers.clear()
        for b in browsers:
            b.stop()


# ---------------------------------------------------------------------------
# Request handler: /json* proxy + WebSocket relay
# ---------------------------------------------------------------------------

class ProxyHandler(BaseHTTPRequestHandler):
    pool: BrowserPool = None            # set by serve()
    listen_host: str = "127.0.0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # -- helpers -----------------------------------------------------------

    def _seed(self) -> str:
        return seed_for(parse_qs(urlparse(self.path).query))

    def _proxy_json(self, method: str):
        seed = self._seed()
        b = self.pool.get(seed)
        target = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            status, payload = b.http(method, target, body)
        except Exception as e:
            self._send(502, json.dumps({"error": str(e)}).encode(), "application/json")
            return
        ctype = "application/json"
        if isinstance(payload, bytes) and payload[:1] in (b"{", b"["):
            payload = rewrite_ws_urls(payload.decode("utf-8", "replace"),
                                      seed, self.listen_host,
                                      self.server.server_address[1]).encode()
        self._send(status, payload, ctype)

    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- HTTP methods -----------------------------------------------------

    def do_GET(self):
        if (self.headers.get("Upgrade", "").lower() == "websocket"
                and urlparse(self.path).path.startswith("/devtools/")):
            # Hijack the connection for the WebSocket relay (headers are
            # parsed by the time do_GET runs).
            self.close_connection = True
            try:
                self._relay_ws()
            except Exception as e:
                sys.stderr.write(f"[fortress-serve] ws relay error: {e}\n")
            return
        path = urlparse(self.path).path
        if path.startswith("/json"):
            self._proxy_json("GET")
        elif path in ("/", ""):
            self._send(200, json.dumps({
                "service": "fortress-serve",
                "usage": "http://<host>:<port>/?seed=<n>  (per-seed browser)",
                "seeds": sorted({b.seed for b in self.pool._browsers.values()}),
            }).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_PUT(self):
        if urlparse(self.path).path.startswith("/json"):
            self._proxy_json("PUT")
        else:
            self._send(404, b"not found", "text/plain")

    # -- WebSocket relay ----------------------------------------------------

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def _relay_ws(self):
        """Hijack the connection and byte-relay to the owning upstream WS."""
        path = urlparse(self.path).path
        b = self.pool.find_by_target(path)
        if b is None:
            self._send(404, b"no upstream owns this target", "text/plain")
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        upstream = socket.create_connection(("127.0.0.1", b.port), timeout=30)
        upstream.send((
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{b.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = upstream.recv(4096)
            if not chunk:
                self._send(502, b"upstream handshake failed", "text/plain")
                upstream.close()
                return
            buf += chunk
        self.connection.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " +
            buf.split(b"Sec-WebSocket-Accept: ", 1)[1].split(b"\r\n", 1)[0] +
            b"\r\n\r\n")
        b.last_used = time.monotonic()
        self._pipe(self.connection, upstream)

    @staticmethod
    def _pipe(a: socket.socket, bsock: socket.socket):
        def copier(src: socket.socket, dst: socket.socket):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
        threads = [threading.Thread(target=copier, args=(s, d), daemon=True)
                   for s, d in ((a, bsock), (bsock, a))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()



# ---------------------------------------------------------------------------
# Launcher + entry point
# ---------------------------------------------------------------------------

def make_launcher(args) -> "callable":
    launcher = os.path.join(args.bundle, "tilion.cmd" if os.name == "nt" else "tilion")
    if not os.path.exists(launcher):
        sys.exit(f"no launcher at {launcher}")

    def launch(seed: str) -> BrowserInstance:
        pool_ports = args._pool_ports
        port = next(pool_ports)
        profile = tempfile.mkdtemp(prefix=f"fortress-serve-{seed}-")
        cmd = [launcher, "--headless=new", "--no-sandbox", "--ignore-gpu-blocklist",
               f"--remote-debugging-port={port}",
               f"--user-data-dir={profile}",
               f"--fingerprint={seed}", "--fingerprint-platform=windows",
               "about:blank"]
        if args.proxy:
            cmd.append(f"--proxy-server={args.proxy}")
        if os.name == "nt":
            cmd = ["cmd", "/c", *cmd]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        inst = BrowserInstance(seed, port, proc, profile)
        # Wait for CDP readiness before handing the instance out.
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                http.client.HTTPConnection("127.0.0.1", port, timeout=2).request("GET", "/json/version")
                return inst
            except Exception:
                time.sleep(0.5)
        inst.stop()
        raise RuntimeError(f"seed {seed} browser failed to start on port {port}")
    return launch


def serve(args) -> ThreadingHTTPServer:
    args._pool_ports = iter(range(args.port + 1, args.port + 1 + 500))
    pool = BrowserPool(make_launcher(args), max_pool=args.max_pool,
                       idle_timeout=args.idle_timeout)
    handler = type("BoundHandler", (ProxyHandler,),
                   {"pool": pool, "listen_host": "127.0.0.1"})
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    httpd.daemon_threads = True
    httpd._fortress_pool = pool
    return httpd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", required=True, help="path to extracted tilion-fortress/")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--max-pool", type=int, default=DEFAULT_MAX_POOL)
    ap.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT)
    ap.add_argument("--proxy", help="upstream proxy for every pooled browser")
    args = ap.parse_args()

    httpd = serve(args)
    print(f"fortress-serve on http://{args.host}:{args.port}/  "
          f"(max-pool={args.max_pool}, idle-timeout={args.idle_timeout}s)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd._fortress_pool.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
