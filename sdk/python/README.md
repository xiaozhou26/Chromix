# chromix (Python)

Drive the Chromix stealth Chromium engine with a **CloakBrowser-compatible API** —
function names, keyword arguments, return types (Playwright `Browser` / `BrowserContext`)
and `CLOAKBROWSER_*` env-var names all match the [`cloakbrowser`](https://github.com/CloakHQ/CloakBrowser)
wrapper, so existing CloakBrowser scripts run on Chromix by changing only the import:

```diff
- from cloakbrowser import launch
+ from chromix import launch
```

```python
from chromix import launch

browser = launch(proxy="http://user:pass@proxy:8080", geoip=True, humanize=True)
page = browser.new_page()
page.goto("https://example.com")
browser.close()
```

## Install

```bash
pip install ./sdk/python playwright
```

On first launch the stealth Chromium binary is downloaded from this repo's GitHub
Release, SHA256-verified, and cached under `~/.cache/chromix`. Point
`CLOAKBROWSER_BINARY_PATH` at a local build (e.g. your own `chrome.exe`) to skip
the download.

## API

| Function | Description |
|---|---|
| `launch(**opts)` | Returns a Playwright `Browser` |
| `launch_async(**opts)` | Async variant |
| `launch_context(**opts)` | Returns a `BrowserContext` (viewport/locale/color_scheme pre-set) |
| `launch_context_async(**opts)` | Async variant |
| `launch_persistent_context(user_data_dir, **opts)` | Persistent profile |
| `launch_persistent_context_async(user_data_dir, **opts)` | Async variant |
| `build_args` / `get_default_stealth_args` | Arg assembly (32-bit random seed + platform claim) |
| `maybe_resolve_geoip(geoip, proxy, tz, locale, args)` | Egress IP → (tz, locale, exit_ip) |
| `ensure_binary` / `clear_cache` / `binary_info` / `check_for_update` | Binary management |
| `HumanConfig` / `resolve_human_config` | Behavioral-layer config (`default` / `careful` presets) |
| `ProxySettings` | Playwright-shaped proxy TypedDict |

Options (`headless, proxy, args, stealth_args, timezone, locale, geoip, humanize,
human_preset, human_config, extension_paths, license_key, browser_version,
release_channel, user_agent, viewport, color_scheme`) match CloakBrowser
name-for-name; `**kwargs` passes through to `playwright.chromium.launch()` /
`browser.new_context()`.

## Env vars

- `CLOAKBROWSER_BINARY_PATH` — use a local chrome binary instead of downloading
- `CLOAKBROWSER_VERSION` / `CLOAKBROWSER_RELEASE_CHANNEL` — pin a version/channel
- `CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS` — geoip lookup timeout
- `CLOAKBROWSER_WIDEVINE_CDM` — explicit Widevine CDM dir (DRM); `CLOAKBROWSER_WIDEVINE=0` disables DRM
- `CHROMIX_CACHE_DIR` / `CHROMIX_DOWNLOAD_HOST` — cache location / release host override

High-risk engine ports are available only through explicit browser `args`:

```python
browser = launch(args=[
    "--fingerprint-devtools-runtime-suppression",
    "--fingerprint-canvas-bridge=127.0.0.1:9228",
    "--fingerprint-canvas-bridge-unsafe",
    "--fingerprint-webrtc-fake-srflx=203.0.113.20",
    "--fingerprint-webrtc-fake-srflx-allow-udp",
])
```

Runtime suppression can break console/binding-based automation. Canvas Bridge
removes the sandbox from bridge renderer processes and forwards canvas/WebGL
operations to the configured endpoint. Fake srflx does not enable non-proxied
UDP unless the separate `allow-udp` flag is supplied.

## CLI

```bash
python -m chromix install      # pre-download the binary
python -m chromix info         # binary / cache info
python -m chromix widevine     # fetch the Widevine CDM (Linux x64)
python -m chromix clear-cache
```

## Intentional differences from CloakBrowser

1. `license_key` is accepted and ignored (one open tier).
2. `geoip` queries ip-api.com over HTTP instead of a local GeoLite2 database;
   explicit `timezone=` / `locale=` always win.
3. No `cloakbrowser/puppeteer` subpath — use the Playwright surface.
4. Widevine is enabled automatically when a CDM is present (installed Chrome,
   `CLOAKBROWSER_WIDEVINE_CDM`, or `python -m chromix widevine`).
