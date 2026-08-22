"""API tests for the CloakBrowser-compatible chromix surface (no browser launch needed)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chromix import api  # noqa: E402
from chromix.humanize import Humanizer, patch_page, resolve_human_config  # noqa: E402


class _MockMouse:
    def __init__(self):
        self.moves, self.wheels = [], []

    def move(self, x, y, steps=None, **kw):
        self.moves.append((x, y))

    def down(self, button="left"):
        pass

    def up(self, button="left"):
        pass

    def wheel(self, dx, dy):
        self.wheels.append((dx, dy))


class _MockKeyboard:
    def __init__(self):
        self.typed = []

    def type(self, text, delay=0, **kw):
        self.typed.append(text)

    def press(self, key, **kw):
        self.typed.append(f"<{key}>")


class _MockPage:
    def __init__(self):
        self.mouse, self.keyboard = _MockMouse(), _MockKeyboard()


def test_cloak_api_import_surface():
    import chromix as cx
    for name in api.__all__:
        assert getattr(cx, name, None) is not None, name


def test_proxy_string_with_credentials():
    pw, extra = api._resolve_proxy_config("http://u:p@proxy:8080")
    assert pw == {"proxy": {"server": "http://proxy:8080", "username": "u", "password": "p"}}
    assert extra == []


def test_proxy_string_plain():
    pw, _ = api._resolve_proxy_config("socks5://proxy:1080")
    assert pw == {"proxy": {"server": "socks5://proxy:1080"}}


def test_proxy_dict_passthrough():
    pw, _ = api._resolve_proxy_config(
        {"server": "http://proxy:8080", "bypass": ".google.com", "username": "u"})
    assert pw == {"proxy": {"server": "http://proxy:8080",
                            "bypass": ".google.com", "username": "u"}}


def test_extract_proxy_url():
    assert api._extract_proxy_url(None) is None
    assert api._extract_proxy_url("http://p:1") == "http://p:1"
    assert api._extract_proxy_url(
        {"server": "http://p:1", "username": "u", "password": "p"}) == "http://u:p@p:1"


def test_build_args_priority_and_dedupe():
    args = api.build_args(
        True,
        ["--fingerprint=42", "--lang=fr-FR", "--window-size=800,600"],
        timezone="Europe/Paris", locale="en-US", headless=True)
    # user overrides stealth seed; dedicated locale param overrides user lang
    assert "--fingerprint=42" in args
    assert "--lang=en-US" in args
    assert "--lang=fr-FR" not in args
    assert "--fingerprint-locale=en-US" in args
    assert "--fingerprint-timezone=Europe/Paris" in args
    assert "--window-size=800,600" in args
    # no duplicates by flag key
    keys = [a.split("=", 1)[0] for a in args]
    assert len(keys) == len(set(keys))


def test_build_args_maximize_suppressed_by_geometry():
    args = api.build_args(True, ["--window-size=800,600"], start_maximized=True)
    assert "--start-maximized" not in args
    args = api.build_args(True, None, start_maximized=True)
    assert "--start-maximized" in args


def test_get_default_stealth_args_seed_shape():
    sa = api.get_default_stealth_args()
    assert "--no-sandbox" in sa
    seeds = [a for a in sa if a.startswith("--fingerprint=")]
    assert len(seeds) == 1 and int(seeds[0].split("=")[1]) >= 10000


def test_geoip_disabled_passthrough():
    assert api.maybe_resolve_geoip(False, None, "UTC", "en") == ("UTC", "en", None)


def test_geoip_flag_promotion(monkeypatch):
    calls = {}

    def fake_http(proxy_url):
        calls["proxy"] = proxy_url
        return ("Asia/Tokyo", "jp", "203.0.113.9")

    monkeypatch.setattr(api, "_geoip_http", fake_http)
    # Raw flags count as explicit: geoip must not clobber them.
    tz, loc, ip = api.maybe_resolve_geoip(
        True, "http://p:1", None, None,
        ["--fingerprint-timezone=Europe/Berlin", "--lang=de-DE"])
    assert (tz, loc) == ("Europe/Berlin", "de-DE")
    assert ip == "203.0.113.9"
    assert calls["proxy"] == "http://p:1"


def test_webrtc_auto_resolution(monkeypatch):
    monkeypatch.setattr(api, "_geoip_http", lambda p: (None, None, "198.51.100.7"))
    out = api._resolve_webrtc_args(["--fingerprint-webrtc-ip=auto"], "http://p:1")
    assert out == ["--fingerprint-webrtc-ip=198.51.100.7"]
    out = api._resolve_webrtc_args(["--fingerprint-webrtc-ip=auto"], None)
    assert out == []


def test_webrtc_exit_ip_append():
    out = api._append_webrtc_exit_ip(["--headless=new"], "198.51.100.7")
    assert out[-1] == "--fingerprint-webrtc-ip=198.51.100.7"
    out = api._append_webrtc_exit_ip(["--fingerprint-webrtc-ip=x"], "198.51.100.7")
    assert out == ["--fingerprint-webrtc-ip=x"]
    assert api._append_webrtc_exit_ip(None, None) is None


def test_human_config_presets_and_overrides():
    cfg = resolve_human_config()
    slow = resolve_human_config("careful")
    assert slow.typing_delay > cfg.typing_delay
    custom = resolve_human_config("default", {"mistype_chance": 0.5, "seed": 7})
    assert custom.mistype_chance == 0.5 and custom.seed == 7


def test_patch_page_wraps_mouse_and_keyboard():
    page = _MockPage()
    cfg = resolve_human_config("default", {"seed": 1})
    # Make it fast: shrink all delays.
    cfg.typing_delay, cfg.typing_delay_spread = 0, 0
    cfg.typing_pause_chance, cfg.mistype_chance = 0, 0
    cfg.click_aim_delay, cfg.click_hold = 0, 0
    patch_page(page, cfg)
    page.mouse.click(100, 100)
    assert page.mouse.moves, "click must move the cursor along a path"
    page.keyboard.type("hi")
    assert page.keyboard.typed == ["h", "i"]
    page.mouse.wheel(0, 60)
    assert any(dy > 0 for _, dy in page.mouse.wheels), \
        "scroll must emit downward wheel events"


def test_humanizer_zero_sleep_fast_path():
    page = _MockPage()
    h = Humanizer(page, cfg=resolve_human_config("default", {"seed": 3}))
    h.cfg.typing_delay = h.cfg.typing_delay_spread = 0
    h.cfg.typing_pause_chance = h.cfg.mistype_chance = 0
    t0 = time.monotonic()
    h.type("abc").click(10, 10)
    assert time.monotonic() - t0 < 1.0
