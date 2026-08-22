# Patches — a partial surface layer (demo)

These are **some** of Fortress's surface-coherence patches: in-tree Chromium/Blink
modifications that read a per-launch persona and present it consistently across the
JS-observable fingerprint surfaces (user-agent, platform, WebGL, timezone, languages,
screen, keyboard, media, and so on), including inside worker and iframe realms.

> **This is a representative subset — not the full set, not the newest work, and not
> the current build.**
>
> What's here is an earlier demonstration slice. The bulk of the current stealth — the
> newer ("v2") surface-coherence patches, plus the part that *mints* a coherent
> per-launch real-device persona and *delivers* it into the process (the device-model
> corpus, the joint-distribution generator, and the process-level delivery seam) —
> ships **compiled inside the released binary** and is intentionally **not** published
> here.
>
> Building from these patches alone will **not** reproduce the shipped stealth (you'll
> get surfaces reading a persona that nothing provides). It demonstrates the technique,
> not the product.

For the real, current engine:

    pip install -U tilion-fortress
    # or
    docker run --rm -p 9222:9222 tilion/fortress:latest

See the [latest release](https://github.com/tiliondev/fortress/releases/latest) for
what actually ships.
