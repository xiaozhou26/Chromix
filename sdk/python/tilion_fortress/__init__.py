"""
tilion-fortress — install and drive the Fortress stealth Chromium engine.

Ships the prebuilt binary only (no engine source). Detects the platform, downloads the
matching bundle from the official GitHub Release, verifies it against SHA256SUMS, caches
it, and launches it with a CDP endpoint. **Linux x64 and Windows x64 have native binaries**
(downloaded automatically — no Docker needed). macOS has no native bundle yet and uses the
Docker image; Docker is also the fallback anywhere a native launch isn't available.

    from tilion_fortress import Fortress
    with Fortress() as f:
        print(f.cdp_url)   # connect any CDP client (Playwright/Puppeteer)
"""
from __future__ import annotations
import hashlib, json, os, platform, shutil, subprocess, sys, tarfile, time, urllib.request, zipfile
from pathlib import Path

__version__ = "151.0.7922.138"
__all__ = ["Fortress", "resolve_platform"]

# CloakBrowser-compatible API (from tilion_fortress import launch, ...) — lazily
# re-exported so importing the SDK never pulls in the compat layer's heavier
# machinery until a caller actually uses it.
def __getattr__(name):
    if name in ("launch", "launch_async", "launch_context", "launch_context_async",
                "launch_persistent_context", "launch_persistent_context_async",
                "ProxySettings", "build_args", "maybe_resolve_geoip",
                "get_default_stealth_args", "ensure_binary", "clear_cache",
                "binary_info", "check_for_update", "HumanConfig",
                "resolve_human_config"):
        from . import cloak
        return getattr(cloak, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

_REPO = "tiliondev/fortress"
# Two release channels. "stable" = Chromium 149 (the recommended default — it matches the Chrome
# version the mass of real users run, so it blends in best). "latest" = 151 (newest engine).
# Override per-instance with channel=..., or globally with the FORTRESS_CHANNEL env var.
_CHANNELS = {
    "stable": {"tag": "v149.0.7827.232", "docker": "tilion/fortress:149.0.7827.232"},
    "latest": {"tag": "v151.0.7922.138", "docker": "tilion/fortress:151.0.7922.138"},
}
_DEFAULT_CHANNEL = os.environ.get("FORTRESS_CHANNEL", "stable")
_CACHE = Path(os.environ.get("FORTRESS_BROWSERS_PATH",
                             Path.home() / ".cache" / "tilion-fortress"))


def _host(tag: str) -> str:
    return os.environ.get("FORTRESS_DOWNLOAD_HOST",
                          f"https://github.com/{_REPO}/releases/download/{tag}")

# platform key -> (release asset, archive kind, launcher relative path)
_ASSETS = {
    "linux-x64":  ("tilion-fortress-linux-x64.tar.gz", "tar", "tilion-fortress/tilion"),
    "win-x64":    ("tilion-fortress-win-x64.zip",       "zip", "tilion-fortress/tilion.cmd"),
    "mac-arm64":  ("tilion-fortress-mac-arm64.tar.gz",  "tar", "tilion-fortress/tilion"),
    "mac-x64":    ("tilion-fortress-mac-x64.tar.gz",    "tar", "tilion-fortress/tilion"),
}


def resolve_platform() -> str | None:
    sysname, mach = platform.system(), platform.machine().lower()
    if sysname == "Linux" and mach in ("x86_64", "amd64"):
        return "linux-x64"
    if sysname == "Windows" and mach in ("amd64", "x86_64"):
        return "win-x64"
    if sysname == "Darwin":
        return "mac-arm64" if mach in ("arm64", "aarch64") else "mac-x64"
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _expected_sha(asset: str, host: str) -> str | None:
    """Fetch SHA256SUMS from the release and return the hash for `asset`."""
    try:
        with urllib.request.urlopen(f"{host}/SHA256SUMS", timeout=30) as r:
            for line in r.read().decode().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("*") == asset:
                    return parts[0].lower()
    except Exception:
        return None
    return None


def _download(plat: str, host: str, tag: str) -> Path:
    """Ensure the bundle for `plat` is present + verified; return the launcher path."""
    asset, kind, launcher_rel = _ASSETS[plat]
    root = _CACHE / tag / plat   # cache per release tag so channels don't collide
    launcher = root / launcher_rel
    if launcher.exists():
        return launcher
    root.mkdir(parents=True, exist_ok=True)
    archive = root / asset
    url = f"{host}/{asset}"
    sys.stderr.write(f"[tilion-fortress] downloading {url} ...\n")
    urllib.request.urlretrieve(url, archive)

    expected = _expected_sha(asset, host)
    if expected:
        actual = _sha256(archive)
        if actual != expected:
            archive.unlink(missing_ok=True)
            raise RuntimeError(f"SHA256 mismatch for {asset}: expected {expected}, got {actual}")
        sys.stderr.write("[tilion-fortress] SHA256 verified\n")
    else:
        sys.stderr.write("[tilion-fortress] WARNING: no SHA256SUMS published; skipping verification\n")

    if kind == "tar":
        with tarfile.open(archive) as t:
            t.extractall(root)
    else:
        with zipfile.ZipFile(archive) as z:
            z.extractall(root)
    archive.unlink(missing_ok=True)
    if launcher.exists() and not launcher.name.endswith(".cmd"):
        launcher.chmod(0o755)
    if not launcher.exists():
        raise RuntimeError(f"bundle extracted but launcher missing: {launcher}")
    return launcher


def _persona_args(persona: dict | None) -> list[str]:
    if not persona:
        return []
    mapping = {
        "platform": "--uxr-platform", "timezone": "--uxr-timezone",
        "languages": "--uxr-languages", "webgl_renderer": "--uxr-webgl-renderer",
        "webgl_vendor": "--uxr-webgl-vendor", "hw_concurrency": "--uxr-hw-concurrency",
        "device_memory": "--uxr-device-memory", "screen_width": "--uxr-screen-width",
        "screen_height": "--uxr-screen-height", "canvas_seed": "--uxr-canvas-seed",
    }
    return [f"{mapping.get(k, '--uxr-' + k.replace('_', '-'))}={v}" for k, v in persona.items()]


def _fingerprint_args(fp: dict | None) -> list[str]:
    """Map fingerprint persona keys to --fingerprint-* CLI switches.

    These are normalized by the browser process (patch 0036) into uxr-*
    equivalents, so the SDK can use the unified fingerprint-* namespace.
    """
    if not fp:
        return []
    fp_map = {
        "seed": "--fingerprint",
        "platform": "--fingerprint-platform",
        "gpu_vendor": "--fingerprint-gpu-vendor",
        "gpu_renderer": "--fingerprint-gpu-renderer",
        "hardware_concurrency": "--fingerprint-hardware-concurrency",
        "device_memory": "--fingerprint-device-memory",
        "screen_width": "--fingerprint-screen-width",
        "screen_height": "--fingerprint-screen-height",
        "taskbar_height": "--fingerprint-taskbar-height",
        "brand": "--fingerprint-brand",
        "brand_version": "--fingerprint-brand-version",
        "platform_version": "--fingerprint-platform-version",
        "timezone": "--fingerprint-timezone",
        "locale": "--fingerprint-locale",
        "location": "--fingerprint-location",
        "storage_quota": "--fingerprint-storage-quota",
        "fonts_dir": "--fingerprint-fonts-dir",
        "windows_font_metrics": "--fingerprint-windows-font-metrics",
        "font_whitelist": "--uxr-font-whitelist",
        "webrtc_ip": "--fingerprint-webrtc-ip",
        "noise": "--fingerprint-noise",
        "sapi_voices": "--fingerprint-sapi-voices",
        "allow_3p_cookies": "--fingerprint-allow-3p-cookies",
    }
    return [f"{fp_map.get(k, '--fingerprint-' + k.replace('_', '-'))}={v}"
            for k, v in fp.items()]


# Switches that would silently degrade stealth if a caller passed them.
_FORBIDDEN_ARGS = {
    # Forces the SwiftShader software rasterizer; its renderer string and
    # render-output hashes are an instant fingerprint match.
    "--enable-unsafe-swiftshader",
}


def _sanitize_args(extra: list[str]) -> list[str]:
    """Drop stealth-breaking switches the caller should not have passed."""
    kept, dropped = [], []
    for a in extra:
        key = a.split("=", 1)[0]
        (dropped if key in _FORBIDDEN_ARGS else kept).append(a)
    if dropped:
        sys.stderr.write(f"[tilion-fortress] dropped stealth-breaking args: {dropped}\n")
    return kept


# Timezone by longitude band (approximate but coherent with the proxy geo).
_TZ_BANDS = [(-180, "Etc/GMT+12"), (-165, "Etc/GMT+11"), (-150, "Etc/GMT+10"),
             (-135, "Etc/GMT+9"), (-120, "Etc/GMT+8"), (-105, "Etc/GMT+7"),
             (-90, "Etc/GMT+6"), (-75, "Etc/GMT+5"), (-60, "Etc/GMT+4"),
             (-45, "Etc/GMT+3"), (-30, "Etc/GMT+2"), (-15, "Etc/GMT+1"),
             (0, "Etc/GMT"), (15, "Etc/GMT-1"), (30, "Etc/GMT-2"),
             (45, "Etc/GMT-3"), (60, "Etc/GMT-4"), (75, "Etc/GMT-5"),
             (90, "Etc/GMT-6"), (105, "Etc/GMT-7"), (120, "Etc/GMT-8"),
             (135, "Etc/GMT-9"), (150, "Etc/GMT-10"), (165, "Etc/GMT-11"),
             (180, "Etc/GMT-12")]


def _tz_for_longitude(lon: float) -> str:
    for edge, tz in _TZ_BANDS:
        if lon < edge:
            return tz
    return "Etc/GMT-12"


def _geoip_lookup(proxy: str | None, timeout: float) -> dict | None:
    """Query ip-api.com for the egress geo (through the proxy when given)."""
    url = "http://ip-api.com/json/?fields=status,lat,lon,timezone,countryCode"
    opener = urllib.request.build_opener()
    if proxy:
        opener.addheaders = []  # keep default headers
        handler = urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
    try:
        with opener.open(url, timeout=timeout) as r:
            data = json.load(r)
        if data.get("status") == "success":
            return data
    except Exception:
        pass
    return None


class Fortress:
    """A running Fortress instance exposing a CDP endpoint at ``cdp_url``."""

    def __init__(self, port: int = 9222, persona: dict | None = None,
                 extra_args: list[str] | None = None, headless: bool = True,
                 channel: str | None = None, fingerprint: dict | None = None,
                 geoip: bool = False, proxy: str | None = None,
                 gpu_blocklist: bool = True, widevine: bool = False):
        self.port, self.persona, self.headless = port, dict(persona or {}), headless
        self.extra_args = _sanitize_args(extra_args or [])
        self.fingerprint = dict(fingerprint or {})
        self.proxy = proxy
        if geoip:
            self._apply_geoip()
        # Keep the GPU off the software-fallback path: with the blocklist
        # active, an unsupported host GPU falls back to SwiftShader whose
        # renderer string and render hashes are an instant tell.
        if gpu_blocklist and "--ignore-gpu-blocklist" not in self.extra_args:
            self.extra_args.append("--ignore-gpu-blocklist")
        # Widevine: the persona claims Google Chrome, so EME must answer. Env
        # form (any of these) is equivalent to widevine=True.
        self.widevine = widevine or any(
            os.environ.get(v) in ("1", "true", "True")
            for v in ("TILION_WIDEVINE", "FORTRESS_WIDEVINE",
                      "CLOAKBROWSER_WIDEVINE", "CLOAKBROWSER_FETCH_WIDEVINE"))
        ch = channel or _DEFAULT_CHANNEL
        if ch not in _CHANNELS:
            raise ValueError(f"unknown channel {ch!r}; use one of {list(_CHANNELS)}")
        self.channel = ch
        self._tag = _CHANNELS[ch]["tag"]
        self._docker = _CHANNELS[ch]["docker"]
        self._host = _host(self._tag)
        self._proc = self._docker_name = self.cdp_url = None
        if self.widevine:
            from . import widevine as _wv
            cdm = _wv.ensure_widevine()
            if cdm:
                self.extra_args.append(_wv.widevine_flag(cdm))
            else:
                sys.stderr.write(
                    "[tilion-fortress] widevine requested but no CDM source found "
                    "(install Google Chrome or set CLOAKBROWSER_WIDEVINE_CDM); "
                    "continuing without DRM\n")

    def _apply_geoip(self, timeout: float = 10.0):
        """Align timezone/locale with the egress IP (geoip=True).

        Overrides AGENTS.md rule 5 ("match the persona to your egress")
        mechanically: timezone always follows the exit IP; languages only
        when the caller did not pin them. Fail-open on lookup errors.
        """
        geo = _geoip_lookup(self.proxy, timeout)
        if not geo:
            sys.stderr.write("[tilion-fortress] geoip lookup failed; persona unchanged\n")
            return
        tz = geo.get("timezone")
        if tz and "timezone" not in self.persona:
            self.persona["timezone"] = tz
        elif not tz and geo.get("lon") is not None:
            derived = _tz_for_longitude(float(geo["lon"]))
            if "timezone" not in self.persona:
                self.persona["timezone"] = derived
        cc = (geo.get("countryCode") or "").lower()
        if cc and "languages" not in self.persona:
            self.persona["languages"] = cc
        sys.stderr.write(
            f"[tilion-fortress] geoip aligned persona (tz={self.persona.get('timezone')}, "
            f"lang={self.persona.get('languages')})\n")

    def start(self) -> "Fortress":
        plat = resolve_platform()
        # native bundle exists for Linux today; native win/mac assets resolve here once published.
        native_ok = plat is not None and (plat == "linux-x64" or self._asset_exists(plat))
        if native_ok:
            self._start_native(plat)
        else:
            self._start_docker()
        self.cdp_url = self._wait_cdp()
        return self

    def _asset_exists(self, plat: str) -> bool:
        asset = _ASSETS[plat][0]
        try:
            req = urllib.request.Request(f"{self._host}/{asset}", method="HEAD")
            with urllib.request.urlopen(req, timeout=15):
                return True
        except Exception:
            return False

    def _start_native(self, plat: str):
        launcher = _download(plat, self._host, self._tag)
        flags = []
        if self.headless:
            flags += ["--headless=new", "--no-sandbox"]
        flags += [f"--remote-debugging-port={self.port}", f"--user-data-dir={_CACHE / 'profile'}"]
        flags += _persona_args(self.persona) + _fingerprint_args(self.fingerprint) + self.extra_args
        # A Windows .cmd launcher cannot be spawned directly by CreateProcess
        # (WinError 193); run it through cmd.exe. POSIX launchers exec in place.
        if str(launcher).lower().endswith(".cmd"):
            args = ["cmd", "/c", str(launcher)] + flags
        else:
            args = [str(launcher)] + flags
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _start_docker(self):
        if not shutil.which("docker"):
            raise RuntimeError(
                "No native Fortress binary for this platform yet and Docker is not installed. "
                "Install Docker Desktop, or run on Linux x64.")
        self._docker_name = f"tilion-fortress-{os.getpid()}-{self.port}"
        args = ["docker", "run", "-d", "--rm", "--name", self._docker_name,
                "-p", f"{self.port}:9222", self._docker] + _persona_args(self.persona) + _fingerprint_args(self.fingerprint) + self.extra_args
        subprocess.run(args, check=True, stdout=subprocess.DEVNULL)

    def _wait_cdp(self, timeout: float = 90.0) -> str:
        deadline = time.time() + timeout
        url = f"http://127.0.0.1:{self.port}/json/version"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    return json.load(r)["webSocketDebuggerUrl"]
            except Exception:
                time.sleep(0.5)
        raise TimeoutError("Fortress CDP endpoint did not come up")

    def close(self):
        if self._proc:
            # On Windows the launcher runs under cmd.exe; terminate() would only
            # kill cmd and orphan chrome.exe, so kill the whole process tree.
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self._proc.terminate()
            self._proc = None
        if self._docker_name:
            subprocess.run(["docker", "rm", "-f", self._docker_name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._docker_name = None

    def __enter__(self): return self.start()
    def __exit__(self, *exc): self.close()

    @classmethod
    def launch(cls, **kw) -> "Fortress": return cls(**kw).start()
