"""Public fingerprint-switch syntax shared by all Python launch paths."""
from __future__ import annotations

import math
import re

OFF_VALUES = frozenset(("off", "false", "0", "disable", "disabled"))
_BRANDS = {"chrome": "Chrome", "google chrome": "Chrome", "edge": "Edge",
           "microsoft edge": "Edge", "opera": "Opera", "vivaldi": "Vivaldi"}
_BOOLEANS = {"--fingerprint-noise", "--fingerprint-sapi-voices",
             "--fingerprint-allow-3p-cookies", "--fingerprint-windows-font-metrics"}
_INTEGERS = {
    "--fingerprint-hardware-concurrency": (1, 128),
    "--fingerprint-screen-width": (1, 32768),
    "--fingerprint-screen-height": (1, 32768),
    "--fingerprint-taskbar-height": (0, 32768),
    "--fingerprint-storage-quota": (0, ((1 << 63) - 1) // (1024 * 1024)),
}


def fingerprint_off(args):
    modes = [a.partition("=")[2].lower() for a in args if a.partition("=")[0] == "--fingerprint"]
    return bool(modes and modes[-1] in OFF_VALUES)


def normalize_fingerprint_args(args):
    result = []
    for arg in args or []:
        if not isinstance(arg, str) or "\0" in arg:
            raise ValueError("browser arguments must be strings without NUL")
        key, equal, value = arg.partition("=")
        if key == "--fingerprint":
            if value.lower() in OFF_VALUES:
                arg = "--fingerprint=off"
            elif value and (not re.fullmatch(r"[0-9]+", value) or not 1 <= int(value) < (1 << 64)):
                raise ValueError("--fingerprint requires a uint64 seed or off/false/0/disable/disabled")
        elif key == "--fingerprint-brand":
            brand = _BRANDS.get(value.lower())
            if brand is None:
                raise ValueError("--fingerprint-brand must be Chrome, Edge, Opera or Vivaldi")
            arg = key + "=" + brand
        elif key in _BOOLEANS:
            low = value.lower()
            if low in OFF_VALUES:
                arg = key + "=false"
            elif low in ("", "true", "1", "on", "enable", "enabled"):
                arg = key + "=true"
            else:
                raise ValueError(f"{key} requires a boolean")
        elif key in _INTEGERS:
            minimum, maximum = _INTEGERS[key]
            if not re.fullmatch(r"[0-9]+", value) or not minimum <= int(value) <= maximum:
                raise ValueError(f"{key} requires an integer in [{minimum}, {maximum}]")
        elif key == "--fingerprint-device-memory":
            try:
                valid = bool(re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value))
                valid = valid and math.isfinite(float(value)) and 0 < float(value) <= 32
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("--fingerprint-device-memory requires a number greater than 0 and at most 32")
        elif key in ("--fingerprint-brand-version", "--fingerprint-platform-version"):
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", value) or any(int(n) > 0xffffffff for n in value.split(".")):
                raise ValueError(f"{key} requires a numeric version with at most four components")
            if key == "--fingerprint-brand-version" and not 1 <= int(value.split(".")[0]) <= 0x7fffffff:
                raise ValueError(f"{key} requires a positive int32 major version")
        result.append(arg)
    if fingerprint_off(result):
        result = [arg for arg in result if arg.partition("=")[0] != "--fingerprint-platform"]
    return result
