#!/usr/bin/env python3
"""Smoke-test an existing Chromix executable against a private loopback HTTPServer.

No downloads, browser discovery, UA/locale emulation, or permission grants are
performed. Run this file with --help for the matrix and output options.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit


PLATFORMS = {
    "linux": {"platform": "Linux x86_64", "ch": "Linux", "ua": "Linux x86_64"},
    "windows": {"platform": "Win32", "ch": "Windows", "ua": "Windows NT 10.0"},
}
HIGH_HEADERS = {
    "platformVersion": "sec-ch-ua-platform-version",
    "architecture": "sec-ch-ua-arch",
    "bitness": "sec-ch-ua-bitness",
    "model": "sec-ch-ua-model",
    "uaFullVersion": "sec-ch-ua-full-version",
    "fullVersionList": "sec-ch-ua-full-version-list",
    "wow64": "sec-ch-ua-wow64",
}
ACCEPT_CH = ", ".join(["sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
                       *HIGH_HEADERS.values()])
SCOPES = ("window", "iframe", "worker")
LIMITATIONS = [
    "A smoke pass is behavioral evidence, not a signed attestation of a Chromix build.",
    "No real media permission is requested or granted; device counts are observations only.",
    "No GPU/physical audio device is required; unsupported optional probes are reported.",
    "Surface probes cover bounded Canvas/GPU/offline audio/codec paths, not all P0-P2 backlog items; optional skips are not coverage passes.",
    "Loopback fetch/ResourceTiming and context offline/online events are tested; network/storage dynamic values are not restart-stability fingerprints. Disk enforcement and storage-bucket isolation are not tested.",
    "No TLS/HTTP2/HTTP3, external service, WebRTC/STUN, or host-isolation proof is tested.",
    "Routing and browser network flags are defense in depth, not an OS network sandbox.",
    "Executable names, hashes and CDP identity cannot automatically authenticate Chromix provenance; verify the release independently.",
    "Each native/persona/seed family starts with a new temporary persistent profile; its restarts reuse that exact directory. Profiles are deleted after the run.",
    "The native control explicitly uses --fingerprint=off without persona overrides; it is not a separate stock Chromium binary.",
]

# The same probe runs in window, iframe and a dedicated worker, after Accept-CH.
NAV_PROBE = r"""async (request) => {
  const n = navigator;
  const u = n.userAgentData;
  let high = null, highError = null;
  if (u) {
    try {
      high = await u.getHighEntropyValues(['platformVersion', 'architecture',
        'bitness', 'model', 'uaFullVersion', 'fullVersionList', 'wow64']);
    } catch (e) { highError = {name:e.name, message:e.message}; }
  }
  const environment = {};
  const connection = n.connection;
  try {
    environment.network = connection ? {available:true, online:n.onLine,
      effectiveType:connection.effectiveType, rtt:connection.rtt,
      downlink:connection.downlink, saveData:connection.saveData} :
      {available:false, reason:'Network Information API unavailable'};
  } catch (e) { environment.network = {error:{name:e.name, message:e.message}}; }
  try {
    environment.storage = n.storage && typeof n.storage.estimate === 'function' ?
      {available:true, estimate:await n.storage.estimate()} :
      {available:false, reason:'StorageManager.estimate unavailable'};
  } catch (e) { environment.storage = {error:{name:e.name, message:e.message}}; }
  const response = await fetch('/echo?' + new URLSearchParams(request),
    {cache:'no-store', mode:'same-origin', redirect:'error'});
  if (!response.ok) throw new Error('header echo failed: ' + response.status);
  return {ua:n.userAgent, language:n.language, languages:Array.from(n.languages),
    platform:n.platform, uaData:u ? {low:u.toJSON(), high, highError} : null,
    intlLocale:Intl.DateTimeFormat().resolvedOptions().locale, environment,
    http:await response.json()};
}"""
WORKER_SCRIPT = ("const probe = " + NAV_PROBE + ";\n"
                 "const request = Object.fromEntries(new URLSearchParams(location.search));\n"
                 "probe({...request, scope:'worker'}).then(value => postMessage({value}), "
                 "e => postMessage({error:{name:e.name,message:e.message}}));\n")
WORKER_PROBE = r"""async (request) => {
  const worker = new Worker('/worker.js?' + new URLSearchParams(request));
  try {
    return await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('dedicated worker timeout')), 10000);
      worker.onmessage = e => {
        clearTimeout(timer);
        if (e.data.error) reject(new Error(JSON.stringify(e.data.error)));
        else resolve(e.data.value);
      };
      worker.onerror = e => { clearTimeout(timer); reject(new Error(e.message)); };
    });
  } finally { worker.terminate(); }
}"""
SIGNAL_PROBE = r"""async () => {
  const digest = async bytes => Array.from(new Uint8Array(
    await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
  const capture = async fn => {
    try { return await fn(); }
    catch (e) { return {error:{name:e.name, message:e.message}}; }
  };
  const canvasResult = await capture(async () => {
    const canvas = document.createElement('canvas');
    canvas.width = 32; canvas.height = 16;
    const ctx = canvas.getContext('2d');
    if (!ctx) return {available:false, reason:'2D context unavailable'};
    ctx.fillStyle = '#234567'; ctx.fillRect(0, 0, 32, 16);
    ctx.fillStyle = '#abc123'; ctx.fillRect(3, 2, 17, 9);
    const pixels = ctx.getImageData(0, 0, 32, 16).data;
    const pixelHash = await digest(pixels);
    const repeatHash = await digest(ctx.getImageData(0, 0, 32, 16).data);
    const url = canvas.toDataURL();
    const blob = await new Promise(resolve => canvas.toBlob(resolve));
    return {available:true, pixelHash, repeatHash,
      dataUrlHash:await digest(new TextEncoder().encode(url)),
      dataUrlRepeat:url === canvas.toDataURL(),
      blobHash:blob ? await digest(await blob.arrayBuffer()) : null};
  });
  const audio = await capture(async () => {
    if (typeof OfflineAudioContext === 'undefined')
      return {available:false, reason:'OfflineAudioContext unavailable'};
    const render = async index => {
      const context = new OfflineAudioContext(1, 128, 44100);
      const buffer = context.createBuffer(1, 128, 44100);
      const samples = Float32Array.from({length:128}, (_, i) => Math.sin(i / 9) * 0.25);
      buffer.copyToChannel(samples, 0);
      const destination = new Float32Array(16).fill(0.75);
      let exception = null;
      // Invalid copy must precede any getChannelData/farbling of the source.
      if (index !== null) {
        try { buffer.copyFromChannel(destination, index); }
        catch (e) { exception = {name:e.name, message:e.message}; }
      }
      const source = context.createBufferSource();
      source.buffer = buffer; source.connect(context.destination); source.start();
      const rendered = await context.startRendering();
      const values = new Float32Array(rendered.getChannelData(0));
      const hash = await digest(values.buffer);
      return {contextSampleRate:context.sampleRate, bufferSampleRate:buffer.sampleRate,
        renderedSampleRate:rendered.sampleRate, index, exception,
        destinationUnchanged:Array.from(destination).every(v => v === 0.75),
        hash, repeatHash:await digest(rendered.getChannelData(0).buffer),
        samples:Array.from(values.slice(0, 8))};
    };
    const control = await render(null);
    const invalid = [await render(1), await render(-1)];
    return {available:true, control, invalid};
  });
  return {canvas:canvasResult, audio};
}"""
MEDIA_PROBE = r"""async (denied) => {
  const permissions = {};
  for (const name of ['camera', 'microphone', 'notifications']) {
    if (!navigator.permissions || typeof navigator.permissions.query !== 'function') {
      permissions[name] = {available:false, reason:'Permissions.query unavailable'};
      continue;
    }
    try { permissions[name] = {available:true, state:(await navigator.permissions.query({name})).state}; }
    catch (e) { permissions[name] = {error:{name:e.name, message:e.message}}; }
  }
  const policy = document.permissionsPolicy || document.featurePolicy;
  const policyAllows = {};
  for (const name of ['camera', 'microphone'])
    policyAllows[name] = policy ? policy.allowsFeature(name) : null;
  let devices = null, devicesError = null;
  if (navigator.mediaDevices) {
    try {
      devices = Array.from(await navigator.mediaDevices.enumerateDevices(), d =>
        ({kind:d.kind, label:d.label, deviceId:d.deviceId, groupId:d.groupId}));
    } catch (e) { devicesError = {name:e.name, message:e.message}; }
  }
  const capture = {};
  if (denied && navigator.mediaDevices) {
    for (const [name, constraints] of [['camera', {video:true}], ['microphone', {audio:true}]]) {
      // Never call getUserMedia unless document policy explicitly forbids it.
      if (policyAllows[name] !== false) { capture[name] = {notRun:'policy denial unconfirmed'}; continue; }
      let timer;
      try {
        const request = navigator.mediaDevices.getUserMedia(constraints).then(stream => {
          stream.getTracks().forEach(track => track.stop());
          return {unexpectedSuccess:true};
        }, e => ({exception:{name:e.name, message:e.message}}));
        capture[name] = await Promise.race([request, new Promise(resolve => {
          timer = setTimeout(() => resolve({timeout:true}), 5000);
        })]);
      } finally { clearTimeout(timer); }
    }
  }
  return {permissions, policyAllows, devices, devicesError,
    deviceProbe:navigator.mediaDevices ? (devicesError ? {error:devicesError} : {available:true}) :
      {available:false, reason:'mediaDevices unavailable'}, capture,
    notificationPermission:typeof Notification === 'undefined' ? null : Notification.permission,
    permissionGrantsByRunner:0, deniedDocument:denied};
}"""


SURFACE_ASSET = Path(__file__).with_name("fingerprint_surface_probe.js")
SURFACE_WORKER_SCRIPT = r"""importScripts('/surface.js');
fingerprintSurfaceProbe('canvas').then(value => postMessage({value}),
  e => postMessage({error:{name:e.name,message:e.message}}));
"""
SURFACE_PROBE = r"""async mode => {
  if (mode === 'worker') {
    const worker = new Worker('/surface-worker.js');
    let timer;
    try {
      return await new Promise((resolve, reject) => {
        timer = setTimeout(() => reject(new Error('surface worker timeout')), 10000);
        worker.onmessage = e => e.data.error ? reject(new Error(JSON.stringify(e.data.error))) : resolve(e.data.value);
        worker.onerror = e => reject(new Error(e.message));
      });
    } finally { clearTimeout(timer); worker.terminate(); }
  }
  if (typeof globalThis.fingerprintSurfaceProbe !== 'function') {
    await new Promise((resolve, reject) => {
      const script = document.createElement('script'); script.src = '/surface.js';
      script.onload = resolve; script.onerror = () => reject(new Error('surface asset load failed'));
      document.head.appendChild(script);
    });
  }
  return await globalThis.fingerprintSurfaceProbe(mode);
}"""
NETWORK_EVENT_SETUP = r"""async () => {
  globalThis.smokeNetworkEvents = [];
  for (const type of ['offline', 'online'])
    addEventListener(type, () => smokeNetworkEvents.push({type, online:navigator.onLine}));
  return {online:navigator.onLine};
}"""
NETWORK_EVENT_READ = r"""async expected => {
  const deadline = performance.now() + 2000;
  while (!smokeNetworkEvents.some(e => e.type === expected) && performance.now() < deadline)
    await new Promise(resolve => setTimeout(resolve, 10));
  return {online:navigator.onLine, events:smokeNetworkEvents.slice()};
}"""


class SmokeError(ValueError):
    """The smoke runner cannot obtain the required evidence."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def binary_identity(path: str | Path) -> dict:
    resolved = Path(path).expanduser().resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or not info.st_size:
        raise SmokeError(f"browser must be a nonempty regular file: {resolved}")
    if not os.access(resolved, os.X_OK):
        raise SmokeError(f"browser is not executable: {resolved}")
    with resolved.open("rb") as stream:
        magic = stream.read(4)
    if magic not in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                     b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca") and magic[:2] != b"MZ":
        raise SmokeError(f"--browser must name the native binary, not a launcher script: {resolved}")
    return {"path": str(resolved), "sha256": sha256_file(resolved), "size": info.st_size}


def allowed_url(url: str, origin: str) -> bool:
    try:
        actual, expected = urlsplit(url), urlsplit(origin)
        return (actual.scheme == expected.scheme == "http"
                and actual.hostname == expected.hostname == "127.0.0.1"
                and actual.netloc == expected.netloc
                and actual.port == expected.port
                and actual.username is None and actual.password is None)
    except ValueError:
        return False


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), LocalHandler)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_port}"

    def snapshot(self):
        with self.lock:
            return list(self.requests)


class LocalHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def do_CONNECT(self):
        self.send_error(403, "external proxy connections forbidden")

    def do_GET(self):
        if (self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
                or not self.path.startswith("/") or self.path.startswith("//")):
            self.send_error(403, "only this loopback origin is served")
            return
        record = {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}
        with self.server.lock:
            self.server.requests.append(record)
        path = urlsplit(self.path).path
        if path == "/surface.js":
            content, mime = SURFACE_ASSET.read_text(encoding="utf-8"), "text/javascript"
        elif path == "/surface-worker.js":
            content, mime = SURFACE_WORKER_SCRIPT, "text/javascript"
        elif path == "/timing":
            content, mime = "chromix-loopback-timing", "text/plain"
        elif path == "/worker.js":
            content, mime = WORKER_SCRIPT, "text/javascript"
        elif path == "/echo":
            content, mime = json.dumps(record), "application/json"
        elif path == "/":
            content, mime = ('<!doctype html><meta charset="utf-8"><title>Fingerprint smoke</title>'
                             '<iframe id="probe" src="/frame"></iframe>', "text/html")
        elif path in ("/frame", "/denied"):
            content, mime = '<!doctype html><meta charset="utf-8"><title>Local probe</title>', "text/html"
        else:
            self.send_error(404)
            return
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-CH", ACCEPT_CH)
        self.send_header("Content-Security-Policy",
                         f"default-src {self.server.origin}; connect-src {self.server.origin}; "
                         f"worker-src {self.server.origin}; frame-src {self.server.origin}; "
                         "object-src 'none'; base-uri 'none'; form-action 'none'")
        if path == "/denied":
            self.send_header("Permissions-Policy", "camera=(), microphone=()")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@contextmanager
def local_server():
    server = LocalServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def scenario_matrix(args) -> list[dict]:
    result = [{"name": f"native-{restart}", "mode": "native", "restart": restart,
               "seed": None, "platform": None, "locale": None} for restart in (1, 2)]
    for platform in dict.fromkeys(args.platform):
        for locale in dict.fromkeys(args.locale):
            for suffix, mode, seed, restart in (
                    ("seed-a-1", "on", args.seed, 1), ("seed-a-2", "on", args.seed, 2),
                    ("seed-b", "on", args.other_seed, 1), ("off", "off", None, 1)):
                result.append({"name": f"{platform}-{locale}-{suffix}", "mode": mode,
                               "seed": seed, "platform": platform, "locale": locale,
                               "restart": restart})
    return result


def profile_group(spec: dict) -> str:
    return "native" if spec["mode"] == "native" else f"{spec['platform']}-{spec['locale']}-{spec['mode']}-{spec['seed']}"


def browser_args(scenario: dict, origin: str, no_sandbox: bool) -> list[str]:
    args = ["--no-first-run", "--no-default-browser-check", "--disable-background-networking",
            "--disable-component-update", "--disable-domain-reliability", "--disable-sync",
            "--disable-quic", "--disable-breakpad", "--no-pings", "--enable-automation",
            "--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication",
            # Rules are ordered: remove implicit loopback bypass first, then
            # restore only our endpoint. Reversing them proxies the fixture too.
            f"--proxy-server={origin}", f"--proxy-bypass-list=<-loopback>;127.0.0.1:{urlsplit(origin).port}",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            "--disable-extensions", "--disable-client-side-phishing-detection",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    if scenario["mode"] == "native":
        args.append("--fingerprint=off")
    elif scenario["mode"] == "off":
        # Off deliberately retains explicit locale/timezone overrides. This
        # cell compares with the native baseline, so it must not supply either.
        args += ["--fingerprint=off", f"--fingerprint-platform={PLATFORMS[scenario['platform']]['platform']}"]
    else:
        seed = scenario["seed"] if scenario["mode"] == "on" else "off"
        args += ["--uxr-synthetic-device-tests=true",
                 f"--fingerprint={seed}", f"--fingerprint-platform={PLATFORMS[scenario['platform']]['platform']}",
                 f"--fingerprint-locale={scenario['locale']}"]
    if no_sandbox:
        args.append("--no-sandbox")
    return args


def add_check(checks: list, name: str, passed: bool, expected=None, observed=None):
    checks.append({"name": name, "status": "passed" if passed else "failed",
                   "expected": expected, "observed": observed})


def skip(checks: list, name: str, reason: str):
    checks.append({"name": name, "status": "not_supported", "reason": reason})


CH_STRING = r'"(?:[\x20-\x21\x23-\x5b\x5d-\x7e]|\\["\\])*"'
CH_BRAND = rf'({CH_STRING})[ \t]*;[ \t]*v=({CH_STRING})'


def brand_pairs(value):
    if not isinstance(value, list) or not value:
        raise ValueError("nonempty brand list required")
    pairs = []
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"brand", "version"}
                or not isinstance(item["brand"], str) or not item["brand"].strip()
                or not isinstance(item["version"], str)
                or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", item["version"])):
            raise ValueError("brand and numeric version strings required")
        pairs.append((item["brand"], item["version"]))
    if len({brand for brand, _ in pairs}) != len(pairs):
        raise ValueError("duplicate Client Hints brand")
    return sorted(pairs)


def parse_ch(value: str, field: str):
    if not isinstance(value, str):
        raise ValueError("Client Hint must be a string")
    if field in ("brands", "fullVersionList"):
        if not re.fullmatch(rf'{CH_BRAND}(?:[ \t]*,[ \t]*{CH_BRAND})*', value):
            raise ValueError("malformed Client Hints brand list")
        return brand_pairs([{"brand": json.loads(m[1]), "version": json.loads(m[2])}
                            for m in re.finditer(CH_BRAND, value)])
    if field in ("mobile", "wow64"):
        if value not in ("?0", "?1"):
            raise ValueError("malformed Client Hints boolean")
        return value == "?1"
    if not re.fullmatch(CH_STRING, value):
        raise ValueError("Client Hint must be a structured quoted string")
    decoded = json.loads(value)
    if field == "uaFullVersion" and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", decoded):
        raise ValueError("numeric full version required")
    return decoded


def language_tags(value: str) -> list[str]:
    return [part.split(";", 1)[0].strip().lower() for part in value.split(",") if part.strip()]


def canonical_identity(scope: dict) -> dict:
    return {key: scope.get(key) for key in ("ua", "language", "languages", "platform", "uaData", "intlLocale")}


def evaluate_scope(scope: dict, spec: dict, name: str, phase: str = "initial") -> list:
    checks = []
    echo = scope.get("http", {})
    query = parse_qs(urlsplit(echo.get("path", "")).query)
    expected_query = {"scenario": [spec["name"]], "restart": [str(spec["restart"])],
                      "scope": [name], "phase": [phase]}
    add_check(checks, f"{name}.http_request_context", urlsplit(echo.get("path", "")).path == "/echo"
              and query == expected_query, expected_query, query)
    headers = echo.get("headers", {})
    add_check(checks, f"{name}.http_ua", bool(scope.get("ua")) and headers.get("user-agent") == scope.get("ua"),
              scope.get("ua"), headers.get("user-agent"))
    languages = [tag.lower() for tag in scope.get("languages", [])]
    tags = language_tags(headers.get("accept-language", ""))
    # Chromium may add bare-language fallbacks and reduce the transmitted list.
    permitted = set(languages) | {tag.split("-")[0] for tag in languages}
    coherent = bool(tags and languages) and tags[0] == languages[0] and set(tags) <= permitted
    add_check(checks, f"{name}.http_languages", coherent, languages, tags)
    add_check(checks, f"{name}.navigator_language", bool(languages) and scope.get("language", "").lower() == languages[0],
              languages[:1], scope.get("language"))
    data = scope.get("uaData") or {}
    low, high = data.get("low") or {}, data.get("high") or {}
    add_check(checks, f"{name}.ua_data_available", bool(low and high) and not data.get("highError"), True, data)
    mappings = {"brands": "sec-ch-ua", "mobile": "sec-ch-ua-mobile",
                "platform": "sec-ch-ua-platform", **HIGH_HEADERS}
    for field, header in mappings.items():
        js = low if field in ("brands", "mobile", "platform") else high
        expected = js.get(field)
        try:
            expected = brand_pairs(expected) if field in ("brands", "fullVersionList") else expected
            observed = parse_ch(headers[header], field)
            passed = field in js and type(expected) is type(observed) and expected == observed
        except (ValueError, KeyError, TypeError) as error:
            observed, passed = {"error": str(error), "header": headers.get(header)}, False
        add_check(checks, f"{name}.ch.{field}", passed, expected, observed)
    for field in ("brands", "mobile", "platform"):
        add_check(checks, f"{name}.entropy.{field}", field in high
                  and type(high.get(field)) is type(low.get(field)) and high.get(field) == low.get(field),
                  low.get(field), high.get(field))
    add_check(checks, f"{name}.intl_locale_available", isinstance(scope.get("intlLocale"), str)
              and bool(scope.get("intlLocale")), "resolved default Intl locale", scope.get("intlLocale"))
    checks.extend(evaluate_environment(scope.get("environment"), name))
    if spec["mode"] == "on":
        expected = PLATFORMS[spec["platform"]]
        add_check(checks, f"{name}.requested_intl_locale",
                  str(scope.get("intlLocale", "")).lower() == spec["locale"].lower(),
                  spec["locale"], scope.get("intlLocale"))
        for field, observed, wanted in (("platform", scope.get("platform"), expected["platform"]),
                                         ("ch_platform", low.get("platform"), expected["ch"]),
                                         ("locale", scope.get("language", "").lower(), spec["locale"].lower())):
            add_check(checks, f"{name}.requested_{field}", observed == wanted, wanted, observed)
        add_check(checks, f"{name}.requested_ua_os", expected["ua"] in scope.get("ua", ""), expected["ua"], scope.get("ua"))
    return checks


def signal_identity(observation: dict) -> dict:
    signals = observation.get("signals", {})
    canvas, audio = signals.get("canvas", {}), signals.get("audio", {})
    result = {}
    if canvas.get("available"):
        result["canvas"] = {key: canvas.get(key) for key in ("pixelHash", "dataUrlHash", "blobHash")}
    if audio.get("available"):
        result["audio"] = audio.get("control", {}).get("hash")
    return result


def surface_identity(observation: dict) -> dict:
    surfaces = observation.get("surfaces") or {}
    result = {}
    for scope in SCOPES:
        values = surfaces.get(scope) or {}
        canvas = values.get("canvas") or {}
        if canvas.get("available") is True:
            result[scope] = {"pixelHash": canvas.get("pixelHash"),
                             "pngHash": (canvas.get("exports") or {}).get("hash")}
        if scope == "window":
            for key in ("webgl1", "webgl2"):
                value = values.get(key) or {}
                if value.get("available") is True:
                    result[key] = value.get("pixelHash")
            audio = values.get("audio") or {}
            if audio.get("available") is True:
                result["audioGraph"] = (audio.get("first") or {}).get("hash")
    return result


def stable_identity(observation: dict) -> dict:
    return {"scopes": {name: canonical_identity(observation.get(name, {})) for name in SCOPES},
            "signals": signal_identity(observation), "surfaces": surface_identity(observation)}


def finite_nonnegative(value) -> bool:
    return ((type(value) is int and value >= 0)
            or (type(value) is float and math.isfinite(value) and value >= 0))


def evaluate_environment(environment, name: str) -> list:
    checks = []
    if not isinstance(environment, dict):
        add_check(checks, f"{name}.environment.completed", False, "probe result", environment)
        return checks
    for category in ("network", "storage"):
        label = f"{name}.{category}"
        value = environment.get(category)
        if not isinstance(value, dict):
            add_check(checks, label + ".completed", False, "probe result", value)
            continue
        if not probe_available(checks, label, value):
            continue
        if category == "network":
            add_check(checks, label + ".type",
                      value.get("effectiveType") in ("slow-2g", "2g", "3g", "4g"),
                      "standard effectiveType", value.get("effectiveType"))
            for field in ("online", "saveData"):
                add_check(checks, label + "." + field, type(value.get(field)) is bool,
                          "boolean", value.get(field))
            rtt, downlink = value.get("rtt"), value.get("downlink")
            add_check(checks, label + ".rtt", finite_nonnegative(rtt)
                      and rtt == int(rtt) and rtt <= 2**32 - 1,
                      "unsigned 32-bit milliseconds; zero is valid", rtt)
            add_check(checks, label + ".downlink", finite_nonnegative(downlink),
                      "finite nonnegative Mbps; zero is valid", downlink)
        else:
            estimate = value.get("estimate")
            if not isinstance(estimate, dict):
                add_check(checks, label + ".estimate", False, "estimate object", estimate)
                continue
            # WebIDL uint64 values can round to 2**64 when exposed as JS Numbers.
            for field in ("usage", "quota"):
                number = estimate.get(field)
                add_check(checks, label + "." + field, finite_nonnegative(number)
                          and number == int(number) and number <= 2**64,
                          "nonnegative integer byte estimate", number)
            # A quota reduction can legitimately leave existing usage above it.
            if "usageDetails" in estimate:
                details = estimate["usageDetails"]
                add_check(checks, label + ".usage_details", isinstance(details, dict)
                          and all(finite_nonnegative(number) and number == int(number)
                                  and number <= 2**64 for number in details.values()),
                          "nonnegative integer detail estimates", details)
    return checks


def probe_available(checks: list, name: str, value) -> bool:
    if (isinstance(value, dict) and value.get("available") is False
            and isinstance(value.get("reason"), str) and value["reason"].strip()
            and "error" not in value):
        skip(checks, name, value["reason"])
        return False
    completed = isinstance(value, dict) and value.get("available") is True and "error" not in value
    add_check(checks, name + ".completed", completed, "successful probe or explicit absence with reason", value)
    return completed


def valid_hash(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def require_true(checks: list, name: str, value: dict, fields):
    for field in fields:
        add_check(checks, name + "." + field, value.get(field) is True, True, value.get(field))


def evaluate_canvas_surface(value, name: str) -> list:
    checks = []
    if not probe_available(checks, name, value):
        return checks
    require_true(checks, name, value, ("repeat", "cropMatches", "paddedMatches", "outsideZero",
                                      "transparentZero", "sourceStable"))
    add_check(checks, name + ".pixelHash", valid_hash(value.get("pixelHash")), "SHA-256", value.get("pixelHash"))
    add_check(checks, name + ".invalid", value.get("invalid") == "IndexSizeError", "IndexSizeError", value.get("invalid"))
    exports = value.get("exports")
    if probe_available(checks, name + ".exports", exports):
        require_true(checks, name + ".exports", exports, ("png", "repeat"))
        add_check(checks, name + ".exports.mime", exports.get("mime") == "image/png", "image/png", exports.get("mime"))
        add_check(checks, name + ".exports.hash", valid_hash(exports.get("hash")), "binary PNG SHA-256", exports.get("hash"))
        url = exports.get("dataURL")
        if probe_available(checks, name + ".dataURL", url):
            require_true(checks, name + ".dataURL", url, ("png", "matchesBlob", "repeat"))
            add_check(checks, name + ".dataURL.hash", valid_hash(url.get("hash"))
                      and url.get("hash") == exports.get("hash"), exports.get("hash"), url.get("hash"))
    bitmap = value.get("bitmap")
    if probe_available(checks, name + ".bitmap", bitmap):
        require_true(checks, name + ".bitmap", bitmap, ("matchesDirect", "repeat", "decodedRepeat"))
        add_check(checks, name + ".bitmap.decodedHash", valid_hash(bitmap.get("decodedReadbackHash")),
                  "readback hash, not raw decoded PNG pixels", bitmap.get("decodedReadbackHash"))
        transfer = bitmap.get("transfer")
        if probe_available(checks, name + ".bitmap.transfer", transfer):
            require_true(checks, name + ".bitmap.transfer", transfer, ("matchesDirect", "repeat", "sourceCleared"))
    float16 = value.get("float16")
    if probe_available(checks, name + ".float16", float16):
        require_true(checks, name + ".float16", float16, ("finite", "repeat", "typed"))
        add_check(checks, name + ".float16.length", type(float16.get("length")) is int
                  and float16["length"] == 16 * 12 * 4, 768, float16.get("length"))
    return checks


def evaluate_gl_surface(value, name: str, version: int) -> list:
    checks = []
    if not probe_available(checks, name, value):
        return checks
    require_true(checks, name, value, ("requestable", "repeat", "shaderPixels", "pendingReadStable",
                                      "alignedMatches", "alignedGuards", "sourceStable"))
    add_check(checks, name + ".pixelHash", valid_hash(value.get("pixelHash")), "SHA-256", value.get("pixelHash"))
    for key, expected in {"initialError": 0, "drawError": 0, "readError": 0, "pendingError": 0x500,
                          "trailingError": 0, "alignedError": 0, "finalError": 0}.items():
        add_check(checks, name + "." + key, type(value.get(key)) is int and value[key] == expected, expected, value.get(key))
    extensions = value.get("extensions")
    add_check(checks, name + ".extensions", isinstance(extensions, list) and len(extensions) <= 128
              and all(isinstance(item, str) and item for item in extensions)
              and len(set(extensions)) == len(extensions), "unique extension names", extensions)
    invalid = value.get("invalid")
    cases = isinstance(invalid, list) and len(invalid) == 2 and all(isinstance(case, dict) for case in invalid)
    add_check(checks, name + ".invalid_cases", cases, "negative width and invalid format", invalid)
    if cases:
        for i, expected in enumerate((0x501, 0x500)):
            case = invalid[i]
            add_check(checks, name + f".invalid_{i}", type(case.get("error")) is int and case["error"] == expected
                      and case.get("unchanged") is True, {"error": expected, "unchanged": True}, case)
    pack = value.get("pack")
    if probe_available(checks, name + ".pack", pack):
        require_true(checks, name + ".pack", pack, ("matches", "guards", "shortUnchanged"))
        for key, expected in (("glError", 0), ("shortError", 0x502)):
            add_check(checks, name + ".pack." + key, type(pack.get(key)) is int and pack[key] == expected, expected, pack.get(key))
    elif version == 2 and isinstance(pack, dict) and pack.get("available") is False:
        add_check(checks, name + ".pack.required", False, "WebGL2 pack probe required", pack)
    return checks


CODEC_TYPES = ['not-a-mime', 'audio/x-chromix-invalid; codecs="missing"', 'audio/wav; codecs="1"',
               'audio/webm; codecs="opus"', 'video/webm; codecs="vp8"', 'video/mp4; codecs="avc1.42E01E"']


def evaluate_codec_surface(value, name: str) -> list:
    checks = []
    if not probe_available(checks, name, value):
        return checks
    for field in ("canPlay", "mse", "recorder"):
        rows = value.get(field)
        if field != "canPlay":
            if not probe_available(checks, name + "." + field, rows):
                continue
            rows = rows.get("values")
        well_formed = (isinstance(rows, list) and len(rows) == len(CODEC_TYPES)
                       and all(isinstance(row, dict) for row in rows)
                       and [row.get("mime") for row in rows] == CODEC_TYPES)
        add_check(checks, name + "." + field + ".cases", well_formed, CODEC_TYPES, rows)
        if well_formed:
            allowed = all(row.get("value") in ("", "maybe", "probably") if field == "canPlay"
                          else type(row.get("value")) is bool for row in rows)
            add_check(checks, name + "." + field + ".types", allowed, "native support observations", rows)
            unsupported = "" if field == "canPlay" else False
            add_check(checks, name + "." + field + ".invalid", all(row.get("value") == unsupported
                      and type(row.get("value")) is type(unsupported) for row in rows[:2]), unsupported, rows[:2])
    capabilities = value.get("capabilities")
    if probe_available(checks, name + ".capabilities", capabilities):
        # Encoding and decoding have different native support contracts.
        for field in ("decoding", "encoding"):
            rows = capabilities.get(field)
            valid = isinstance(rows, list) and len(rows) == 2 and all(isinstance(row, dict) for row in rows)
            add_check(checks, name + "." + field + ".cases", valid, "invalid codec and Opus observations", rows)
            if valid:
                add_check(checks, name + "." + field + ".types", all(type(row.get(key)) is bool
                          for row in rows for key in ("supported", "smooth", "powerEfficient")), "boolean capability fields", rows)
                add_check(checks, name + "." + field + ".invalid", rows[0].get("supported") is False, False, rows[0])
    return checks


def evaluate_audio_surface(value, name: str) -> list:
    checks = []
    if not probe_available(checks, name, value):
        return checks
    for field in ("first", "second", "silence"):
        row = value.get(field)
        if not isinstance(row, dict):
            add_check(checks, name + "." + field, False, "render evidence", row)
            continue
        require_true(checks, name + "." + field, row, ("repeat", "finite", "zero" if field == "silence" else "nonzero"))
        add_check(checks, name + "." + field + ".hash", valid_hash(row.get("hash")), "SHA-256", row.get("hash"))
        add_check(checks, name + "." + field + ".shape", row.get("rate") == 44100 and row.get("length") == 4096,
                  [44100, 4096], [row.get("rate"), row.get("length")])
    first, second = value.get("first"), value.get("second")
    add_check(checks, name + ".graph_repeat", isinstance(first, dict) and isinstance(second, dict)
              and valid_hash(first.get("hash")) and first.get("hash") == second.get("hash"), first, second)
    boundary = value.get("boundary")
    if not isinstance(boundary, dict):
        add_check(checks, name + ".boundary", False, "mutable buffer evidence", boundary)
    else:
        require_true(checks, name + ".boundary", boundary,
                     ("mutable", "truncated", "beyondUnchanged", "sourceStable", "otherChannelSilent"))
        add_check(checks, name + ".boundary.rates", boundary.get("rates") == [48000, 48000], [48000, 48000], boundary.get("rates"))
        rows = boundary.get("invalid")
        valid = isinstance(rows, list) and len(rows) == 2 and all(isinstance(row, dict) for row in rows)
        add_check(checks, name + ".boundary.invalid", valid and [row.get("index") for row in rows] == [2, -1], [2, -1], rows)
        if valid:
            for i, row in enumerate(rows):
                add_check(checks, name + f".boundary.invalid_{i}", row.get("unchanged") is True
                          and all(row.get(key) == "IndexSizeError" for key in ("from", "to", "get")), "IndexSizeError; unchanged", row)
    wav = value.get("wav")
    if not isinstance(wav, dict):
        add_check(checks, name + ".wav", False, "decoded PCM WAV evidence", wav)
    else:
        samples = wav.get("samples")
        expected = [v / 32768 for v in (0, 8192, -8192, 16384, -16384, 32767, -32768, 0)]
        valid = isinstance(samples, list) and len(samples) == len(expected)
        add_check(checks, name + ".wav.samples", valid and all(type(v) in (int, float) and math.isfinite(v)
                  and abs(v - ref) <= 1 / 32768 for v, ref in zip(samples, expected)), expected, samples)
        require_true(checks, name + ".wav", wav, ("repeat",))
        add_check(checks, name + ".wav.shape", [wav.get(key) for key in ("rate", "length", "channels", "sourceBytes")]
                  == [48000, 8, 1, 60], [48000, 8, 1, 60], wav)
    return checks


def evaluate_webgpu_surface(value, name: str) -> list:
    checks = []
    if not probe_available(checks, name, value):
        return checks
    require_true(checks, name, value, ("copyMatches", "canvasMatches"))
    add_check(checks, name + ".format", value.get("format") in ("bgra8unorm", "rgba8unorm"), "preferred 8-bit canvas format", value.get("format"))
    for field in ("advertised", "enabled"):
        rows = value.get(field)
        add_check(checks, name + "." + field, isinstance(rows, list) and len(rows) <= 128
                  and all(isinstance(row, str) and row for row in rows) and len(rows) == len(set(rows)), "unique feature names", rows)
    add_check(checks, name + ".requestable", isinstance(value.get("advertised"), list)
              and value.get("advertised") == value.get("enabled"), value.get("advertised"), value.get("enabled"))
    add_check(checks, name + ".copied", value.get("copied") == [0x12345678, 0, 0xFFFFFFFF, 0xABCDEF01],
              [0x12345678, 0, 0xFFFFFFFF, 0xABCDEF01], value.get("copied"))
    rgba = value.get("rgba")
    expected = [191, 128, 64, 255] if value.get("format") == "bgra8unorm" else [64, 128, 191, 255]
    add_check(checks, name + ".rgba", isinstance(rgba, list) and len(rgba) == 4
              and all(type(v) is int and abs(v - ref) <= 1 for v, ref in zip(rgba, expected)), expected, rgba)
    add_check(checks, name + ".validation", "validationError" in value and value["validationError"] is None
              and value.get("uncapturedErrors") == [], "no validation/uncaptured errors", value)
    return checks


def evaluate_network_surface(value, name: str) -> list:
    checks = []
    if not probe_available(checks, name, value):
        return checks
    add_check(checks, name + ".fetch", value.get("ok") is True and value.get("status") == 200
              and value.get("body") == "chromix-loopback-timing", "loopback payload and HTTP 200", value)
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        add_check(checks, name + ".entry", False, "one ResourceTiming entry", entries)
        return checks
    entry = entries[0]
    try:
        url = urlsplit(entry.get("name", ""))
        local = allowed_url(entry["name"], f"http://127.0.0.1:{url.port}") and url.path == "/timing"
    except (ValueError, KeyError, TypeError):
        local = False
    add_check(checks, name + ".entry", local and entry.get("initiatorType") == "fetch", "loopback fetch ResourceTiming", entry)
    times = [entry.get(key) for key in ("startTime", "responseStart", "responseEnd")]
    add_check(checks, name + ".ordering", all(finite_nonnegative(v) for v in times) and times == sorted(times),
              "nondecreasing finite native timestamps", times)
    duration = entry.get("duration")
    add_check(checks, name + ".duration", finite_nonnegative(duration) and all(finite_nonnegative(v) for v in times)
              and abs(duration - (times[2] - times[0])) <= 1, "responseEnd minus startTime", duration)
    for field in ("transferSize", "encodedBodySize", "decodedBodySize"):
        size = entry.get(field)
        add_check(checks, name + "." + field, finite_nonnegative(size) and size == int(size), "nonnegative native byte size", size)
    return checks


def evaluate_surfaces(surfaces) -> list:
    checks = []
    if not isinstance(surfaces, dict):
        add_check(checks, "surfaces.completed", False, "window/iframe/worker probe results", surfaces)
        return checks
    for scope in SCOPES:
        value = surfaces.get(scope)
        if not isinstance(value, dict) or "error" in value:
            add_check(checks, f"surfaces.{scope}.completed", False, "scope probe result", value)
            continue
        checks.extend(evaluate_canvas_surface(value.get("canvas"), f"surfaces.{scope}.canvas"))
    window = surfaces.get("window")
    if not isinstance(window, dict):
        return checks
    for scope in ("iframe", "worker"):
        other = surfaces.get(scope)
        a, b = window.get("canvas"), other.get("canvas") if isinstance(other, dict) else None
        if isinstance(a, dict) and isinstance(b, dict) and a.get("available") is True and b.get("available") is True:
            add_check(checks, f"surfaces.{scope}.canvas.matches_window", valid_hash(a.get("pixelHash"))
                      and a.get("pixelHash") == b.get("pixelHash"), a.get("pixelHash"), b.get("pixelHash"))
            x, y = a.get("exports"), b.get("exports")
            if isinstance(x, dict) and isinstance(y, dict) and x.get("available") is True and y.get("available") is True:
                add_check(checks, f"surfaces.{scope}.png.matches_window", valid_hash(x.get("hash"))
                          and x.get("hash") == y.get("hash"), x.get("hash"), y.get("hash"))
    for key, evaluator in (("webgl1", lambda v, n: evaluate_gl_surface(v, n, 1)),
                           ("webgl2", lambda v, n: evaluate_gl_surface(v, n, 2)),
                           ("webgpu", evaluate_webgpu_surface), ("audio", evaluate_audio_surface),
                           ("codecs", evaluate_codec_surface), ("network", evaluate_network_surface)):
        checks.extend(evaluator(window.get(key), "surfaces.window." + key))
    return checks


def evaluate_network_events(value) -> list:
    checks = []
    if not isinstance(value, dict):
        add_check(checks, "network.events.completed", False, "offline/online transition evidence", value)
        return checks
    for scope in ("window", "iframe"):
        rows = value.get(scope)
        if not isinstance(rows, dict):
            add_check(checks, f"network.events.{scope}", False, "scope transition evidence", rows)
            continue
        for phase, online, events in (("initial", True, None), ("offline", False, [{"type": "offline", "online": False}]),
                                       ("restored", True, [{"type": "offline", "online": False}, {"type": "online", "online": True}])):
            row = rows.get(phase)
            passed = isinstance(row, dict) and row.get("online") is online and (events is None or row.get("events") == events)
            add_check(checks, f"network.events.{scope}.{phase}", passed, {"online": online, "events": events}, row)
    return checks


def evaluate_signals(signals: dict) -> list:
    checks = []
    if not isinstance(signals, dict):
        add_check(checks, "signals.completed", False, "signal probe result", signals)
        return checks
    for name in ("canvas", "audio"):
        value = signals.get(name)
        if not probe_available(checks, name, value):
            continue
        if name == "canvas":
            hashes = {key: value.get(key) for key in ("pixelHash", "repeatHash", "dataUrlHash", "blobHash")}
            add_check(checks, "canvas.hashes", all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h)
                      for h in hashes.values()), "SHA-256 for pixels, data URL and blob", hashes)
            add_check(checks, "canvas.repeat", bool(value.get("pixelHash")) and value.get("pixelHash") == value.get("repeatHash")
                      and value.get("dataUrlRepeat") is True, "stable repeated reads", value)
        if name == "audio" and value.get("available"):
            control = value.get("control", {})
            invalid = value.get("invalid", [])
            add_check(checks, "audio.invalid_cases", [case.get("index") for case in invalid] == [1, -1], [1, -1],
                      [case.get("index") for case in invalid])
            for case in [control, *invalid]:
                label = f"audio.index_{case.get('index')}"
                rates = [case.get(key) for key in ("contextSampleRate", "bufferSampleRate", "renderedSampleRate")]
                add_check(checks, label + ".sample_rate", rates == [44100] * 3, [44100] * 3, rates)
                add_check(checks, label + ".repeat", bool(case.get("hash")) and case.get("hash") == case.get("repeatHash"),
                          case.get("hash"), case.get("repeatHash"))
            for case in invalid:
                label = f"audio.index_{case.get('index')}"
                add_check(checks, label + ".exception", (case.get("exception") or {}).get("name") == "IndexSizeError",
                          "IndexSizeError", case.get("exception"))
                add_check(checks, label + ".destination_unchanged", case.get("destinationUnchanged") is True, True,
                          case.get("destinationUnchanged"))
                add_check(checks, label + ".source_unchanged", bool(control.get("hash")) and case.get("hash") == control.get("hash"),
                          control.get("hash"), case.get("hash"))
    return checks


def evaluate_media(media: dict, denied: bool) -> list:
    checks = []
    label = "media.denied" if denied else "media.pristine"
    if not isinstance(media, dict):
        add_check(checks, label + ".completed", False, "media probe result", media)
        return checks
    add_check(checks, label + ".permission_grants", type(media.get("permissionGrantsByRunner")) is int
              and media["permissionGrantsByRunner"] == 0, 0, media.get("permissionGrantsByRunner"))
    add_check(checks, label + ".document_policy", media.get("deniedDocument") is denied, denied, media.get("deniedDocument"))
    permissions = media.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    for name in ("camera", "microphone", "notifications"):
        value = permissions.get(name)
        if probe_available(checks, f"{label}.{name}.query", value):
            add_check(checks, f"{label}.{name}.not_granted", value.get("state") in ("prompt", "denied"),
                      "no authorization was granted", value)
    notification = media.get("notificationPermission")
    if notification is not None:
        state = permissions.get("notifications", {}).get("state")
        add_check(checks, label + ".notification", notification != "granted" and
                  (state is None or state == {"default": "prompt"}.get(notification, notification)), state, notification)
    devices_available = probe_available(checks, label + ".devices", media.get("deviceProbe"))
    if devices_available:
        devices = media.get("devices")
        valid = isinstance(devices, list) and all(isinstance(device, dict) for device in devices)
        add_check(checks, label + ".devices_result", valid and media.get("devicesError") is None,
                  "successful device enumeration", devices)
        if valid:
            exposed = [device for device in devices if device.get("label")]
            add_check(checks, label + ".labels_hidden", not exposed, [], exposed)
    if denied and devices_available:
        for name in ("camera", "microphone"):
            result = media.get("capture", {}).get(name, {})
            if media.get("policyAllows", {}).get(name) is not False:
                add_check(checks, f"{label}.{name}.capture", False, "policy denial must be confirmed; no capture attempted", result)
            else:
                add_check(checks, f"{label}.{name}.capture_rejected",
                          (result.get("exception") or {}).get("name") in ("NotAllowedError", "SecurityError"),
                          "permission/policy rejection, no successful stream", result)
    return checks


def evaluate_observation(observation: dict, spec: dict) -> list:
    checks = []
    for name in SCOPES:
        checks.extend(evaluate_scope(observation.get(name, {}), spec, name))
    window = canonical_identity(observation.get("window", {}))
    for name in ("iframe", "worker"):
        identity = canonical_identity(observation.get(name, {}))
        add_check(checks, f"{name}.matches_window", identity == window, window, identity)
    checks.extend(evaluate_signals(observation.get("signals", {})))
    checks.extend(evaluate_surfaces(observation.get("surfaces")))
    checks.extend(evaluate_network_events(observation.get("network_events")))
    checks.extend(evaluate_media(observation.get("media", {}), False))
    checks.extend(evaluate_media(observation.get("media_denied", {}), True))
    return checks


def evaluate_matrix(scenarios: list[dict]) -> list:
    checks = []
    complete = [scenario for scenario in scenarios if scenario.get("observation")]
    add_check(checks, "matrix.all_scenarios_observed", bool(scenarios) and len(complete) == len(scenarios),
              len(scenarios), len(complete))
    natives = [s for s in complete if s["mode"] == "native"]
    if len(natives) != 2:
        add_check(checks, "matrix.native_control", False, 2, len(natives))
        return checks
    add_check(checks, "matrix.native_profile_reused", bool(natives[0].get("profile"))
              and natives[0].get("profile") == natives[1].get("profile"),
              natives[0].get("profile"), natives[1].get("profile"))
    baseline = stable_identity(natives[0]["observation"])
    other = stable_identity(natives[1]["observation"])
    add_check(checks, "matrix.native_restart_stable", baseline == other, baseline, other)
    cells = {(s["platform"], s["locale"]) for s in scenarios if s["mode"] == "on"}
    add_check(checks, "matrix.persona_cells_present", bool(cells), "one or more", len(cells))
    for platform, locale in sorted(cells):
        group = [s for s in complete if (s["platform"], s["locale"]) == (platform, locale)]
        on, off = [s for s in group if s["mode"] == "on"], [s for s in group if s["mode"] == "off"]
        label = f"matrix.{platform}.{locale}"
        if len(on) != 3 or len(off) != 1:
            add_check(checks, label + ".complete", False, "3 on, 1 off", [len(on), len(off)])
            continue
        first, restart, different = on
        add_check(checks, label + ".profile_reused", bool(first.get("profile"))
                  and first.get("profile") == restart.get("profile"),
                  first.get("profile"), restart.get("profile"))
        first_id = stable_identity(first["observation"])
        restart_id = stable_identity(restart["observation"])
        off_id = stable_identity(off[0]["observation"])
        add_check(checks, label + ".same_seed_restart", first_id == restart_id, first_id, restart_id)
        add_check(checks, label + ".off_matches_native", baseline == off_id, baseline, off_id)
        a, b, disabled = (signal_identity(s["observation"]) for s in (first, different, off[0]))
        common = set(a) & set(b) & set(disabled)
        changed = sorted(key for key in common if a[key] != b[key])
        effect = sorted(key for key in common if a[key] != disabled[key])
        add_check(checks, label + ".seed_changes_signal", bool(changed), "at least one canvas/audio path changes", changed)
        add_check(checks, label + ".persona_changes_signal", bool(effect), "at least one canvas/audio path differs from off", effect)
    return checks


def verify_execution(cdp, identity: dict, sandbox: bool) -> dict:
    try:
        version = cdp.send("Browser.getVersion")
        command = cdp.send("Browser.getBrowserCommandLine")["arguments"]
        if not command:
            raise SmokeError("browser did not expose its executed command line")
        actual = binary_identity(command[0])
        if actual != identity:
            raise SmokeError(f"executed binary differs from requested binary: {actual}")
        if sandbox and any(arg == "--no-sandbox" or arg.startswith("--no-sandbox=") for arg in command):
            raise SmokeError("browser unexpectedly launched without the sandbox")
        return {"binary": actual, "command_line": command, "version": version,
                "source": "CDP Browser.getBrowserCommandLine (not OS process attestation)"}
    finally:
        cdp.detach()


def evaluate(target, script: str, argument, timeout_ms: int):
    bounded = f"""async (argument) => {{
      let timer;
      try {{
        return await Promise.race([({script})(argument), new Promise((_, reject) => {{
          timer = setTimeout(() => reject(new Error('probe timeout')), {timeout_ms});
        }})]);
      }} finally {{ clearTimeout(timer); }}
    }}"""
    return target.evaluate(bounded, argument)


def collect_page(page, origin: str, timeout_ms: int, spec: dict, phase: str) -> dict:
    if phase == "reload":
        page.reload(wait_until="load")
    else:
        page.goto(origin + "/", wait_until="load")
    frame = page.frame(url=origin + "/frame")
    if frame is None:
        raise SmokeError("same-origin iframe did not load")
    frame.wait_for_load_state("load", timeout=timeout_ms)
    request = {"scenario": spec["name"], "restart": str(spec["restart"]), "phase": phase}
    return {"window": evaluate(page, NAV_PROBE, {**request, "scope": "window"}, timeout_ms),
            "iframe": evaluate(frame, NAV_PROBE, {**request, "scope": "iframe"}, timeout_ms),
            "worker": evaluate(page, WORKER_PROBE, request, timeout_ms),
            "signals": evaluate(page, SIGNAL_PROBE, None, timeout_ms),
            "surfaces": {"window": evaluate(page, SURFACE_PROBE, "all", timeout_ms),
                         "iframe": evaluate(frame, SURFACE_PROBE, "canvas", timeout_ms),
                         "worker": evaluate(page, SURFACE_PROBE, "worker", timeout_ms)},
            "media": evaluate(page, MEDIA_PROBE, False, timeout_ms)}


def collect_network_events(context, page, origin: str, timeout_ms: int) -> dict:
    frame = page.frame(url=origin + "/frame")
    if frame is None:
        raise SmokeError("network event iframe missing")
    targets = {"window": page, "iframe": frame}
    result = {name: {"initial": evaluate(target, NETWORK_EVENT_SETUP, None, timeout_ms)}
              for name, target in targets.items()}
    try:
        context.set_offline(True)
        for name, target in targets.items():
            result[name]["offline"] = evaluate(target, NETWORK_EVENT_READ, "offline", timeout_ms)
    finally:
        context.set_offline(False)
    for name, target in targets.items():
        result[name]["restored"] = evaluate(target, NETWORK_EVENT_READ, "online", timeout_ms)
    return result


def run_scenario(playwright, spec: dict, identity: dict, server: LocalServer, args, profile: Path) -> dict:
    result = {**spec, "args": browser_args(spec, server.origin, args.no_sandbox),
              "profile": str(profile), "profile_fresh": not profile.exists(),
              "launch_options": {"headless": not args.headed, "chromium_sandbox": not args.no_sandbox},
              "checks": [], "failures": [], "blocked_requests": [], "observation": None,
              "execution": None}
    start = len(server.snapshot())
    context = None
    closing = False
    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), executable_path=identity["path"], args=result["args"],
            headless=not args.headed, chromium_sandbox=not args.no_sandbox,
            accept_downloads=False, service_workers="block", timeout=args.timeout_ms)
        context.set_default_timeout(args.timeout_ms)
        context.set_default_navigation_timeout(args.timeout_ms)

        def guard(route):
            request = route.request
            previous = request.redirected_from
            if allowed_url(request.url, server.origin) and (previous is None or allowed_url(previous.url, server.origin)):
                route.continue_()
            else:
                result["blocked_requests"].append(request.url)
                route.abort("blockedbyclient")

        def watch_request(request):
            if not allowed_url(request.url, server.origin):
                result["blocked_requests"].append(request.url)

        def watch_page(page):
            page.on("crash", lambda *_: result["failures"].append({"name": "page_crash"}))
            page.on("pageerror", lambda error: result["failures"].append(
                {"name": "page_error", "message": str(error)}))

        def disconnected(*_):
            if not closing:
                result["failures"].append({"name": "unexpected_browser_disconnect"})

        def block_websocket(socket):
            result["blocked_requests"].append(socket.url)
            socket.close()

        context.route("**/*", guard)
        context.route_web_socket("**/*", block_websocket)
        context.on("request", watch_request)
        context.on("page", watch_page)
        context.on("close", disconnected)
        if context.browser is not None:
            context.browser.on("disconnected", disconnected)
        for existing in context.pages:
            watch_page(existing)
        page = context.new_page()
        result["execution"] = verify_execution(context.new_cdp_session(page), identity, not args.no_sandbox)
        observation = collect_page(page, server.origin, args.timeout_ms, spec, "initial")
        result["observation"] = observation
        observation["network_events"] = collect_network_events(context, page, server.origin, args.timeout_ms)
        reload_observation = collect_page(page, server.origin, args.timeout_ms, spec, "reload")
        result["reload_observation"] = reload_observation
        add_check(result["checks"], "reload.stable", stable_identity(observation) == stable_identity(reload_observation),
                  stable_identity(observation), stable_identity(reload_observation))
        for name in SCOPES:
            result["checks"].extend(evaluate_scope(reload_observation[name], spec, name, "reload"))
        result["checks"].extend(evaluate_signals(reload_observation["signals"]))
        result["checks"].extend(evaluate_surfaces(reload_observation.get("surfaces")))
        denied = context.new_page()
        denied.goto(server.origin + "/denied", wait_until="load")
        observation["media_denied"] = evaluate(denied, MEDIA_PROBE, True, args.timeout_ms)
        result["checks"].extend(evaluate_observation(observation, spec))
    except Exception as error:
        result["failures"].append({"name": type(error).__name__, "message": str(error)})
    finally:
        closing = True
        if context is not None:
            try:
                context.close()
            except Exception as error:
                result["failures"].append({"name": "browser_close", "message": str(error)})
        result["requests"] = server.snapshot()[start:]
    add_check(result["checks"], "network.only_local_origin", not result["blocked_requests"], [], result["blocked_requests"])
    result["failures"].extend(check for check in result["checks"] if check["status"] == "failed")
    result["status"] = "failed" if result["failures"] else "passed"
    return result


def load_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise SmokeError("Python Playwright package is not installed; no package or browser was downloaded") from error
    return sync_playwright


def finalize_report(report: dict) -> dict:
    all_checks = [*report["checks"], *(check for scenario in report["scenarios"] for check in scenario.get("checks", []))]
    skipped = sorted({check["name"] for check in all_checks if check["status"] == "not_supported"})
    report["coverage"] = {"scope": "bounded smoke probes, not exhaustive platform coverage",
                          "not_supported": skipped, "has_optional_skips": bool(skipped)}
    for scenario in report["scenarios"]:
        for failure in scenario.get("failures", []):
            report["failures"].append({"scenario": scenario["name"], "failure": failure})
    report["failures"].extend(check for check in report["checks"] if check["status"] == "failed")
    if not report["scenarios"] and not report["failures"]:
        report["failures"].append({"name": "no_browser_evidence", "message": "No browser scenarios ran"})
    report["status"] = "failed" if report["failures"] else "passed"
    if report["status"] == "failed":
        report.get("verification", {})["runtime_verified"] = False
    return report


def run(args) -> dict:
    report = {"schema_version": 1, "runner": "fingerprint_smoke", "status": "failed",
              "browser": {"requested_path": str(args.browser), "path": None, "sha256": None},
              "parameters": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
              "verification": {"kind": "browser_smoke", "browser_scenarios_completed": 0,
                               "runtime_verified": False, "provenance_authenticated": False},
              "scenarios": [], "checks": [], "failures": [], "limitations": LIMITATIONS}
    try:
        identity = binary_identity(args.browser)
        report["browser"].update(identity)
        if args.expected_sha256:
            matched = identity["sha256"] == args.expected_sha256
            add_check(report["checks"], "binary.expected_sha256", matched, args.expected_sha256, identity["sha256"])
            if not matched:
                raise SmokeError("browser SHA-256 does not match --expected-sha256")
        if args.output and same_file(args.output, Path(identity["path"])):
            raise SmokeError("--output must not overwrite the browser executable")
        if args.seed == args.other_seed:
            raise SmokeError("--seed and --other-seed must differ")
        specs = scenario_matrix(args)
        if len(specs) > 34:
            raise SmokeError("matrix limited to eight platform/locale cells (34 launches)")
        sync_playwright = load_playwright()
        report["verification"]["playwright_version"] = importlib.metadata.version("playwright")
        with tempfile.TemporaryDirectory(prefix="chromix-smoke-") as profiles, local_server() as server, sync_playwright() as playwright:
            report["origin"] = server.origin
            for spec in specs:
                profile = Path(profiles) / profile_group(spec)
                scenario = run_scenario(playwright, spec, identity, server, args, profile)
                report["scenarios"].append(scenario)
                if scenario["status"] == "failed":
                    break
            add_check(report["checks"], "matrix.all_scenarios_run", len(report["scenarios"]) == len(specs),
                      len(specs), len(report["scenarios"]))
            report["checks"].extend(evaluate_matrix(report["scenarios"]))
        report["verification"]["browser_scenarios_completed"] = sum(
            bool(s["observation"]) for s in report["scenarios"])
        report["verification"]["runtime_verified"] = len(report["scenarios"]) == len(specs) and all(
            s["status"] == "passed" for s in report["scenarios"])
        actual = binary_identity(args.browser)
        add_check(report["checks"], "binary.unchanged_after_run", actual == identity, identity, actual)
    except Exception as error:
        report["failures"].append({"name": type(error).__name__, "message": str(error)})
    return finalize_report(report)


def seed_value(value):
    try:
        seed = int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("seed must be a nonzero uint64 integer") from error
    if not 1 <= seed <= 0xFFFFFFFFFFFFFFFF:
        raise argparse.ArgumentTypeError("seed must be in [1, 18446744073709551615]")
    return seed


def locale_value(value):
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value):
        raise argparse.ArgumentTypeError("use a single locale tag such as de-DE")
    return value


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def same_file(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve() or (
        left.exists() and right.exists() and left.samefile(right))


def sha256_value(value):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise argparse.ArgumentTypeError("expected 64 hexadecimal SHA-256 characters")
    return value.lower()


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", required=True, type=Path,
                        help="explicit existing native Chromix binary (may be named chrome); verify provenance independently")
    parser.add_argument("--expected-sha256", type=sha256_value,
                        help="optional independently obtained binary SHA-256; not automatic provenance authentication")
    parser.add_argument("--seed", type=seed_value, default=0x13579BDF)
    parser.add_argument("--other-seed", type=seed_value, default=0x2468ACE0)
    parser.add_argument("--platform", nargs="+", choices=tuple(PLATFORMS), default=list(PLATFORMS))
    parser.add_argument("--locale", nargs="+", type=locale_value, default=["de-DE"])
    parser.add_argument("--timeout-ms", type=positive_int, default=30000)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--no-sandbox", action="store_true", help="explicit opt-in; never added automatically for root")
    parser.add_argument("--output", type=Path, help="also write the JSON report here")
    return parser


def main(argv=None) -> int:
    args = argument_parser().parse_args(argv)
    report = run(args)
    if args.output:
        try:
            if same_file(args.output, args.browser):
                raise SmokeError("--output must not overwrite the browser executable")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError) as error:
            report["status"] = "failed"
            report["failures"].append({"name": "output_error", "message": str(error)})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
