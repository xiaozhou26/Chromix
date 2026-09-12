# Public fingerprint flags

This is the contract of the **current Chromium 152.0.7977.82 source stack**, not
a claim about previously released binaries. Rebuild the browser and use the
matching Python/Node SDK checkout. Upstream descriptions saying “148+” or “150+”
do not prove that an older Chromix executable contains these changes.

All flags below work through browser `args`; patch `0036` also normalizes them
for direct executable launches. No JavaScript init-script injection is required.
One browser launch has one immutable persona, shared with its renderers/workers;
these are not independent per-BrowserContext identities.

## Flags and defaults

Defaults in this table apply when `--fingerprint=<seed>` is enabled. Without it,
explicit field flags still work, but the CPU/RAM/screen/quota defaults are not
all injected. `--fingerprint=off` disables persona overrides as described below.

| Flag | Current implementation |
|---|---|
| `--fingerprint=<seed>` | Nonzero decimal uint64 seed. Bare `--fingerprint` generates a seed. SDK persistent profiles reuse their saved seed; nonpersistent SDK launches generate a seed. |
| `--fingerprint-platform` | `windows` / `Win32`, `macos` / `MacIntel`, `linux` / `Linux x86_64`. Same-OS generic aliases retain native high-entropy OS fields; cross-OS aliases use declared desktop defaults. |
| `--fingerprint-gpu-vendor` | WebGL `UNMASKED_VENDOR_WEBGL`; absent values are selected with the renderer from a seed/platform template. |
| `--fingerprint-gpu-renderer` | WebGL `UNMASKED_RENDERER_WEBGL`; coherent template selection also supplies known WebGPU vendor/architecture fields. Real software/fallback WebGPU adapters stay native. |
| `--fingerprint-hardware-concurrency` | `navigator.hardwareConcurrency`, default **8**; SDK accepts integers 1–128. Does not allocate or emulate CPU cores. |
| `--fingerprint-device-memory` | `navigator.deviceMemory`, default **8 GB**; positive values up to 32 are rounded to the nearest supported bucket: 0.25, 0.5, 1, 2, 4, 8, 16, 32. Does not change V8 heap limits. |
| `--fingerprint-screen-width` | Default **1920** on Windows/Linux, **1440** on macOS. DIP screen geometry uses the launch display backend, not isolated screen getters. |
| `--fingerprint-screen-height` | Default **1080** on Windows/Linux, **900** on macOS. Width/height must be positive integers no greater than 32768. |
| `--fingerprint-brand` | `Chrome`, `Edge`, `Opera`, `Vivaldi` (case-insensitive). Changes UA/Client Hints branding, not the browser engine or vendor-specific features. |
| `--fingerprint-brand-version` | Numeric browser-brand version. Chrome also uses it for its engine token unless an explicit `uxr-ua-full-version` wins. Edge/Opera/Vivaldi retain a separate Chromium engine version and append `Edg` / `OPR` / `Vivaldi` UA tokens. |
| `--fingerprint-platform-version` | Numeric Client Hints platform version. Does not change the host OS. |
| `--fingerprint-timezone` | IANA timezone, for example `America/New_York`. Also retained in off mode when explicitly supplied. |
| `--fingerprint-locale` | Language tag, for example `en-US`; normalized language configuration and Accept-Language. Also retained in off mode when explicitly supplied. |
| `--fingerprint-storage-quota` | Integer **MiB** (1024² bytes), default **102400 MiB**. Launch-local browser quota policy, shared by origin estimates, Storage Buckets and legacy quota APIs. `0` is a valid zero quota. |
| `--fingerprint-taskbar-height` | Default **48** Windows, **95** macOS, **0** Linux. Reserves that height from the emulated work area; does not resize the physical OS taskbar/dock. |
| `--fingerprint-windows-font-metrics` | Opt-in Linux → Windows font metric alignment. Requires a Windows persona and actual matching Windows font files; otherwise no-op. See the font boundary below. |
| `--fingerprint-webrtc-ip` | Literal IPv4/IPv6 or `auto`. Rewrites **local presentation copies** of ICE candidates, SDP and stats. `geoip=True` / `geoip: true` supplies the resolved exit IP unless an explicit IP wins. See routing/resolution below. |
| `--fingerprint-noise=false` | Keeps identity seeds and disables existing fingerprint perturbation paths, including the optional Canvas text-metric path. Does **not** imply four active Canvas/WebGL/audio/client-rect noise engines. |
| `--fingerprint=off` | Native-persona debug mode. Also accepts `false`, `0`, `disable`, `disabled` (case-insensitive). Removes fingerprint seeds/platform and identity overrides; explicit locale/timezone remain. |
| `--fingerprint-allow-3p-cookies` | Launch-only opt-in for third-party cookies; default **off**. Removes the presence-only phaseout test switch and applies the cookie backend policy without changing persisted preferences. |
| `--fingerprint-sapi-voices=false` | Opt out of Windows voice-table presentation. Default **on for a Windows persona**; genuine Windows uses its actual native voice inventory. Cross-OS table mode does not install SAPI or synthesize those voices. |
| `--enable-blink-features=FakeShadowRoot` | Explicit Blink feature: exposes closed **author** roots through `element.shadowRoot`. Default off; UA-internal roots and native `OpenShadowRoot()` semantics remain unchanged. |

Public boolean flags accept `true/1/on/enable/enabled` (or no value), and
`false/0/off/disable/disabled`. SDKs reject malformed public numeric/boolean
values before launch. An explicit `--uxr-*` field normally takes precedence over
its public alias; conflicting geometry aliases are rejected in SDK synthetic
geometry mode. Use `--key=value`, not two separate arguments.

## Identity templates are not a measured device pool

GPU identities are selected as platform-compatible tuples: three Windows,
three Linux, three macOS ARM and two macOS Intel templates. Their weights are
deterministic test choices, **not measured market shares**. Recognized one-sided
GPU hints select a compatible counterpart. Unknown one-sided identities leave
the missing side empty rather than borrowing an unrelated native identity.
Complete explicit vendor/renderer pairs remain the caller's values; unknown
WebGPU architecture/device/driver fields are not invented.

Public GPU identity flags preserve actual GL limits, extensions, shader
precision, readback and Dawn capabilities. Changing a string does not provide
another GPU backend. CPU/RAM getters similarly do not change scheduling,
SIMD/Wasm support or allocation limits.

The older independently seeded hardware/display and GL-capability test paths
still require `--uxr-synthetic-device-tests=true`. They are separate from the
public fixed defaults above. Legacy font substitutions and Canvas readback/
export noise also remain synthetic-test opt-ins. WebGL/audio/client-rect
getter-only perturbations have **not** been restored by this compatibility work.

[Measured device mode](device-pool.md) selects evidence-backed whole native
records and launches with `--fingerprint=off`. It does not use these defaults
or convert these GPU templates into measured hardware records.

## Geometry and quota semantics

- Supplying only one screen dimension or a taskbar/work-area field completes
  the missing screen dimensions from the platform defaults. It does not turn
  on the unrelated CPU/RAM/quota defaults.
- Available width/height cannot exceed screen dimensions; taskbar height must
  agree with an explicitly supplied available height. Native window size and
  page viewport are distinct from screen size. SDK default page viewport stays
  native unless explicit viewport or synthetic geometry is requested.
- Quota is a browser-side policy, not a renderer-only `storage.estimate()` lie.
  Real usage, disk exhaustion, per-bucket caps and native errors remain;
  explicit DevTools quota overrides take precedence. Static origin/bucket
  reporting returns the effective override rather than a disk-based estimate
  or a larger requested bucket cap. An explicit zero override means no new
  allocation; it is not the native zero/unlimited sentinel. Privileged
  unlimited storage keeps its native policy. This is not a disk-size
  reservation. The maximum accepted MiB value is **8796093022207**, checked
  before multiplication into signed 64-bit bytes.

## WebRTC IP and proxy resolution

An explicit IP is parsed as a literal, never as a hostname. The presentation
override covers local `icecandidate` events, candidate address/related-address
fields, local SDP including offer/answer results, local candidate stats and
local candidate-error addresses. Remote candidates/descriptions are untouched.
Zero placeholders and TURN relay allocations are preserved. No candidates or
successful STUN responses are fabricated.

An exact published local SDP passed back to `setLocalDescription()` is restored
to its cached native SDP before libwebrtc parses it. Arbitrarily edited SDP is
not reverse-guessed. Publishing substituted addresses to a peer still does not
make them reachable: the actual socket/proxy/TURN configuration owns routing.

### Python / Node

- Resolve before Playwright launch through the **effective** proxy (including
  launch/context proxy overrides or a single raw `--proxy-server`).
- `auto` and GeoIP use the SDK's existing `http://ip-api.com` JSON endpoint;
  GeoIP resolves timezone/locale and reuses the same exit IP without a second
  lookup. Explicit timezone/locale/IP win. This HTTP metadata is not independent
  verification of a route.
- Metadata transport supports HTTP/HTTPS proxies and SOCKS4/4a/5/5h (`socks`
  means SOCKS5). SOCKS5 username/password authentication is supported by the
  **metadata client**. SOCKS5/4a resolve destination names at the proxy; SOCKS4
  needs local IPv4 DNS. This does not add SOCKS authentication or every URL
  alias to Chromium/Playwright's browser proxy backend.
- No environment proxy discovery, redirect following, direct fallback or
  alternate proxy retry. PAC/auto-detect and multi-route raw configurations are
  rejected for metadata lookup. Empty raw proxies, raw proxy credentials and
  different simultaneous raw/Playwright proxy endpoints are rejected; matching
  endpoints may retain the high-level proxy credentials. Omitted default ports
  and equivalent IPv6 spellings do not create a false conflict. A proxy
  conflicting with `--no-proxy-server` is rejected. Without a high-level proxy,
  `--no-proxy-server` selects direct even if other raw proxy flags are present.
  Failures stop launch.
- Default total timeout is 10 seconds, configurable by
  `CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS` in `(0, 60]`; response limit is 64 KiB.
  Python system DNS may outlast the deadline, but a late connection does not
  send the lookup request. Python needs IANA timezone data (for example `tzdata`
  on Windows).
- Proxied launches default to
  `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` unless an explicit
  native policy was supplied. Changing a later context's proxy does not change
  the browser's already frozen persona/IP.

### Direct executable

`--fingerprint-webrtc-ip=auto` resolves once via **https://api.ipify.org**, after
the browser network service is ready and before the first renderer snapshot.
It uses a 10-second deadline and 256-byte limit, no cookies, redirects, cache
or retry. Explicit fixed proxy routes use an isolated network context with no
bypass/fallback list. Without a command-line proxy it uses the system network
context; `--no-proxy-server` explicitly selects direct.

Native auto rejects empty explicit proxy routes, PAC/auto-detect, route lists
and proxy URIs containing credentials; `--no-proxy-server` takes precedence
over other raw proxy flags. Use the SDK's prelaunch resolution for metadata proxy credentials;
normal Chromium/Playwright proxy support still determines browser connectivity.
Failure aborts startup rather than switching to another route. The native
resolver checks that the persona is not frozen both before and after the
asynchronous lookup. Startup integration still requires a matching Chromium build test.

`webrtc-fake-srflx` and `webrtc-fake-srflx-allow-udp` remain retired. Supporting
`webrtc-ip` does not re-enable them.

## Fonts, voices, cookies and off mode

Font metrics use bounded OS/2/hhea table reads only after the resolved family
matches the Windows-font allowlist. Missing/substituted fonts, invalid tables
and variable MVAR metrics leave native metrics unchanged. This aligns selected
ascent/descent/leading values, **not** the complete DirectWrite rasterizer,
hinting, glyph fallback or shaping. On Linux install the actual fonts or supply
`fonts_dir` / `fontsDir` so Fontconfig can load them; naming a font is not enough.

Windows speech tables are updated at the native asynchronous voice-list event
boundary, not on every getter. On a real Windows host the normal mode preserves
the actual inventory/backend; on another OS a Windows persona exposes the table.
The flag does not install language packs, provide Windows voice synthesis, or
hide every backend capability difference.

Third-party cookie opt-in changes the general launch policy, not SameSite/Secure
requirements or site-specific content blocks. It is not a promise that any
particular embedded login/payment flow completes.

For native-persona comparison, use only `--fingerprint=off`, with GeoIP off and
no explicit timezone/locale, Playwright emulation or other feature overrides.
Explicit cookie and `FakeShadowRoot` opt-ins are independent and can still be
applied. Off mode restores the patched UA/GPU/hardware/display/voice/noise and
automation getter controls; it does not uninstall ungoogled Chromium or undo
unrelated browser fixes.

## Examples

Use an executable rebuilt with this source stack:

```powershell
.\chrome.exe --fingerprint=42 --fingerprint-platform=windows `
  --fingerprint-brand=Edge --fingerprint-brand-version=152.0.0.0 `
  --fingerprint-hardware-concurrency=8 --fingerprint-device-memory=8 `
  --fingerprint-screen-width=1920 --fingerprint-screen-height=1080 `
  --fingerprint-taskbar-height=48 --fingerprint-storage-quota=102400 `
  --fingerprint-timezone=Asia/Shanghai --fingerprint-locale=zh-CN `
  --fingerprint-noise=false --fingerprint-allow-3p-cookies `
  --enable-blink-features=FakeShadowRoot
```

```python
from chromix import launch

browser = launch(
    proxy="http://127.0.0.1:8080",
    geoip=True,
    args=["--fingerprint=42", "--fingerprint-noise=false"],
)
browser.close()
```

```javascript
import { launch } from '@xiaoxiaofeihh/chromix';

const browser = await launch({
  proxy: 'http://127.0.0.1:8080',
  geoip: true,
  args: ['--fingerprint=42', '--fingerprint-noise=false'],
});
await browser.close();
```

## Verification scope

`tools/tests/test_fingerprint_features.py` compiles the public normalizer,
quota parser and shadow binding contracts. Backend tests execute extracted
GPU/IP/font helpers; GPU/WebRTC suites retain native capability/STUN invariants.
SDK suites exercise all launch paths plus loopback HTTP/TLS/SOCKS transports.
`tools/tests/test_fingerprint_quota_backend.py` round-trips independent quota
source excerpts and compiles the actual reporting/allocation-check methods
against callback/database shims. It covers bucket caps, zero overrides, native
errors, default/named buckets, privileged storage and asynchronous DevTools changes.
These are not full Chromium translation-unit builds or browser acceptance.

The [native regression gate](fingerprint-acceptance.md) must run on an executable
from this exact source stack. No matching rebuilt browser or passing release
gate has been recorded for this compatibility update. In particular, native
startup auto, storage writes/buckets, third-party cookie behavior, cross-OS
font rendering and speech synthesis still need integration evidence.
