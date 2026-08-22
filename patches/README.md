# Chromix patches

Surface-coherence patches applied to a pinned Chromium checkout (see
`CHROMIUM_VERSION`): in-tree Chromium/Blink modifications that read a
per-launch persona and present it consistently across the JS-observable
fingerprint surfaces (user-agent, platform, WebGL, timezone, languages, screen,
keyboard, media, and so on), including inside worker and iframe realms.

- One patch per file, numbered contiguously from `0001`; `series` lists them in
  apply order (`build/apply-patches.sh` / `build/windows/build.ps1` consume it).
- All CLI switches the patches introduce use the de-branded `--uxr-*` prefix
  (the `--fingerprint-*` aliases from the SDKs are normalized to `--uxr-*` by
  patch 0036). Run `python tools/check_patches.py` to enforce the invariants.

To build a patched Chromium, see `build/build.sh` or the staged
`.github/workflows/build-win-x64-github.yml` CI.
