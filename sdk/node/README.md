# @xiaoxiaofeihh/chromix

Drive the Chromix Chromium engine with a **CloakBrowser-compatible API**.
Function names, option names (camelCase), and return types (Playwright
`Browser` / `BrowserContext` via `playwright-core`) match the
[`cloakbrowser`](https://github.com/CloakHQ/CloakBrowser) wrapper, so existing
CloakBrowser scripts can migrate by changing the import:

```diff
- import { launch } from 'cloakbrowser';
+ import { launch } from '@xiaoxiaofeihh/chromix';
```

```javascript
import { launch } from '@xiaoxiaofeihh/chromix';

const browser = await launch({
  proxy: 'http://user:pass@residential-proxy:port',
  geoip: true,       // match timezone + locale to proxy IP
  headless: false,
  humanize: true,    // human-like mouse, keyboard, scroll
});
const page = await browser.newPage();
await page.goto('https://example.com');
await browser.close();
```

Convenience wrappers:

```javascript
import {
  launchContext,
  launchPersistentContext,
} from '@xiaoxiaofeihh/chromix';

const context = await launchContext({
  userAgent: 'Custom UA',
  viewport: { width: 1920, height: 1080 },
});
const persistentContext = await launchPersistentContext({
  userDataDir: './chrome-profile',
  headless: false,
});
```

## Install

```bash
npm install @xiaoxiaofeihh/chromix playwright-core
```

The unscoped npm name `chromix` belongs to an unrelated project. This SDK is
published under the `@xiaoxiaofeihh` scope; use the full scoped name when
installing or importing it.

The SDK uses the lightweight `yauzl` ZIP reader and loads an installed
`playwright-core` or `playwright` package at launch time. On first launch, the
Chromix binary is downloaded from this repository's GitHub Release,
SHA256-verified when the release manifest is available, and cached under
`~/.cache/chromix`. Point `CLOAKBROWSER_BINARY_PATH` at a local build to skip
the download.

## Options

CloakBrowser options work unchanged: `headless, proxy, args, stealthArgs,
timezone, locale, geoip, humanize, humanPreset, humanConfig, userAgent,
viewport, colorScheme, extensionPaths, browserVersion, releaseChannel,
licenseKey, contextOptions, launchOptions, userDataDir` (+ `startMaximized`).

Persistent contexts create `.chromix-fingerprint-seed` inside `userDataDir` on
first stealth launch and reuse it thereafter. The file is one decimal 32-bit
seed followed by a newline, uses the same format as the Python SDK, and is
published atomically for concurrent first launches. An explicit
`--fingerprint=...` in `args`, `launchOptions.args`, or `contextOptions.args`
wins without creating or rewriting the file; `stealthArgs: false` also skips
seed I/O. Defaults claim the native persona: `linux`, `windows`, or `macos`.

Default page viewport geometry is native. Public fingerprint mode supplies
CPU/RAM 8/8, platform-specific screen/taskbar defaults and a 102400 MiB quota.
The older seeded synthetic viewport/hardware pools require explicit
`args: ['--uxr-synthetic-device-tests=true']` and remain separate test templates.

Explicit synthetic seeds accept nonzero decimal uint64 strings without rounding
through JavaScript `Number`; Python and Node derive identical geometry. Malformed
seeds, conflicting screen/taskbar aliases, invalid work areas and incomplete
viewport pairs fail. `--uxr-viewport-width`/`--uxr-viewport-height` override the
UI-strip template. A configured viewport sends screen and DPR together, including
DPR 1; `viewport: null` removes inherited screen/DPR defaults. The
[launch display backend](../../docs/persona-cross-process-design.md) needs a
browser rebuilt from the current patch stack.

### Public fingerprint flags

GPU vendor/renderer, CPU/RAM, screen/taskbar, brand/version/platform version,
timezone/locale, quota, Windows font metrics, WebRTC IP/auto, noise/off,
third-party cookies, Windows voice tables and `FakeShadowRoot` are available
through `args`. See the [complete flag contract](../../docs/fingerprint-flags.md)
for defaults and limitations. Updating this SDK does not add native features
to an old executable; use a browser rebuilt from the matching patch stack.

```javascript
const browser = await launch({ args: [
  '--fingerprint=42',
  '--fingerprint-brand=Edge',
  '--fingerprint-brand-version=152.0.0.0',
  '--fingerprint-noise=false',
  '--fingerprint-allow-3p-cookies',
  '--enable-blink-features=FakeShadowRoot',
] });
```

`--fingerprint=off` accepts `false/0/disable/disabled` and strips the injected
platform. Explicit timezone/locale and `geoip: true` still apply regional
settings; omit them for a native-persona comparison. `noise=false` keeps seeds
while disabling existing perturbations; it does not install four independent
Canvas/WebGL/audio/client-rect noise implementations.

## Measured device launch

`launchContext({devicePool: {python: 'python', host: 'record.json',
records: ['record.json'], seed: '42'}})` validates entire evidence bundles and
the native host, then verifies five live contexts before returning. The persistent
variant uses `launchPersistentContext` with `userDataDir` and binds record/seed.
Install the matching Python SDK into the selected interpreter first:
`python -m pip install ./sdk/python` from this checkout. Set
`CLOAKBROWSER_BINARY_PATH` to the exact collected executable. Evidence defaults to
a 24-hour age limit. Other field/launch/context overrides are rejected;
browser-returning `launch` does not support measured mode. See
[device pool documentation](../../docs/device-pool.md) for the full contract.

Environment variables: `CLOAKBROWSER_BINARY_PATH`, `CLOAKBROWSER_VERSION`,
`CLOAKBROWSER_RELEASE_CHANNEL`, `CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS`,
`CLOAKBROWSER_WIDEVINE_CDM` / `CLOAKBROWSER_WIDEVINE=0` (DRM), and
`CHROMIX_CACHE_DIR` / `CHROMIX_DOWNLOAD_HOST` (cache / release host override).

## Intentional differences from CloakBrowser

1. `licenseKey` is accepted and ignored (one open tier).
2. `geoip` queries ip-api.com instead of a local GeoLite2 database.
3. No `cloakbrowser/puppeteer` subpath is provided; use the Playwright surface.
4. Widevine/DRM is enabled automatically when a CDM is present (installed
   Chrome or `CLOAKBROWSER_WIDEVINE_CDM`); on Linux, fetch one with
   `python -m chromix widevine`.

High-risk engine ports are available only through explicit `args`:

```javascript
const browser = await launch({ args: [
  '--fingerprint-devtools-runtime-suppression',
  '--fingerprint-canvas-bridge=127.0.0.1:9228',
  '--fingerprint-canvas-bridge-unsafe',
] });
```

Runtime suppression can break console/binding-based automation. Canvas Bridge
removes the sandbox from bridge renderer processes and forwards canvas/WebGL
operations to the configured endpoint.

### Proxy and GeoIP behavior

GeoIP is metadata, not a routing mechanism. The lookup uses the effective
HTTP/HTTPS/SOCKS proxy, including `launchOptions.proxy` overrides, and does not
inherit environment proxies or `NO_PROXY` bypasses. Failed lookups do not
fall back to the host connection. Metadata transport supports SOCKS4/4a/5/5h
and SOCKS5 credentials. This does not add SOCKS authentication or every URL
alias to Chromium/Playwright's browser proxy backend. SOCKS5/4a use remote
destination DNS; SOCKS4 uses local IPv4 DNS. Lookup accepts one raw
`--proxy-server` route, not PAC/auto-detect, route lists, empty raw proxies,
raw proxy credentials or a proxy conflicting with `--no-proxy-server`.
Simultaneous raw/Playwright proxy endpoints must match; omitted default ports
and equivalent IPv6 spellings are normalized. Supply credentials in the high-level option.

With a proxy, the SDK defaults to the native
`--force-webrtc-ip-handling-policy=disable_non_proxied_udp` unless an explicit
native policy was supplied. This is a WebRTC policy, not a guarantee about
all DNS, HTTP, QUIC or operating-system traffic.

`--fingerprint-webrtc-ip=<IPv4|IPv6|auto>` is supported. Auto resolves before
launch through the effective proxy. `geoip: true` reuses its one lookup to inject
the exit IP unless an explicit IP wins; off mode skips IP injection. The browser
rewrites local candidate/SDP/stats presentation, not sockets or STUN success.
Remote addresses, zero placeholders and relay allocations remain native.
`webrtc-fake-srflx` and `webrtc-fake-srflx-allow-udp` (including `uxr` equivalents)
remain rejected. The HTTP metadata service is not independent proof of an exit
route. Bare-browser auto has a separate bounded HTTPS startup resolver; see
the [full resolution contract](../../docs/fingerprint-flags.md#webrtc-ip-and-proxy-resolution).

GeoIP lookup failures now reject with `Error`. The timeout defaults to 10
seconds and accepts values greater than zero and at most 60. Creating a
later context with another proxy does not recompute browser-level locale
or timezone/IP.

## CLI

After installation, the package provides the `chromix` executable:

```bash
npx chromix --version
npx chromix install       # pre-download the binary
npx chromix info          # binary / cache info
npx chromix clear-cache
```

Run the registry package without installing it first:

```bash
npx @xiaoxiaofeihh/chromix --version
```

## Versioning

The npm package follows SemVer independently of Chromium's four-part version.
The bundled SDK currently targets Chromium source `152.0.7977.82`; the actual
binary release selected by `stable` or `latest` is shown by `chromix info`.

## License

The Node SDK is available under the BSD 3-Clause License. See [`LICENSE`](LICENSE).
