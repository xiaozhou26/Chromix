"""CloakBrowser-compatible API surface for tilion-fortress.

Mirrors the ``cloakbrowser`` Python wrapper (github.com/CloakHQ/CloakBrowser)
so existing CloakBrowser scripts run on Fortress by changing only the import:

    - from cloakbrowser import launch
    + from tilion_fortress import launch

Same function names, same keyword arguments, same return types (Playwright
Browser / BrowserContext objects), same env-var names (CLOAKBROWSER_*). The
engine underneath is Fortress: the ``--fingerprint-*`` flags this layer emits
are normalized to the engine's ``--uxr-*`` persona switches by patch 0036.

Intentional differences:
  - ``license_key`` is accepted and ignored (Fortress ships one open tier).
  - ``geoip`` uses ip-api.com over HTTP instead of a local GeoLite2 database;
    no extra dependency, same semantics (explicit timezone/locale win).
  - The humanize layer covers mouse / typing / scroll / click wrapping.
"""
from __future__ import annotations
import json
import os
import random
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any, TypedDict

from . import (_CACHE, _CHANNELS, _download, _host, resolve_platform)
from .humanize import HumanConfig, HumanConfigOverrides, HumanPreset, resolve_human_config

__all__ = [
    "launch", "launch_async", "launch_context", "launch_context_async",
    "launch_persistent_context", "launch_persistent_context_async",
    "ProxySettings", "build_args", "maybe_resolve_geoip",
    "get_default_stealth_args", "ensure_binary", "clear_cache",
    "binary_info", "check_for_update", "HumanConfig", "resolve_human_config",
]

DEFAULT_VIEWPORT = {"width": 1920, "height": 947}


class _ProxySettingsRequired(TypedDict):
    server: str


class ProxySettings(_ProxySettingsRequired, total=False):
    """Playwright-compatible proxy configuration."""
    bypass: str
    username: str
    password: str


# ---------------------------------------------------------------------------
# Binary management (CLOAKBROWSER_* env aliases)
# ---------------------------------------------------------------------------

def _channel_for(browser_version: str | None, release_channel: str | None) -> str:
    ver = browser_version or os.environ.get("CLOAKBROWSER_VERSION")
    ch = release_channel or os.environ.get("CLOAKBROWSER_RELEASE_CHANNEL")
    if ch in _CHANNELS:
        return ch
    if ver:
        for name, spec in _CHANNELS.items():
            if spec["tag"].lstrip("v").startswith(ver.split(".")[0]):
                return name
    return "stable"


def _chrome_binary(plat: str, tag: str) -> Path:
    root = _CACHE / tag / plat
    name = "chrome.exe" if plat == "win-x64" else "chrome"
    return root / "tilion-fortress" / name


def ensure_binary(license_key: str | None = None,
                  browser_version: str | None = None,
                  release_channel: str | None = None) -> Path:
    """Return the path to the Fortress chrome binary, downloading if needed.

    ``license_key`` is accepted for call-compatibility and ignored.
    """
    explicit = os.environ.get("CLOAKBROWSER_BINARY_PATH")
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise FileNotFoundError(f"CLOAKBROWSER_BINARY_PATH does not exist: {p}")
    plat = resolve_platform()
    if plat is None:
        raise RuntimeError(
            "No native Fortress binary for this platform; use the Docker image "
            "(tilion/fortress) or Linux x64 / Windows x64.")
    ch = _channel_for(browser_version, release_channel)
    tag = _CHANNELS[ch]["tag"]
    chrome = _chrome_binary(plat, tag)
    if chrome.exists():
        return chrome
    _download(plat, _host(tag), tag)
    if not chrome.exists():
        raise RuntimeError(f"bundle extracted but chrome binary missing: {chrome}")
    return chrome


def binary_info(browser_version: str | None = None,
                release_channel: str | None = None) -> dict:
    ch = _channel_for(browser_version, release_channel)
    tag = _CHANNELS[ch]["tag"]
    plat = resolve_platform() or "unknown"
    chrome = _chrome_binary(plat, tag)
    return {
        "tier": "open-source",
        "version": tag.lstrip("v"),
        "channel": ch,
        "platform": plat,
        "path": str(chrome) if chrome.exists() else None,
        "installed": chrome.exists(),
        "cache_dir": str(_CACHE),
    }


def clear_cache() -> None:
    if _CACHE.exists():
        shutil.rmtree(_CACHE, ignore_errors=True)


def check_for_update() -> dict:
    """Compare the installed stable tag against the latest GitHub release."""
    current = _CHANNELS["stable"]["tag"]
    latest = current
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/tiliondev/fortress/releases/latest",
            headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            latest = json.load(r).get("tag_name", current)
    except Exception:
        pass
    return {"current_version": current.lstrip("v"),
            "latest_version": latest.lstrip("v"),
            "update_available": latest != current}


# ---------------------------------------------------------------------------
# Stealth args
# ---------------------------------------------------------------------------

def get_default_stealth_args() -> list[str]:
    """Stealth defaults with a random per-launch fingerprint seed.

    macOS runs native (no Windows spoofing); Linux/Windows claim the Windows
    persona — normalized to --uxr-* by the engine's chrome_main patch.
    """
    seed = random.randint(10000, 99999)
    base = ["--no-sandbox", f"--fingerprint={seed}"]
    if sys.platform == "darwin":
        return base + ["--fingerprint-platform=macos"]
    return base + ["--fingerprint-platform=windows"]


def build_args(stealth_args: bool,
               extra_args: list[str] | None,
               timezone: str | None = None,
               locale: str | None = None,
               headless: bool = True,
               extension_paths: list[str] | None = None,
               start_maximized: bool = False) -> list[str]:
    """Combine stealth args with user args; dedupe by flag key.

    Priority: stealth defaults < user args < dedicated timezone/locale params.
    """
    seen: dict[str, str] = {}
    if stealth_args:
        for arg in get_default_stealth_args():
            seen[arg.split("=", 1)[0]] = arg
    # Keep the GPU off the software-fallback path (SwiftShader is an instant
    # fingerprint); headed Linux also needs it for WebGL under Xvfb.
    if not headless or os.name == "nt":
        seen["--ignore-gpu-blocklist"] = "--ignore-gpu-blocklist"
    if extra_args:
        for arg in extra_args:
            seen[arg.split("=", 1)[0]] = arg
    if timezone:
        seen["--fingerprint-timezone"] = f"--fingerprint-timezone={timezone}"
    if locale:
        seen["--lang"] = f"--lang={locale}"
        seen["--fingerprint-locale"] = f"--fingerprint-locale={locale}"
    if extension_paths:
        ext_val = ",".join(os.path.abspath(p) for p in extension_paths)
        seen["--load-extension"] = f"--load-extension={ext_val}"
        seen["--disable-extensions-except"] = f"--disable-extensions-except={ext_val}"
    if start_maximized and not any(
            k in seen for k in ("--start-maximized", "--window-size", "--window-position")):
        seen["--start-maximized"] = "--start-maximized"
    return list(seen.values())


# ---------------------------------------------------------------------------
# GeoIP + WebRTC exit IP
# ---------------------------------------------------------------------------

def _geoip_http(proxy_url: str | None) -> tuple[str | None, str | None, str | None] | None:
    timeout = float(os.environ.get("CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS", 10))
    url = "http://ip-api.com/json/?fields=status,timezone,countryCode,query"
    try:
        if proxy_url:
            handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener()
        with opener.open(url, timeout=timeout) as r:
            data = json.load(r)
        if data.get("status") == "success":
            cc = (data.get("countryCode") or "").lower()
            return data.get("timezone"), cc or None, data.get("query")
    except Exception:
        pass
    return None


def _extract_proxy_url(proxy: str | ProxySettings | None) -> str | None:
    if not proxy:
        return None
    if isinstance(proxy, str):
        return proxy
    url = proxy["server"]
    if proxy.get("username") and proxy.get("password"):
        scheme, rest = url.split("://", 1) if "://" in url else ("http", url)
        url = f"{scheme}://{proxy['username']}:{proxy['password']}@{rest}"
    return url


def maybe_resolve_geoip(geoip: bool,
                        proxy: str | ProxySettings | None,
                        timezone: str | None,
                        locale: str | None,
                        args: list[str] | None = None) -> tuple[str | None, str | None, str | None]:
    """Auto-fill timezone/locale from the egress IP; returns (tz, locale, exit_ip).

    Explicit params (or raw flags in ``args``) always win over geoip results.
    """
    if not geoip:
        return timezone, locale, None
    if timezone is None and args:
        for a in args:
            if a.startswith("--fingerprint-timezone="):
                timezone = a.split("=", 1)[1]
    if locale is None and args:
        for a in args:
            if a.startswith(("--lang=", "--fingerprint-locale=")):
                locale = a.split("=", 1)[1]
    res = _geoip_http(_extract_proxy_url(proxy))
    if not res:
        sys.stderr.write("[tilion-fortress] geoip lookup failed; timezone/locale unchanged\n")
        return timezone, locale, None
    geo_tz, geo_locale, exit_ip = res
    if timezone is None:
        timezone = geo_tz
    if locale is None:
        locale = geo_locale
    return timezone, locale, exit_ip


def _resolve_webrtc_args(args: list[str] | None,
                         proxy: str | ProxySettings | None) -> list[str] | None:
    """Replace --fingerprint-webrtc-ip=auto with the resolved proxy exit IP."""
    if not args or "--fingerprint-webrtc-ip=auto" not in args:
        return args
    args = list(args)
    proxy_url = _extract_proxy_url(proxy)
    if not proxy_url:
        sys.stderr.write("[tilion-fortress] --fingerprint-webrtc-ip=auto requires a proxy; removing flag\n")
        args.remove("--fingerprint-webrtc-ip=auto")
        return args
    res = _geoip_http(proxy_url)
    exit_ip = res[2] if res else None
    if exit_ip:
        args[args.index("--fingerprint-webrtc-ip=auto")] = f"--fingerprint-webrtc-ip={exit_ip}"
    else:
        args.remove("--fingerprint-webrtc-ip=auto")
    return args


def _append_webrtc_exit_ip(args: list[str] | None, exit_ip: str | None) -> list[str] | None:
    if exit_ip and not (args and any(a.startswith("--fingerprint-webrtc-ip") for a in args)):
        args = list(args or [])
        args.append(f"--fingerprint-webrtc-ip={exit_ip}")
    return args


def _resolve_proxy_config(proxy: str | ProxySettings | None) -> tuple[dict, list[str]]:
    """Split a proxy into Playwright kwargs + extra CLI args."""
    if not proxy:
        return {}, []
    if isinstance(proxy, str):
        scheme, rest = (proxy.split("://", 1) + [""])[:2] if "://" in proxy else ("http", proxy)
        if "@" in rest:
            creds, host = rest.rsplit("@", 1)
            username, _, password = creds.partition(":")
            pw = {"server": f"{scheme}://{host}", "username": username}
            if password:
                pw["password"] = password
            return {"proxy": pw}, []
        return {"proxy": {"server": proxy}}, []
    pw = {"server": proxy["server"]}
    for k in ("bypass", "username", "password"):
        if proxy.get(k):
            pw[k] = proxy[k]
    return {"proxy": pw}, []


# ---------------------------------------------------------------------------
# Playwright launch family
# ---------------------------------------------------------------------------

_VIEWPORT_UNSET = object()
_ua_warned = False


def _prepare(headless, proxy, args, stealth_args, timezone, locale, geoip,
             extension_paths, start_maximized, browser_version=None,
             release_channel=None, widevine=False):
    binary = ensure_binary(browser_version=browser_version,
                           release_channel=release_channel)
    timezone, locale, exit_ip = maybe_resolve_geoip(geoip, proxy, timezone, locale, args)
    proxy_kwargs, proxy_extra = _resolve_proxy_config(proxy)
    args = _resolve_webrtc_args(args, proxy)
    args = _append_webrtc_exit_ip(args, exit_ip)
    if widevine and not (args and any(a.startswith("--uxr-widevine-cdm") for a in args)):
        from .widevine import ensure_widevine, widevine_flag
        cdm = ensure_widevine()
        if cdm:
            args = list(args or []) + [widevine_flag(cdm)]
        else:
            sys.stderr.write("[tilion-fortress] widevine requested but no CDM "
                             "source found; continuing without DRM\n")
    chrome_args = build_args(stealth_args, (args or []) + proxy_extra,
                             timezone=timezone, locale=locale, headless=headless,
                             extension_paths=extension_paths,
                             start_maximized=start_maximized)
    return binary, chrome_args, proxy_kwargs


def _patch_close(closeable, pw):
    orig = closeable.close

    def _close(*a, **kw):
        try:
            return orig(*a, **kw)
        finally:
            try:
                pw.stop()
            except Exception:
                pass
    closeable.close = _close


def _wrap_new_page(browser, humanize: bool, cfg_factory):
    """Wrap new_page/new_context so pages get humanized mouse/keyboard."""
    if not humanize:
        return
    from .humanize import patch_page
    orig_np, orig_nc = browser.new_page, browser.new_context

    def _human_ctx(ctx):
        orig_np2 = ctx.new_page

        def new_page(*a, **kw):
            page = orig_np2(*a, **kw)
            patch_page(page, cfg_factory())
            return page
        ctx.new_page = new_page
        return ctx

    def new_context(*a, **kw):
        return _human_ctx(orig_nc(*a, **kw))

    def new_page(*a, **kw):
        page = orig_np(*a, **kw)
        patch_page(page, cfg_factory())
        return page

    browser.new_context = new_context
    browser.new_page = new_page


def launch(headless: bool = True,
           proxy: str | ProxySettings | None = None,
           args: list[str] | None = None,
           stealth_args: bool = True,
           timezone: str | None = None,
           locale: str | None = None,
           geoip: bool = False,
           humanize: bool = False,
           human_preset: HumanPreset = "default",
           human_config: HumanConfigOverrides | None = None,
           extension_paths: list[str] | None = None,
           license_key: str | None = None,
           browser_version: str | None = None,
           release_channel: str | None = None,
           widevine: bool = False,
           _suppress_maximize: bool = False,
           **kwargs: Any) -> Any:
    """Launch Fortress and return a Playwright Browser (CloakBrowser-compatible).

    ``license_key`` is accepted for compatibility and ignored.
    ``widevine=True`` locates/fetches a Widevine CDM and activates EME.
    """
    from playwright.sync_api import sync_playwright

    binary, chrome_args, proxy_kwargs = _prepare(
        headless, proxy, args, stealth_args, timezone, locale, geoip,
        extension_paths, start_maximized=not _suppress_maximize,
        browser_version=browser_version, release_channel=release_channel,
        widevine=widevine)

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(
            executable_path=str(binary), headless=headless, args=chrome_args,
            ignore_default_args=["--enable-automation"], **proxy_kwargs, **kwargs)
    except Exception:
        pw.stop()
        raise
    _patch_close(browser, pw)
    _wrap_new_page(browser, humanize,
                   lambda: resolve_human_config(human_preset, human_config))
    return browser


async def launch_async(headless: bool = True,
                       proxy: str | ProxySettings | None = None,
                       args: list[str] | None = None,
                       stealth_args: bool = True,
                       timezone: str | None = None,
                       locale: str | None = None,
                       geoip: bool = False,
                       humanize: bool = False,
                       human_preset: HumanPreset = "default",
                       human_config: HumanConfigOverrides | None = None,
                       extension_paths: list[str] | None = None,
                       license_key: str | None = None,
                       browser_version: str | None = None,
                       release_channel: str | None = None,
                       widevine: bool = False,
                       **kwargs: Any) -> Any:
    """Async variant of launch(); returns an async Playwright Browser."""
    from playwright.async_api import async_playwright

    binary, chrome_args, proxy_kwargs = _prepare(
        headless, proxy, args, stealth_args, timezone, locale, geoip,
        extension_paths, start_maximized=True,
        browser_version=browser_version, release_channel=release_channel,
        widevine=widevine)
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(
            executable_path=str(binary), headless=headless, args=chrome_args,
            ignore_default_args=["--enable-automation"], **proxy_kwargs, **kwargs)
    except Exception:
        await pw.stop()
        raise
    browser_close_orig = browser.close

    async def _close(*a, **kw2):
        try:
            return await browser_close_orig(*a, **kw2)
        finally:
            await pw.stop()
    browser.close = _close
    if humanize:
        from .humanize import patch_page
        orig_np = browser.new_page

        async def new_page(*a, **kw3):
            page = await orig_np(*a, **kw3)
            patch_page(page, resolve_human_config(human_preset, human_config))
            return page
        browser.new_page = new_page
    return browser


def _split_context_kwargs(viewport, locale, color_scheme, user_agent, kwargs):
    """Assemble new_context() kwargs from dedicated params + **kwargs."""
    global _ua_warned
    if user_agent and not _ua_warned:
        _ua_warned = True
        sys.stderr.write("[tilion-fortress] warning: user_agent emulation desyncs "
                         "UA Client Hints; prefer the engine persona (--uxr-ua-*)\n")
    ctx_kwargs = dict(kwargs)
    if viewport is not _VIEWPORT_UNSET:
        ctx_kwargs["viewport"] = viewport
    elif ("viewport" not in ctx_kwargs and "no_viewport" not in ctx_kwargs):
        # Headless: fixed 1080p-maximized-Chrome viewport keeps outer==inner
        # coherent; headed callers pass viewport=None for the real window.
        ctx_kwargs.setdefault("viewport", DEFAULT_VIEWPORT)
    if locale:
        ctx_kwargs.setdefault("locale", locale)
    if color_scheme:
        ctx_kwargs.setdefault("color_scheme", color_scheme)
    if user_agent:
        ctx_kwargs["user_agent"] = user_agent
    return ctx_kwargs


def launch_context(headless: bool = True,
                   proxy: str | ProxySettings | None = None,
                   args: list[str] | None = None,
                   stealth_args: bool = True,
                   user_agent: str | None = None,
                   viewport: Any = _VIEWPORT_UNSET,
                   locale: str | None = None,
                   timezone: str | None = None,
                   color_scheme: str | None = None,
                   geoip: bool = False,
                   humanize: bool = False,
                   human_preset: HumanPreset = "default",
                   human_config: HumanConfigOverrides | None = None,
                   extension_paths: list[str] | None = None,
                   license_key: str | None = None,
                   browser_version: str | None = None,
                   release_channel: str | None = None,
                   widevine: bool = False,
                   **kwargs: Any) -> Any:
    """Launch Fortress and return a BrowserContext with common options pre-set.

    All **kwargs (viewport, geolocation, permissions, ...) go to
    ``browser.new_context()`` exactly as in CloakBrowser.
    """
    ctx_kwargs = _split_context_kwargs(viewport, locale, color_scheme, user_agent, kwargs)
    browser = launch(headless=headless, proxy=proxy, args=args, stealth_args=stealth_args,
                     timezone=timezone, locale=locale, geoip=geoip, humanize=humanize,
                     human_preset=human_preset, human_config=human_config,
                     extension_paths=extension_paths, license_key=license_key,
                     browser_version=browser_version, release_channel=release_channel,
                     widevine=widevine, _suppress_maximize=True)
    ctx = browser.new_context(**ctx_kwargs)
    orig_close = ctx.close

    def _close_ctx(*a, **kw):
        try:
            return orig_close(*a, **kw)
        finally:
            browser.close()
    ctx.close = _close_ctx
    return ctx


def launch_persistent_context(user_data_dir: str | os.PathLike,
                              headless: bool = True,
                              proxy: str | ProxySettings | None = None,
                              args: list[str] | None = None,
                              stealth_args: bool = True,
                              user_agent: str | None = None,
                              viewport: Any = _VIEWPORT_UNSET,
                              locale: str | None = None,
                              timezone: str | None = None,
                              color_scheme: str | None = None,
                              geoip: bool = False,
                              humanize: bool = False,
                              human_preset: HumanPreset = "default",
                              human_config: HumanConfigOverrides | None = None,
                              extension_paths: list[str] | None = None,
                              license_key: str | None = None,
                              browser_version: str | None = None,
                              release_channel: str | None = None,
                              widevine: bool = False,
                              **kwargs: Any) -> Any:
    """Launch with a persistent profile; returns a BrowserContext."""
    from playwright.sync_api import sync_playwright

    ctx_kwargs = _split_context_kwargs(viewport, locale, color_scheme, user_agent, kwargs)
    binary, chrome_args, proxy_kwargs = _prepare(
        headless, proxy, args, stealth_args, timezone, locale, geoip,
        extension_paths, start_maximized=False,
        browser_version=browser_version, release_channel=release_channel,
        widevine=widevine)
    pw = sync_playwright().start()
    try:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir), executable_path=str(binary),
            headless=headless, args=chrome_args,
            ignore_default_args=["--enable-automation"], **proxy_kwargs, **ctx_kwargs)
    except Exception:
        pw.stop()
        raise
    _patch_close(ctx, pw)
    if humanize:
        from .humanize import patch_page
        for page in ctx.pages:
            patch_page(page, resolve_human_config(human_preset, human_config))
        orig_np = ctx.new_page

        def new_page(*a, **kw):
            page = orig_np(*a, **kw)
            patch_page(page, resolve_human_config(human_preset, human_config))
            return page
        ctx.new_page = new_page
    return ctx


async def launch_context_async(**kw: Any) -> Any:
    """Async launch_context — same options, returns an async BrowserContext."""
    from playwright.async_api import async_playwright

    ctx_kwargs = _split_context_kwargs(kw.get("viewport", _VIEWPORT_UNSET),
                                       kw.get("locale"), kw.get("color_scheme"),
                                       kw.get("user_agent"),
                                       {k: v for k, v in kw.items() if k not in (
                                           "headless", "proxy", "args", "stealth_args",
                                           "user_agent", "viewport", "locale", "timezone",
                                           "color_scheme", "geoip", "humanize",
                                           "human_preset", "human_config",
                                           "extension_paths", "license_key",
                                           "browser_version", "release_channel", "widevine")})
    headless = kw.get("headless", True)
    binary, chrome_args, proxy_kwargs = _prepare(
        headless, kw.get("proxy"), kw.get("args"), kw.get("stealth_args", True),
        kw.get("timezone"), kw.get("locale"), kw.get("geoip", False),
        kw.get("extension_paths"), start_maximized=True,
        browser_version=kw.get("browser_version"), release_channel=kw.get("release_channel"),
        widevine=kw.get("widevine", False))
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(
            executable_path=str(binary), headless=headless, args=chrome_args,
            ignore_default_args=["--enable-automation"], **proxy_kwargs)
        ctx = await browser.new_context(**ctx_kwargs)
    except Exception:
        await pw.stop()
        raise

    async def _close(*a, **kw2):
        try:
            return await ctx_close_orig(*a, **kw2)
        finally:
            try:
                await browser.close()
            finally:
                await pw.stop()
    ctx_close_orig = ctx.close
    ctx.close = _close
    if kw.get("humanize"):
        from .humanize import patch_page
        orig_np = ctx.new_page

        async def new_page(*a, **kw3):
            page = await orig_np(*a, **kw3)
            patch_page(page, resolve_human_config(kw.get("human_preset", "default"),
                                                  kw.get("human_config")))
            return page
        ctx.new_page = new_page
    return ctx


async def launch_persistent_context_async(**kw: Any) -> Any:
    """Async launch_persistent_context — same options, async BrowserContext."""
    from playwright.async_api import async_playwright

    user_data_dir = kw.get("user_data_dir")
    ctx_kwargs = _split_context_kwargs(kw.get("viewport", _VIEWPORT_UNSET),
                                       kw.get("locale"), kw.get("color_scheme"),
                                       kw.get("user_agent"),
                                       {k: v for k, v in kw.items() if k not in (
                                           "user_data_dir", "headless", "proxy", "args",
                                           "stealth_args", "user_agent", "viewport",
                                           "locale", "timezone", "color_scheme", "geoip",
                                           "humanize", "human_preset", "human_config",
                                           "extension_paths", "license_key",
                                           "browser_version", "release_channel", "widevine")})
    headless = kw.get("headless", True)
    binary, chrome_args, proxy_kwargs = _prepare(
        headless, kw.get("proxy"), kw.get("args"), kw.get("stealth_args", True),
        kw.get("timezone"), kw.get("locale"), kw.get("geoip", False),
        kw.get("extension_paths"), start_maximized=False,
        browser_version=kw.get("browser_version"), release_channel=kw.get("release_channel"),
        widevine=kw.get("widevine", False))
    pw = await async_playwright().start()
    try:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir), executable_path=str(binary),
            headless=headless, args=chrome_args,
            ignore_default_args=["--enable-automation"], **proxy_kwargs, **ctx_kwargs)
    except Exception:
        await pw.stop()
        raise

    async def _close(*a, **kw2):
        try:
            return await ctx_close_orig(*a, **kw2)
        finally:
            await pw.stop()
    ctx_close_orig = ctx.close
    ctx.close = _close
    if kw.get("humanize"):
        from .humanize import patch_page
        for page in ctx.pages:
            patch_page(page, resolve_human_config(kw.get("human_preset", "default"),
                                                  kw.get("human_config")))
    return ctx
