# Chromix patches

Surface-coherence patches applied after the pinned `ungoogled-chromium` core,
matching Windows/Linux/macOS platform layer, and binary pruning (see
`CHROMIUM_VERSION` and `build/ungoogled-revisions.psd1`): in-tree Chromium/Blink modifications that read a
per-launch persona for selected JS-observable fingerprint surfaces (user-agent,
platform, WebGL, timezone, languages, screen, keyboard, media, geolocation,
detailed-screen, codec capability, and so on). Coverage and known cross-interface
gaps, including worker/iframe behavior, are tracked in
[`FINGERPRINT_STATUS.md`](../FINGERPRINT_STATUS.md). A configured getter does not
by itself change the underlying device, permission, rendering or network behavior.

Network Information preserves native notifier behavior; storage quota overrides
now use the browser quota backend (`0129`), not isolated renderer getters.
MediaCapabilities filters can
only restrict native support, smoothness and power efficiency. GPU templates
remain synthetic test records rather than a measured full-device pool.

The current series contains **146 patches**. The [public flag contract](../docs/fingerprint-flags.md)
documents fixed public defaults, GPU platform tuples, independent brand versions,
quota, third-party cookies, closed author shadow roots, Windows font metrics,
voice tables, local WebRTC presentation/auto and noise/off semantics. These are
source/SDK implementations awaiting a matching rebuilt browser, not evidence
that previously published packages contain them.

Canvas patches `0121`–`0124` retain one prepared buffer across async encoding
and remove random input mutation from the shared image encoder. Persona noise
is applied once in Canvas readback/buffer preparation; disabling it also keeps
encoded output free of the downstream legacy transform. GPU readback preserves
native packing, errors and backend bytes instead of a CPU-only rewrite. These
changes still require a matching Chromium build and browser verification.

- One patch per file, numbered contiguously from `0001`; `series` lists them in
  apply order (`build/apply-patches.sh` / `build/windows/build.ps1` consume it).
- Clearcote patches are used as the behavioral reference, but are rebased and
  split for pinned Chromium 152 rather than applied verbatim from its Chromium
  149 tree. Ports include detailed-screen coherence, geolocation,
  MediaCapabilities, the CDP infinite-expiry cookie fix, and opt-in ports of
  Runtime-domain suppression and Canvas/WebGL Bridge. Fake WebRTC candidate
  generation remains retired; `0139`–`0145` add explicit local presentation
  rewriting and startup auto resolution without changing native ICE gathering.
- High-risk Clearcote behaviors remain default-off. Enable them explicitly with:
  - `--uxr-devtools-runtime-suppression` to suppress selected V8 Runtime-domain
    observables; this can break console delivery and automation bindings.
  - `--uxr-canvas-bridge=<host:port|ws://host:port/path>` together with
    `--uxr-canvas-bridge-unsafe`; this removes the renderer sandbox for bridge
    renderers and sends canvas/WebGL operations to that endpoint.
- WebRTC uses actual ICE gathering and the native IP handling policy. The
  SDKs support `webrtc-ip=<IP|auto>` and reject retired `webrtc-fake-srflx`
  options; they default proxied launches to `disable_non_proxied_udp`. They
  resolve metadata through HTTP/HTTPS/SOCKS without direct fallback. This does not supply
  a full-network proxy or TLS/HTTP persona implementation.
- All CLI switches the patches introduce use the de-branded `--uxr-*` prefix
  (the `--fingerprint-*` aliases from the SDKs are normalized to `--uxr-*` by
  patch 0036). Run `python tools/check_patches.py` to enforce the invariants.

For Windows x64, Linux x64/arm64, and macOS x64/arm64, the source-layer order is
**Chromium archive → ungoogled core → platform patches → prune → Chromix**.
The matching platform repository is `ungoogled-chromium-windows`,
`ungoogled-chromium-portablelinux`, or `ungoogled-chromium-macos`; exact commits
come from `build/ungoogled-revisions.psd1`. A successful patch/fixture check is
not a successful build. Release packages or CI artifacts built before this
series changed do not validate it; matching native compile and runtime
verification remain required.

To build the pinned ungoogled source layers and then apply this series, see
`BUILDING.md`, `build/prepare-ungoogled.sh`, `build/windows/build.ps1`, or the
staged `.github/workflows/build-win-x64-github.yml` and the four
`build-linux-{x64,arm64}.yml` / `build-macos-{x64,arm64}.yml` CI entrypoints.
