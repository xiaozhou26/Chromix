# chromix (Node)

Drive the Chromix stealth Chromium engine with a **CloakBrowser-compatible API** —
function names, option names (camelCase), and return types (Playwright
`Browser` / `BrowserContext` via `playwright-core`) all match the
[`cloakbrowser`](https://github.com/CloakHQ/CloakBrowser) wrapper, so existing
CloakBrowser scripts run on Chromix by changing only the import:

```diff
- import { launch } from 'cloakbrowser';
+ import { launch } from 'chromix';
```

```javascript
import { launch } from 'chromix';

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
import { launchContext, launchPersistentContext } from 'chromix';

const context = await launchContext({ userAgent: 'Custom UA', viewport: { width: 1920, height: 1080 } });
const ctx = await launchPersistentContext({ userDataDir: './chrome-profile', headless: false });
```

## Install

```bash
npm install chromix playwright-core
```

Requires `playwright-core` (or `playwright`) as a peer — the SDK itself has zero
dependencies. On first launch the stealth Chromium binary is downloaded from this
repo's GitHub Release, SHA256-verified, and cached under `~/.cache/chromix`.
Point `CLOAKBROWSER_BINARY_PATH` at a local build to skip the download.

## Options

All CloakBrowser options work unchanged: `headless, proxy, args, stealthArgs,
timezone, locale, geoip, humanize, humanPreset, humanConfig, userAgent, viewport,
colorScheme, extensionPaths, browserVersion, releaseChannel, licenseKey,
contextOptions, launchOptions, userDataDir` (+ `startMaximized`).

Env vars: `CLOAKBROWSER_BINARY_PATH`, `CLOAKBROWSER_VERSION`,
`CLOAKBROWSER_RELEASE_CHANNEL`, `CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS`,
`CLOAKBROWSER_WIDEVINE_CDM` / `CLOAKBROWSER_WIDEVINE=0` (DRM),
`CHROMIX_CACHE_DIR` / `CHROMIX_DOWNLOAD_HOST` (cache / release host override).

## Intentional differences from CloakBrowser

1. `licenseKey` is accepted and ignored (one open tier).
2. `geoip` queries ip-api.com instead of a local GeoLite2 database.
3. No `cloakbrowser/puppeteer` subpath — use the Playwright surface.
4. Widevine/DRM is enabled automatically when a CDM is present (installed
   Chrome or `CLOAKBROWSER_WIDEVINE_CDM`); on Linux fetch one with
   `python -m chromix widevine`.

## CLI

```bash
npx chromix --version
npx chromix install       # pre-download the binary
npx chromix info          # binary / cache info
npx chromix clear-cache
```
