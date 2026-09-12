# Chromix

English | [简体中文](readme_cn.md)

[![Windows x64 build](https://github.com/xiaozhou26/Chromix/actions/workflows/build-win-x64-github.yml/badge.svg)](https://github.com/xiaozhou26/Chromix/actions/workflows/build-win-x64-github.yml)
[![Linux x64 build](https://github.com/xiaozhou26/Chromix/actions/workflows/build-linux-x64.yml/badge.svg)](https://github.com/xiaozhou26/Chromix/actions/workflows/build-linux-x64.yml)
[![Linux ARM64 build](https://github.com/xiaozhou26/Chromix/actions/workflows/build-linux-arm64.yml/badge.svg)](https://github.com/xiaozhou26/Chromix/actions/workflows/build-linux-arm64.yml)
[![macOS Intel build](https://github.com/xiaozhou26/Chromix/actions/workflows/build-macos-x64.yml/badge.svg)](https://github.com/xiaozhou26/Chromix/actions/workflows/build-macos-x64.yml)
[![macOS ARM build](https://github.com/xiaozhou26/Chromix/actions/workflows/build-macos-arm64.yml/badge.svg)](https://github.com/xiaozhou26/Chromix/actions/workflows/build-macos-arm64.yml)
[![GitHub release](https://img.shields.io/github/v/release/xiaozhou26/Chromix?display_name=tag)](https://github.com/xiaozhou26/Chromix/releases)

Chromix is a Chromium-based browser build focused on presenting a coherent,
per-launch browser persona across JavaScript-visible surfaces. It is built on
pinned `ungoogled-chromium` sources and the matching Windows/Linux/macOS platform
layer, then adds a reviewed Chromium 152 patch series and lightweight Python and
Node SDKs. Five independent workflows build Windows x64, Linux x64/arm64,
and macOS x64/arm64. Each platform publishes independently after its build,
checksum, extraction, version, headless smoke and fingerprint regression gates succeed. Successful
platforms append to the same Chromium-version release tag; they need not share
a source commit or wait for other platforms.

> Chromix is intended for browser automation, compatibility testing, privacy
> research, and controlled fingerprinting experiments. A custom browser does
> not make automation undetectable; network reputation, behavior, account
> history, and application-specific signals still matter.

## Highlights

- **Configurable browser personas:** user agent, platform, locale, timezone,
  screen, hardware, media, canvas, WebGL, WebGPU, audio, font, and related
  surfaces, with cross-interface gaps and verification status tracked in
  [`FINGERPRINT_STATUS.md`](FINGERPRINT_STATUS.md).
- **Stable profile identity:** persistent SDK profiles reuse one fingerprint
  seed; nonpersistent launches generate a random 32-bit seed. Command-line
  switches allow reproducible test personas.
- **CloakBrowser-compatible SDK surface:** existing Playwright-based Python and
  Node scripts can usually migrate by changing the import.
- **Proxy-aware setup:** optional GeoIP resolution aligns locale and timezone
  using the effective proxy. Proxied launches default to Chromium's native
  non-proxied-UDP restriction; explicit/GeoIP-derived WebRTC IP presentation
  overrides leave actual ICE routing to the native backend.
- **Portable packages:** Windows, Linux, and macOS bundles are ZIP archives with
  runtime files, locales, fonts, and Chromium/Chromix license files. macOS
  bundles are unsigned and not notarized.
- **Reproducible source layers:** the Chromium source archive, ungoogled core,
  and Windows/Linux/macOS platform revisions are pinned in the repository.
- **Integrity checks:** releases include `SHA256SUMS`; the SDK verifies a bundle
  before extracting it when the manifest is available.

## Fingerprint verification

[`FINGERPRINT_STATUS.md`](FINGERPRINT_STATUS.md) records the P0/P1/P2 coverage,
retired inconsistent overrides, and build/runtime verification boundaries.
For a locally built or independently verified existing executable, run
`python3 tools/fingerprint_smoke.py --browser /path/to/chrome --platform linux --locale de-DE --output /tmp/fingerprint-smoke.json`.
The runner requires Python Playwright, serves its own loopback test pages, and
never downloads a browser. A passing tooling test is not a passing browser smoke.

The new [fingerprint regression gate](docs/fingerprint-acceptance.md) checks source
freshness before compilation and runs seven bounded suites against the exact
extracted executable. Display configuration now uses a launch-time emulation
backend instead of separate getters. These changes still need a matching native
Chromix build; installed Chrome control results are not release acceptance.

The GPU pool contains Windows/Linux/macOS identity templates, not a measured
full-device dataset. Screen/layout, font provenance, CPU/memory capabilities,
media backends and wire-level networking still have open consistency work.
Storage quota overrides now use the browser quota backend; Network Information
retains native notifier values. See the [public flag reference](docs/fingerprint-flags.md)
for defaults, implemented boundaries and the requirement to rebuild this stack.

## Downloads

Prebuilt **Windows x64, Linux x64/arm64, and macOS x64/arm64** packages are
published on the [GitHub Releases page](https://github.com/xiaozhou26/Chromix/releases)
as each independent platform succeeds, without waiting for the others. Available
platforms accumulate under one `v<CHROMIUM_VERSION>` tag, titled `Chromix <version>`.
The tag stays pinned to its initial source commit; notes identify each platform's
actual source SHA and build run, which may differ for the same Chromium version.
Existing assets and checksums are preserved, and an already published ZIP is never
replaced with different bytes. Every browser bundle is accompanied by `SHA256SUMS`.
macOS bundles remain unsigned and are not notarized because Apple signing
credentials are not part of this build.

Old aggregate runs are not automatically adopted. The selected Windows run `34080799322`
(artifact `10066146011`, source `23fd0a7a0c63cd452cfaec6b2aba8469ef5d4123`) was
verified and published manually to `v152.0.7977.82` as `Chromix 152.0.7977.82`, with
the original ZIP unchanged and licenses supplied as sidecars. Successful artifacts
from the old POSIX aggregate run `34308090891` likewise require manual verification
before appending to that tag; a failed aggregate event cannot publish them. Future
independent platform successes append automatically. See the release assets for
current availability; this transition does not require another Windows build.

| Browser version | Platform | Release |
|---|---|---|
| `152.0.7977.82` | Windows x64; other platforms pending | [`v152.0.7977.82`](https://github.com/xiaozhou26/Chromix/releases/tag/v152.0.7977.82) |
| `152.0.7977.75` | Windows x64 | [`v152.0.7977.75`](https://github.com/xiaozhou26/Chromix/releases/tag/v152.0.7977.75) |
| `151.0.7922.173` | Windows x64 | [`v151.0.7922.173`](https://github.com/xiaozhou26/Chromix/releases/tag/v151.0.7922.173) |

The source tree is pinned to Chromium `152.0.7977.82`; binary releases can lag
behind that pin. Machine-readable source pins are in
`build/ungoogled-revisions.psd1`; legacy version files remain available for
tooling compatibility. SDK package versions and release channels are unchanged.

### Verify and run on Windows

Download both `chromix-win-x64.zip` and `SHA256SUMS` from the same release, then
verify the archive in PowerShell:

```powershell
$actual = (Get-FileHash .\chromix-win-x64.zip -Algorithm SHA256).Hash.ToLowerInvariant()
$expected = ((Get-Content .\SHA256SUMS | Where-Object { $_ -match '\s+\*?chromix-win-x64\.zip$' }) -split '\s+')[0].ToLowerInvariant()
if ($actual -ne $expected) { throw "Chromix archive checksum mismatch" }
```

Extract and start the browser:

```powershell
Expand-Archive .\chromix-win-x64.zip -DestinationPath .\chromix-win-x64
.\chromix-win-x64\chromix\chromix.cmd
```

Pass Chromium flags after the launcher when a deterministic test persona is
needed:

```powershell
.\chromix-win-x64\chromix\chromix.cmd `
  --fingerprint=123456789 `
  --fingerprint-platform=windows `
  --fingerprint-timezone=Europe/Berlin `
  --fingerprint-locale=de-DE
```

## Python SDK

The Python wrapper returns Playwright `Browser` and `BrowserContext` objects and
uses CloakBrowser-compatible function names and keyword arguments.

Install from PyPI:

```bash
python -m pip install chromix playwright
python -m playwright install-deps
```

To install the current checkout instead, replace `chromix` with `./sdk/python`.

Launch Chromix:

```python
from chromix import launch

browser = launch(
    proxy="http://user:pass@proxy.example:8080",
    geoip=True,
    headless=False,
    humanize=True,
)
page = browser.new_page()
page.goto("https://example.com")
print(page.title())
browser.close()
```

Async and persistent-context variants are also available:

```python
from chromix import launch_persistent_context

context = launch_persistent_context(
    "./profile",
    locale="en-US",
    timezone="America/New_York",
    headless=False,
)
page = context.new_page()
page.goto("https://example.com")
context.close()
```

Binary management commands:

```bash
python -m chromix --version
python -m chromix install
python -m chromix info
python -m chromix clear-cache
python -m chromix widevine    # Linux x64 Widevine helper
```

See [`sdk/python/README.md`](sdk/python/README.md) for the full API and
environment variables.

## Node SDK

The Node wrapper uses `playwright-core` and exposes the matching camelCase API.

Install from npm:

```bash
npm install @xiaoxiaofeihh/chromix playwright-core
```

Launch Chromix:

```javascript
import { launch } from "@xiaoxiaofeihh/chromix";

const browser = await launch({
  proxy: "http://user:pass@proxy.example:8080",
  geoip: true,
  headless: false,
  humanize: true,
});
const page = await browser.newPage();
await page.goto("https://example.com");
console.log(await page.title());
await browser.close();
```

Binary management commands:

```bash
npx chromix --version
npx chromix install
npx chromix info
npx chromix clear-cache
```

See [`sdk/node/README.md`](sdk/node/README.md) for all options and intentional
compatibility differences.

### Package publication

- The Python distribution and import package are both named [`chromix`](https://pypi.org/project/chromix/).
- The Node SDK is [`@xiaoxiaofeihh/chromix`](https://www.npmjs.com/package/@xiaoxiaofeihh/chromix). The unscoped npm name `chromix` belongs to an unrelated project; always install and import the scoped package.

## Use a local browser binary

Set `CLOAKBROWSER_BINARY_PATH` to bypass release download in either SDK. Point
to the actual executable, not a ZIP, directory, Windows `.cmd`, or macOS `.app`
directory. Keep the rest of the extracted bundle alongside it.

Windows example:

```powershell
$env:CLOAKBROWSER_BINARY_PATH = "D:\chromix-build\src\out\Chromix\chrome.exe"
```

For an extracted Linux candidate use `/absolute/path/chromix/chrome`; on macOS
use `/absolute/path/chromix/Chromium.app/Contents/MacOS/Chromium`. Export that
path in the shell running the SDK, then call the usual `launch(...)`. Verify the
inner ZIP's checksum and preserve executable bits/framework symlinks when
extracting it; see [BUILDING.md](BUILDING.md#verify-and-run-a-posix-candidate).

The current Python `chromix.launch`/`launch_async` wrappers resolve the binary
first and pass their own `executable_path` to Playwright. Supplying
`chromix.launch(executable_path=...)` is **not supported**: it does not bypass
download and results in duplicate keyword arguments at launch. Direct Playwright
`chromium.launch(executable_path=...)` is a different API and does accept that
option. Similarly, Node's top-level `executablePath` is not a local-binary
selector; `launchOptions.executablePath` overrides Playwright's option only
*after* `ensureBinary()` runs. Use `CLOAKBROWSER_BINARY_PATH` for download-free
local use in both wrappers.

Other useful environment variables:

| Variable | Purpose |
|---|---|
| `CLOAKBROWSER_VERSION` | Select a configured browser major/version channel |
| `CLOAKBROWSER_RELEASE_CHANNEL` | Select `stable` or `latest` |
| `CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS` | Set the GeoIP lookup timeout |
| `CLOAKBROWSER_WIDEVINE_CDM` | Point to an existing Widevine CDM directory |
| `CLOAKBROWSER_WIDEVINE=0` | Disable Widevine discovery |
| `CHROMIX_CACHE_DIR` | Override the SDK binary cache directory |
| `CHROMIX_DOWNLOAD_HOST` | Override the release asset host |

Configured native bundle targets are Windows x64, Linux x64/arm64, and macOS
x64/arm64. All use ZIP archives. macOS candidates are unsigned for distribution
and not notarized.

## Persona options

The SDK accepts familiar high-level options such as `proxy`, `timezone`,
`locale`, `geoip`, `userAgent`, `viewport`, `colorScheme`, extension paths, and
persistent profile directories. It converts those settings into Playwright
options and the browser's `--fingerprint-*` command-line aliases.

Useful explicit arguments include:

```text
--fingerprint=<nonzero-uint64-seed>
--fingerprint-platform=linux|windows|macos
--fingerprint-timezone=<IANA-timezone>
--fingerprint-locale=<locale>
--force-webrtc-ip-handling-policy=disable_non_proxied_udp
```

Explicit caller settings take priority over GeoIP-derived values. Keep one
stable seed and profile directory when a test needs a persistent identity;
generate a new seed only when a new persona is intended.

The complete [public fingerprint flag table](docs/fingerprint-flags.md) covers
GPU, CPU/RAM, screen/taskbar, brand/version, quota, Windows font metrics, WebRTC
IP/auto, noise/off, third-party cookies, Windows voices and `FakeShadowRoot`.
Public fingerprint mode defaults to CPU/RAM 8/8 and platform-specific screen
geometry; these are declared defaults, not measured device records. The SDK's
page viewport stays native unless explicitly configured. Previously released
executables do not gain these features by updating the SDK alone.

## Advanced opt-in features

The following ports are compiled in but **disabled by default** because they can
break automation assumptions or weaken browser isolation:

- `--fingerprint-devtools-runtime-suppression` suppresses selected V8 Runtime
  observables and can interfere with console delivery or automation bindings.
- `--fingerprint-canvas-bridge=<host:port|ws://...>` plus
  `--fingerprint-canvas-bridge-unsafe` forwards canvas/WebGL operations to a
  configured endpoint and removes the sandbox from participating renderer
  processes.

`--fingerprint-webrtc-ip=<IP|auto>` is supported as a local presentation override.
The SDK can resolve it through HTTP/HTTPS/SOCKS metadata transport before launch;
bare-browser auto uses a bounded startup network request. The retired
`--fingerprint-webrtc-fake-srflx` and `--fingerprint-webrtc-fake-srflx-allow-udp`
options (and their `uxr` counterparts) remain rejected. Changing candidate text
does not route traffic; see the [resolution contract](docs/fingerprint-flags.md#webrtc-ip-and-proxy-resolution).

Use these only in controlled environments. More implementation detail is in
[`patches/README.md`](patches/README.md).

## Build from source

Chromix packages Windows x64, Linux x64/arm64, and macOS x64/arm64. The pinned layers are:

| Layer | Pin |
|---|---|
| Chromium | `152.0.7977.82` |
| ungoogled-chromium | `152.0.7977.82-1` |
| ungoogled-chromium-windows | `152.0.7977.82-1.1` |
| ungoogled-chromium-portablelinux | `152.0.7977.82-1` |
| ungoogled-chromium-macos | `152.0.7977.82-1.1` |
| Chromix | 146 patches listed in `patches/series` |

Requirements include Visual Studio 2022 with Desktop development with C++, the
Windows 11 SDK 10.0.26100 Debugging Tools, Python 3, Git, PowerShell 7, 7-Zip,
and roughly 120 GB of free disk space for Windows. Linux additionally needs
Chromium's Debian/Ubuntu build dependencies, Node.js, Go, and Ninja. macOS
needs Xcode, the command-line tools, Node.js, Ninja, and `zip` for packaging.
macOS artifacts are unsigned and not notarized because Developer ID and Apple
notary credentials are not part of this build.

From a Developer PowerShell:

```powershell
pwsh build/windows/build.ps1 -WorkDir D:\chromix-build -Jobs 8
```

Resume an interrupted compile with the same source tree:

```powershell
pwsh build/windows/build.ps1 -WorkDir D:\chromix-build -Resume -Jobs 8
```

The resulting browser is written to:

```text
D:\chromix-build\src\out\Chromix\chrome.exe
```

Read [`BUILDING.md`](BUILDING.md) for source revisions, GitHub Actions cache
reuse, domain substitution, packaging, and patch-maintenance details.

## Repository layout

```text
patches/          Chromium persona and integration patch series
build/windows/    Windows preparation, staged CI, build, and packaging scripts
build/linux/      Linux packaging helper
build/macos/      macOS build and notarization helpers
sdk/python/       Python Playwright wrapper and binary manager
sdk/node/         Node Playwright wrapper and binary manager
tools/            Patch linter, GN helpers, and regression tests
assets/fonts/     Font assets and provenance used by packaging
```

## Development checks

Run the repository checks before submitting a change:

```bash
python3 tools/check_patches.py
python3 -m unittest discover -s tools/tests -v
git diff --check
```

A full source preparation and compile is performed by the staged Windows GitHub
Actions workflow and the parallel Linux/macOS workflow; Chromium builds exceed a
single hosted runner's normal time budget.

## License

Chromix's original source code, patch integration, and SDKs are released under
the [BSD 3-Clause License](LICENSE). Bundled Chromium and third-party components
retain their own upstream licenses and notices; the BSD license does not replace
those terms. Font assets likewise retain the terms documented by their original
providers—see [`assets/fonts/SOURCE.md`](assets/fonts/SOURCE.md).

## Project status

Chromix is under active development. Chromium rebases can require individual
patch updates, and binaries are published only after the corresponding staged
Windows build and package checks succeed. Use a fixed release tag and verify its
checksum for reproducible automation.

## community

[LINUX DO](https://linux.do)
