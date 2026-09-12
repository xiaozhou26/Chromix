"""CloakBrowser-compatible API surface for chromix.

Mirrors the ``cloakbrowser`` Python wrapper (github.com/CloakHQ/CloakBrowser)
so existing CloakBrowser scripts run on Chromix by changing only the import:

    - from cloakbrowser import launch
    + from chromix import launch

Same function names, same keyword arguments, same return types (Playwright
Browser / BrowserContext objects), same env-var names (CLOAKBROWSER_*). The
engine underneath is Chromix: the ``--fingerprint-*`` flags this layer emits
are normalized to the engine's ``--uxr-*`` persona switches by patch 0036.

Intentional differences:
  - ``license_key`` is accepted and ignored (Chromix ships one open tier).
  - ``geoip`` uses ip-api.com over HTTP instead of a local GeoLite2 database;
    no extra dependency, same semantics (explicit timezone/locale win).
  - The humanize layer covers mouse / typing / scroll / click wrapping.
"""
from __future__ import annotations
import json
import os
import secrets
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, TypedDict

from ._binary import (
    _CACHE, _CHANNELS, _binary_path, _bundle_complete, _download, _host, resolve_platform,
)
from ._fonts import apply_font_env, font_dir_whitelist_arg
from ._persona import ensure_persona_geometry
from ._fingerprint import normalize_fingerprint_args
from ._network import (
    extract_proxy_url as _extract_proxy_url, geoip_http as _geoip_http,
    network_args as _network_args, split_proxy as _split_proxy,
    lookup_proxy as _lookup_proxy, resolve_webrtc_args as _resolve_webrtc_args,
)
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
    return _binary_path(plat, _CACHE / tag / plat)


def ensure_binary(license_key: str | None = None,
                  browser_version: str | None = None,
                  release_channel: str | None = None) -> Path:
    """Return the path to the Chromix chrome binary, downloading if needed.

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
            "No native Chromix binary for this platform (Linux x64/arm64, "
            "Windows x64, macOS x64/arm64); or point CLOAKBROWSER_BINARY_PATH at a local build.")
    ch = _channel_for(browser_version, release_channel)
    tag = _CHANNELS[ch]["tag"]
    chrome = _chrome_binary(plat, tag)
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
    installed = _bundle_complete(plat, _CACHE / tag / plat)
    return {
        "tier": "open-source",
        "version": tag.lstrip("v"),
        "channel": ch,
        "platform": plat,
        "path": str(chrome) if installed else None,
        "installed": installed,
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
            "https://api.github.com/repos/xiaozhou26/Chromix/releases/latest",
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
    """Stealth defaults with a random 32-bit seed and the native OS persona."""
    seed = secrets.randbits(32) or 1
    base = [f"--fingerprint={seed}"]
    platform = {"linux": "linux", "win32": "windows", "darwin": "macos"}.get(sys.platform)
    return base + ([f"--fingerprint-platform={platform}"] if platform else [])


_PROFILE_SEED_FILE = ".chromix-fingerprint-seed"


def _read_profile_seed(path: Path) -> int:
    data = path.read_bytes()
    try:
        seed = int(data)
        if not 1 <= seed <= 0xFFFFFFFF or data != f"{seed}\n".encode("ascii"):
            raise ValueError
    except ValueError:
        raise ValueError(f"Invalid Chromix profile seed file: {path}") from None
    return seed


def _profile_seed(user_data_dir: str | os.PathLike) -> int:
    profile = Path(user_data_dir)
    path = profile / _PROFILE_SEED_FILE
    try:
        return _read_profile_seed(path)
    except FileNotFoundError:
        pass
    profile.mkdir(parents=True, exist_ok=True)
    seed = secrets.randbits(32) or 1
    fd, temporary = tempfile.mkstemp(prefix=f"{_PROFILE_SEED_FILE}.", dir=profile)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(f"{seed}\n".encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        # Publish a complete file without replacing a concurrent winner.
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        return _read_profile_seed(path)
    finally:
        os.unlink(temporary)


def _persistent_args(user_data_dir, stealth_args, args):
    if not user_data_dir or not os.fspath(user_data_dir):
        raise ValueError("launch_persistent_context requires user_data_dir")
    if not stealth_args or any(a.split("=", 1)[0] == "--fingerprint" for a in args or []):
        return args
    return list(args or []) + [f"--fingerprint={_profile_seed(user_data_dir)}"]


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
    extra_args = _network_args(extra_args)
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
    return normalize_fingerprint_args(list(seen.values()))


# ---------------------------------------------------------------------------
# GeoIP metadata and WebRTC presentation-address resolution
# ---------------------------------------------------------------------------


def maybe_resolve_geoip(geoip: bool,
                        proxy: str | ProxySettings | None,
                        timezone: str | None,
                        locale: str | None,
                        args: list[str] | None = None) -> tuple[str | None, str | None, str | None]:
    """Auto-fill timezone/locale from the egress IP; returns (tz, locale, exit_ip).

    Explicit params (or raw flags in ``args``) always win over geoip results.
    Explicit proxy ignores environment/bypass settings; absent proxy means direct.
    Lookup errors raise ValueError; the returned IP does not control routing.
    """
    if not geoip:
        return timezone, locale, None
    if timezone is None and args:
        for a in args:
            if a.startswith(("--fingerprint-timezone=", "--uxr-timezone=")):
                timezone = a.split("=", 1)[1]
    if locale is None and args:
        for a in args:
            if a.startswith(("--lang=", "--fingerprint-locale=", "--uxr-locale=")):
                locale = a.split("=", 1)[1]
    res = _geoip_http(_extract_proxy_url(proxy))
    if not res:
        raise ValueError("GeoIP lookup failed; no direct fallback")
    geo_tz, geo_locale, exit_ip = res
    if timezone is None:
        timezone = geo_tz
    if locale is None:
        locale = geo_locale
    return timezone, locale, exit_ip


def _resolve_proxy_config(proxy: str | ProxySettings | None) -> tuple[dict, list[str]]:
    """Normalize URL credentials into Playwright's separate auth fields."""
    config = _split_proxy(proxy)
    return ({"proxy": config} if config else {}), []


# ---------------------------------------------------------------------------
# Playwright launch family
# ---------------------------------------------------------------------------

_VIEWPORT_UNSET = object()
_ua_warned = False


def _prepare(headless, proxy, args, stealth_args, timezone, locale, geoip,
             extension_paths, start_maximized, browser_version=None,
             release_channel=None, fonts_dir=None):
    args = _network_args(args, proxy)
    proxy_kwargs, proxy_extra = _resolve_proxy_config(proxy)
    lookup_proxy = _lookup_proxy(args, proxy) if geoip else proxy
    timezone, locale, exit_ip = maybe_resolve_geoip(geoip, lookup_proxy, timezone, locale, args)
    args = _resolve_webrtc_args(args, lookup_proxy, exit_ip=exit_ip, geoip=geoip,
                               lookup=_geoip_http)
    binary = ensure_binary(browser_version=browser_version,
                           release_channel=release_channel)
    # Widevine / DRM: enabled automatically when a CDM is present (same policy
    # as CloakBrowser). Opt out with CLOAKBROWSER_WIDEVINE=0.
    if os.environ.get("CLOAKBROWSER_WIDEVINE", "1") not in ("0", "false", "False") and \
            not (args and any(a.startswith("--uxr-widevine-cdm") for a in args)):
        from .widevine import find_cdm, widevine_flag
        cdm = find_cdm()
        if cdm:
            args = list(args or []) + [widevine_flag(cdm)]
    if fonts_dir:
        whitelist = font_dir_whitelist_arg(fonts_dir)
        if whitelist:
            args = [whitelist] + list(args or [])
        else:
            sys.stderr.write(f"[chromix] fonts_dir={fonts_dir}: no parseable fonts found\n")
    chrome_args = build_args(stealth_args, (args or []) + proxy_extra,
                             timezone=timezone, locale=locale, headless=headless,
                             extension_paths=extension_paths,
                             start_maximized=start_maximized)
    geometry = None
    if (stealth_args and "--fingerprint=off" not in chrome_args
            and "--uxr-synthetic-device-tests=true" in chrome_args):
        chrome_args, geometry = ensure_persona_geometry(chrome_args)
        if not headless and not any(a.split("=", 1)[0] == "--window-size" for a in chrome_args):
            chrome_args = [a for a in chrome_args if a != "--start-maximized"]
            chrome_args.append(f"--window-size={geometry['outer_width']},{geometry['outer_height']}")
    return binary, chrome_args, proxy_kwargs, geometry


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


def _wrap_geometry(browser, geometry, headless, asynchronous=False):
    browser._chromix_geometry = geometry

    def wrap(original):
        if asynchronous:
            async def call(*args, **kwargs):
                kwargs = _split_context_kwargs(_VIEWPORT_UNSET, None, None, None,
                                                kwargs, geometry=geometry, headless=headless)
                return await original(*args, **kwargs)
        else:
            def call(*args, **kwargs):
                kwargs = _split_context_kwargs(_VIEWPORT_UNSET, None, None, None,
                                                kwargs, geometry=geometry, headless=headless)
                return original(*args, **kwargs)
        return call

    for name in ("new_page", "new_context"):
        if hasattr(browser, name):
            setattr(browser, name, wrap(getattr(browser, name)))


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
           _suppress_maximize: bool = False,
           **kwargs: Any) -> Any:
    """Launch Chromix and return a Playwright Browser (CloakBrowser-compatible).

    ``license_key`` is accepted for compatibility and ignored.
    """
    from playwright.sync_api import sync_playwright

    if "device_pool" in kwargs:
        raise ValueError("device_pool requires launch_context or launch_persistent_context")
    fonts_dir = kwargs.pop("fonts_dir", None)
    binary, chrome_args, proxy_kwargs, geometry = _prepare(
        headless, proxy, args, stealth_args, timezone, locale, geoip,
        extension_paths, start_maximized=not _suppress_maximize,
        browser_version=browser_version, release_channel=release_channel, fonts_dir=fonts_dir)
    apply_font_env(binary, kwargs, fonts_dir=fonts_dir)

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(
            executable_path=str(binary), headless=headless, args=chrome_args,
            ignore_default_args=["--enable-automation"], **proxy_kwargs, **kwargs)
    except Exception:
        pw.stop()
        raise
    _patch_close(browser, pw)
    _wrap_geometry(browser, geometry, headless)
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
                       **kwargs: Any) -> Any:
    """Async variant of launch(); returns an async Playwright Browser."""
    from playwright.async_api import async_playwright

    if "device_pool" in kwargs:
        raise ValueError("device_pool requires launch_context_async or launch_persistent_context_async")
    fonts_dir = kwargs.pop("fonts_dir", None)
    binary, chrome_args, proxy_kwargs, geometry = _prepare(
        headless, proxy, args, stealth_args, timezone, locale, geoip,
        extension_paths, start_maximized=True,
        browser_version=browser_version, release_channel=release_channel, fonts_dir=fonts_dir)
    apply_font_env(binary, kwargs, fonts_dir=fonts_dir)
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
    _wrap_geometry(browser, geometry, headless, asynchronous=True)
    if humanize:
        from .humanize import patch_page
        orig_np = browser.new_page

        async def new_page(*a, **kw3):
            page = await orig_np(*a, **kw3)
            patch_page(page, resolve_human_config(human_preset, human_config))
            return page
        browser.new_page = new_page
    return browser


def _split_context_kwargs(viewport, locale, color_scheme, user_agent, kwargs,
                          geometry=None, headless=True):
    """Assemble new_context() kwargs from dedicated params + **kwargs."""
    global _ua_warned
    if user_agent and not _ua_warned:
        _ua_warned = True
        sys.stderr.write("[chromix] warning: user_agent emulation desyncs "
                         "UA Client Hints; prefer the engine persona (--uxr-ua-*)\n")
    ctx_kwargs = dict(kwargs)
    if viewport is not _VIEWPORT_UNSET:
        ctx_kwargs["viewport"] = viewport
    elif ("viewport" not in ctx_kwargs and "no_viewport" not in ctx_kwargs):
        ctx_kwargs["viewport"] = (
            {"width": geometry.get("viewport_width", geometry.get("outer_width", geometry["width"])),
             "height": geometry.get("viewport_height", geometry["inner_height"])} if geometry else None
        ) if headless else None
    if "viewport" in ctx_kwargs and ctx_kwargs["viewport"] is None:
        ctx_kwargs.setdefault("no_viewport", True)
    if (ctx_kwargs.get("viewport") is not None and not ctx_kwargs.get("no_viewport")
            and geometry):
        # Playwright forwards these together to native device emulation. Without
        # screen, it substitutes viewport dimensions and overrides launch geometry.
        ctx_kwargs.setdefault("screen", {"width": geometry["width"], "height": geometry["height"]})
        ctx_kwargs.setdefault("device_scale_factor", geometry["dpr"])
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
                   **kwargs: Any) -> Any:
    """Launch Chromix and return a BrowserContext with common options pre-set.

    All **kwargs (viewport, geolocation, permissions, ...) go to
    ``browser.new_context()`` exactly as in CloakBrowser.
    """
    fonts_dir = kwargs.pop("fonts_dir", None)
    browser_kwargs = {"env": kwargs.pop("env")} if "env" in kwargs else {}
    browser = launch(headless=headless, proxy=proxy, args=args, stealth_args=stealth_args,
                     timezone=timezone, locale=locale, geoip=geoip, humanize=humanize,
                     human_preset=human_preset, human_config=human_config,
                     extension_paths=extension_paths, license_key=license_key,
                     browser_version=browser_version, release_channel=release_channel,
                     _suppress_maximize=True, fonts_dir=fonts_dir, **browser_kwargs)
    ctx_kwargs = _split_context_kwargs(viewport, locale, color_scheme, user_agent, kwargs,
                                       geometry=browser._chromix_geometry, headless=headless)
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
                              **kwargs: Any) -> Any:
    """Launch with a persistent profile and seed; returns a BrowserContext."""
    from playwright.sync_api import sync_playwright

    args = _persistent_args(user_data_dir, stealth_args, args)
    fonts_dir = kwargs.pop("fonts_dir", None)
    binary, chrome_args, proxy_kwargs, geometry = _prepare(
        headless, proxy, args, stealth_args, timezone, locale, geoip,
        extension_paths, start_maximized=False,
        browser_version=browser_version, release_channel=release_channel, fonts_dir=fonts_dir)
    ctx_kwargs = _split_context_kwargs(viewport, locale, color_scheme, user_agent, kwargs,
                                       geometry=geometry, headless=headless)
    launch_kwargs = {"env": ctx_kwargs.pop("env")} if "env" in ctx_kwargs else {}
    apply_font_env(binary, launch_kwargs, fonts_dir=fonts_dir)
    pw = sync_playwright().start()
    try:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir), executable_path=str(binary),
            headless=headless, args=chrome_args,
            ignore_default_args=["--enable-automation"], **proxy_kwargs,
            **launch_kwargs, **ctx_kwargs)
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

    headless = kw.get("headless", True)
    fonts_dir = kw.get("fonts_dir")
    binary, chrome_args, proxy_kwargs, geometry = _prepare(
        headless, kw.get("proxy"), kw.get("args"), kw.get("stealth_args", True),
        kw.get("timezone"), kw.get("locale"), kw.get("geoip", False),
        kw.get("extension_paths"), start_maximized=True,
        browser_version=kw.get("browser_version"), release_channel=kw.get("release_channel"),
        fonts_dir=fonts_dir)
    ctx_kwargs = _split_context_kwargs(kw.get("viewport", _VIEWPORT_UNSET),
                                       kw.get("locale"), kw.get("color_scheme"),
                                       kw.get("user_agent"),
                                       {k: v for k, v in kw.items() if k not in (
                                           "headless", "proxy", "args", "stealth_args",
                                           "user_agent", "viewport", "locale", "timezone",
                                           "color_scheme", "geoip", "humanize",
                                           "human_preset", "human_config",
                                           "extension_paths", "license_key", "fonts_dir",
                                           "browser_version", "release_channel")},
                                       geometry=geometry, headless=headless)
    launch_kwargs = dict(proxy_kwargs)
    if "env" in ctx_kwargs:
        launch_kwargs["env"] = ctx_kwargs.pop("env")
    apply_font_env(binary, launch_kwargs, fonts_dir=fonts_dir)
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(
            executable_path=str(binary), headless=headless, args=chrome_args,
            ignore_default_args=["--enable-automation"], **launch_kwargs)
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
    args = _persistent_args(user_data_dir, kw.get("stealth_args", True), kw.get("args"))
    headless = kw.get("headless", True)
    fonts_dir = kw.get("fonts_dir")
    binary, chrome_args, proxy_kwargs, geometry = _prepare(
        headless, kw.get("proxy"), args, kw.get("stealth_args", True),
        kw.get("timezone"), kw.get("locale"), kw.get("geoip", False),
        kw.get("extension_paths"), start_maximized=False,
        browser_version=kw.get("browser_version"), release_channel=kw.get("release_channel"),
        fonts_dir=fonts_dir)
    ctx_kwargs = _split_context_kwargs(kw.get("viewport", _VIEWPORT_UNSET),
                                       kw.get("locale"), kw.get("color_scheme"),
                                       kw.get("user_agent"),
                                       {k: v for k, v in kw.items() if k not in (
                                           "user_data_dir", "headless", "proxy", "args",
                                           "stealth_args", "user_agent", "viewport",
                                           "locale", "timezone", "color_scheme", "geoip",
                                           "humanize", "human_preset", "human_config",
                                           "extension_paths", "license_key", "fonts_dir",
                                           "browser_version", "release_channel")},
                                       geometry=geometry, headless=headless)
    launch_kwargs = {"env": ctx_kwargs.pop("env")} if "env" in ctx_kwargs else {}
    apply_font_env(binary, launch_kwargs, fonts_dir=fonts_dir)
    pw = await async_playwright().start()
    try:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir), executable_path=str(binary),
            headless=headless, args=chrome_args,
            ignore_default_args=["--enable-automation"], **proxy_kwargs,
            **launch_kwargs, **ctx_kwargs)
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


def _measured_context_entry(original, asynchronous=False):
    from functools import wraps
    import inspect
    signature = inspect.signature(original)

    def dispatch(args, kwargs):
        options = kwargs.pop("device_pool")
        bound = signature.bind(*args, **kwargs)
        supplied = dict(bound.arguments)
        supplied.update(supplied.pop("kwargs", {}))
        supplied.update(supplied.pop("kw", {}))
        if "persistent" in original.__name__ and not supplied.get("user_data_dir"):
            raise ValueError("measured persistent launch requires user_data_dir")
        from ._device_launch import launch_measured
        return launch_measured(options, asynchronous=asynchronous, **supplied)

    if asynchronous:
        @wraps(original)
        async def wrapped(*args, **kwargs):
            if "device_pool" in kwargs:
                return await dispatch(args, kwargs)
            return await original(*args, **kwargs)
    else:
        @wraps(original)
        def wrapped(*args, **kwargs):
            if "device_pool" in kwargs:
                return dispatch(args, kwargs)
            return original(*args, **kwargs)
    return wrapped


launch_context = _measured_context_entry(launch_context)
launch_persistent_context = _measured_context_entry(launch_persistent_context)
launch_context_async = _measured_context_entry(launch_context_async, asynchronous=True)
launch_persistent_context_async = _measured_context_entry(launch_persistent_context_async, asynchronous=True)
