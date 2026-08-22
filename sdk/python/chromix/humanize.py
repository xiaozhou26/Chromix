"""
humanize — wrapper-level human input simulation for Fortress pages.

Implements the behavioral half of stealth (the engine handles the static
fingerprint): Bézier mouse trajectories with velocity easing and overshoot,
per-character typing with a realistic inter-key distribution and occasional
typo-and-correct, and eased scrolling.

Two ways to use it:

1. Directly, against any sync Playwright page (or anything exposing the same
   ``.mouse`` / ``.keyboard`` surface)::

       from chromix.humanize import human_click, human_type
       human_click(page, 240, 300)
       human_type(page, "hunter2")

2. Automatically, via the CloakBrowser-compatible API::

       from chromix import launch
       browser = launch(humanize=True)     # pages are patched on creation
       page = browser.new_page()
       page.mouse.click(240, 300)          # already human-like

Configuration follows CloakBrowser conventions: milliseconds, presets
'default'/'careful', and a HumanConfigOverrides mapping for per-field tweaks.
"""
from __future__ import annotations
import random
import time
from dataclasses import dataclass
from typing import Literal, TypedDict

__all__ = ["HumanConfig", "HumanConfigOverrides", "resolve_human_config",
           "Humanizer", "patch_page", "human_move", "human_click",
           "human_type", "human_press", "human_scroll"]

HumanPreset = Literal["default", "careful"]


class HumanConfigOverrides(TypedDict, total=False):
    typing_delay: float
    typing_delay_spread: float
    typing_pause_chance: float
    mouse_wobble_max: float
    mouse_overshoot_chance: float
    mouse_min_steps: int
    mouse_steps_divisor: float
    click_aim_delay: float
    click_hold: float
    mistype_chance: float
    scroll_pause: float
    seed: int


@dataclass
class HumanConfig:
    """All tunables, in milliseconds (CloakBrowser-style)."""
    typing_delay: float = 70
    typing_delay_spread: float = 40
    typing_pause_chance: float = 0.1
    mouse_wobble_max: float = 1.5
    mouse_overshoot_chance: float = 0.35
    mouse_min_steps: int = 8
    mouse_steps_divisor: float = 12.0
    click_aim_delay: float = 80
    click_hold: float = 100
    mistype_chance: float = 0.02
    scroll_pause: float = 300
    seed: int | None = None


def resolve_human_config(preset: HumanPreset = "default",
                         overrides: HumanConfigOverrides | None = None) -> HumanConfig:
    cfg = HumanConfig()
    if preset == "careful":
        cfg.typing_delay, cfg.typing_delay_spread = 130, 60
        cfg.mouse_steps_divisor, cfg.mouse_overshoot_chance = 8.0, 0.15
        cfg.click_aim_delay, cfg.click_hold = 200, 180
        cfg.mistype_chance = 0.04
    for k, v in (overrides or {}).items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    if cfg.seed is None:
        cfg.seed = random.randrange(1 << 31)
    return cfg


def _bezier(p0, p1, p2, p3, t):
    u = 1.0 - t
    x = u*u*u*p0[0] + 3*u*u*t*p1[0] + 3*u*t*t*p2[0] + t*t*t*p3[0]
    y = u*u*u*p0[1] + 3*u*u*t*p1[1] + 3*u*t*t*p2[1] + t*t*t*p3[1]
    return x, y


def _ease(t: float) -> float:
    """Ease-in-out approximation of hand acceleration."""
    return t * t * (3.0 - 2.0 * t)


class Humanizer:
    """Stateful human-input driver over a Playwright-style page.

    Keeps the current cursor position so consecutive moves chain naturally
    (real cursors never teleport).
    """

    def __init__(self, page, seed: int | None = None, cfg: HumanConfig | None = None,
                 raw_move=None, raw_type=None, raw_press=None, raw_wheel=None):
        self.page = page
        self.cfg = cfg or HumanConfig(seed=seed)
        if seed is not None:
            self.cfg.seed = seed
        self.rng = random.Random(self.cfg.seed)
        self.pos = (0.0, 0.0)
        # Raw input callbacks. patch_page() replaces page.mouse.move etc. with
        # wrappers that call back into this Humanizer, so it must inject the
        # unwrapped originals here or every move would recurse.
        self._move_cb = raw_move or page.mouse.move
        self._type_cb = raw_type or page.keyboard.type
        self._press_cb = raw_press or page.keyboard.press
        self._wheel_cb = raw_wheel or page.mouse.wheel

    def _ms(self, base: float, spread_frac: float = 0.5) -> float:
        """Jittered delay around `base` ms, returned in seconds."""
        return self.rng.uniform(base * (1 - spread_frac), base * (1 + spread_frac)) / 1000.0

    # -- mouse -----------------------------------------------------------

    def move(self, x: float, y: float, duration: float | None = None,
             steps: int | None = None):
        cfg = self.cfg
        dist = ((x - self.pos[0]) ** 2 + (y - self.pos[1]) ** 2) ** 0.5
        if dist < 1.0:
            return self
        if duration is None:
            duration = 0.08 + min(0.62, dist / 2500.0) + self.rng.random() * 0.06
        if steps is None:
            steps = max(cfg.mouse_min_steps, int(dist / cfg.mouse_steps_divisor))
        dx, dy = x - self.pos[0], y - self.pos[1]
        nx, ny = -dy / dist, dx / dist
        bow = (0.15 + self.rng.random() * 0.35) * dist
        if self.rng.random() < 0.5:
            bow = -bow
        c1 = (self.pos[0] + dx * 0.3 + nx * bow * 0.5,
              self.pos[1] + dy * 0.3 + ny * bow * 0.5)
        c2 = (self.pos[0] + dx * 0.7 + nx * bow * 0.4,
              self.pos[1] + dy * 0.7 + ny * bow * 0.4)
        last_t = time.monotonic()
        for i in range(1, steps + 1):
            t = _ease(i / steps)
            px, py = _bezier(self.pos, c1, c2, (x, y), t)
            wobble = self.rng.uniform(0.0, cfg.mouse_wobble_max)
            self._move_cb(px + self.rng.uniform(-wobble, wobble),
                                 py + self.rng.uniform(-wobble, wobble))
            target = last_t + duration * (i / steps) ** 1.15
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            last_t = target
        # Small overshoot-and-correct, like a hand settling on the target.
        if dist > 60 and self.rng.random() < cfg.mouse_overshoot_chance:
            ox, oy = x + self.rng.uniform(2, 6), y + self.rng.uniform(2, 6)
            self._move_cb(ox, oy)
            time.sleep(self.rng.uniform(0.02, 0.05))
        self._move_cb(x, y)
        self.pos = (x, y)
        return self

    def click(self, x: float | None = None, y: float | None = None,
              button: str = "left", double: bool = False):
        if x is not None and y is not None:
            self.move(x, y)
        time.sleep(self._ms(self.cfg.click_aim_delay))
        self.page.mouse.down(button=button)
        time.sleep(self._ms(self.cfg.click_hold))
        self.page.mouse.up(button=button)
        if double:
            time.sleep(self.rng.uniform(0.05, 0.10))
            self.page.mouse.down(button=button)
            time.sleep(self._ms(self.cfg.click_hold))
            self.page.mouse.up(button=button)
        return self

    # -- keyboard ----------------------------------------------------------

    def type(self, text: str, cps: float | None = None, mistype: bool | None = None):
        cfg = self.cfg
        if mistype is None:
            mistype = cfg.mistype_chance > 0
        for ch in text:
            if (mistype and ch.isalpha()
                    and self.rng.random() < cfg.mistype_chance):
                neighbor = self.rng.choice("qwertyuiopasdfghjklzxcvbnm")
                self._type_cb(neighbor, delay=0)
                time.sleep(self.rng.uniform(0.15, 0.4))
                self._press_cb("Backspace")
                time.sleep(self.rng.uniform(0.08, 0.2))
            self._type_cb(ch, delay=0)
            delay = self.rng.uniform(cfg.typing_delay - cfg.typing_delay_spread,
                                     cfg.typing_delay + cfg.typing_delay_spread) / 1000.0
            if self.rng.random() < cfg.typing_pause_chance:
                delay += self.rng.uniform(0.4, 1.0)
            time.sleep(max(delay, 0.01))
        return self

    def press(self, key: str):
        time.sleep(self._ms(50))
        self._press_cb(key)
        return self

    # -- scroll --------------------------------------------------------------

    def scroll(self, total_dy: int, duration: float | None = None):
        if total_dy == 0:
            return self
        if duration is None:
            duration = min(2.0, abs(total_dy) / 900.0 + 0.3)
        steps = max(4, int(abs(total_dy) / 120))
        done = 0
        for i in range(1, steps + 1):
            target = int(total_dy * _ease(i / steps))
            step = target - done
            if step:
                self._wheel_cb(0, step)
                done = target
            if self.rng.random() < 0.06:
                time.sleep(self.cfg.scroll_pause / 1000.0 * self.rng.uniform(0.6, 1.6))
            else:
                time.sleep(duration / steps * self.rng.uniform(0.6, 1.4))
        return self


# ---------------------------------------------------------------------------
# In-place page patching (used by launch(humanize=True))
# ---------------------------------------------------------------------------

def patch_page(page, cfg: HumanConfig | None = None):
    """Wrap a Playwright page's mouse/keyboard so ordinary calls behave humanly.

    ``page.mouse.move/click/dblclick/wheel`` and ``page.keyboard.type`` are
    replaced; everything else passes through untouched.
    """
    cfg = cfg or HumanConfig()
    mouse, keyboard = page.mouse, page.keyboard
    # Capture the unwrapped originals BEFORE installing the wrappers below —
    # the Humanizer must drive the real input path, not our own wrappers.
    raw_move, raw_type = mouse.move, keyboard.type
    raw_press, raw_wheel = keyboard.press, mouse.wheel
    h = Humanizer(page, cfg=cfg, raw_move=raw_move, raw_type=raw_type,
                  raw_press=raw_press, raw_wheel=raw_wheel)

    def _move(x, y, steps=None, **kw):
        h.move(x, y)
    mouse.move = _move

    def _click(x, y, button="left", click_count=None, delay=None, **kw):
        h.click(x, y, button=button, double=bool((click_count or 1) > 1))
    mouse.click = _click

    def _dblclick(x, y, button="left", **kw):
        h.click(x, y, button=button, double=True)
    mouse.dblclick = _dblclick

    def _wheel(dx, dy):
        if dy:
            h.scroll(int(dy))
        if dx:
            raw_wheel(dx, 0)
    mouse.wheel = _wheel

    def _type(text, delay=0, **kw):
        h.type(text)
    keyboard.type = _type

    def _press(*a, **kw):
        time.sleep(h._ms(50))
        raw_press(*a, **kw)
    keyboard.press = _press
    return page


# Convenience one-shot wrappers (each creates a throwaway Humanizer).
def human_move(page, x, y, **kw):
    return Humanizer(page).move(x, y, **kw)


def human_click(page, x=None, y=None, **kw):
    return Humanizer(page).click(x, y, **kw)


def human_type(page, keyboard_or_text, text=None, **kw):
    # Accept either human_type(page, "txt") or human_type(page, page.keyboard, "txt").
    if text is None:
        text = keyboard_or_text
    return Humanizer(page).type(text, **kw)


def human_press(page, key):
    return Humanizer(page).press(key)


def human_scroll(page, dy, **kw):
    return Humanizer(page).scroll(dy, **kw)
