# Chromix patches

Surface-coherence patches applied after the pinned `ungoogled-chromium` core
and Windows overlay (see `CHROMIUM_VERSION` and
`build/ungoogled-revisions.psd1`): in-tree Chromium/Blink modifications that read a
per-launch persona and present it consistently across the JS-observable
fingerprint surfaces (user-agent, platform, WebGL, timezone, languages, screen,
keyboard, media, geolocation, detailed-screen, codec capability, and so on),
including inside worker and iframe realms where the upstream API permits it.

- One patch per file, numbered contiguously from `0001`; `series` lists them in
  apply order (`build/apply-patches.sh` / `build/windows/build.ps1` consume it).
- Clearcote patches are used as the behavioral reference, but are rebased and
  split for pinned Chromium 152 rather than applied verbatim from its Chromium
  149 tree. Ports include detailed-screen coherence, geolocation,
  MediaCapabilities, the CDP infinite-expiry cookie fix, and opt-in ports of
  Runtime-domain suppression, Canvas/WebGL Bridge, and fake WebRTC srflx.
- High-risk Clearcote behaviors remain default-off. Enable them explicitly with:
  - `--uxr-devtools-runtime-suppression` to suppress selected V8 Runtime-domain
    observables; this can break console delivery and automation bindings.
  - `--uxr-canvas-bridge=<host:port|ws://host:port/path>` together with
    `--uxr-canvas-bridge-unsafe`; this removes the renderer sandbox for bridge
    renderers and sends canvas/WebGL operations to that endpoint.
  - `--uxr-webrtc-fake-srflx=<IPv4>` to fabricate a server-reflexive candidate;
    add `--uxr-webrtc-fake-srflx-allow-udp` only when non-proxied UDP gathering
    is intentionally allowed.
- All CLI switches the patches introduce use the de-branded `--uxr-*` prefix
  (the `--fingerprint-*` aliases from the SDKs are normalized to `--uxr-*` by
  patch 0036). Run `python tools/check_patches.py` to enforce the invariants.

To build the pinned ungoogled source layers and then apply this series, see
`BUILDING.md`, `build/windows/build.ps1`, or the staged
`.github/workflows/build-win-x64-github.yml` CI.
