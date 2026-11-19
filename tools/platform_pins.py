#!/usr/bin/env python3
"""Resolve validated Chromium/ungoogled pins for one target platform."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {"linux": "Linux", "macos": "MacOS", "windows": "Windows"}
COMMON_FIELDS = ("ChromiumVersion", "UngoogledVersion", "UngoogledCommit")
LINUX_OVERRIDES = tuple("Linux" + field for field in COMMON_FIELDS)
LINUX_VERSION_FILE = "CHROMIUM_LINUX_VERSION"
WINDOWS_OVERRIDES = tuple("Windows" + field for field in COMMON_FIELDS)
WINDOWS_VERSION_FILE = "CHROMIUM_WINDOWS_VERSION"
MACOS_OVERRIDES = tuple("MacOS" + field for field in COMMON_FIELDS)
MACOS_VERSION_FILE = "CHROMIUM_MACOS_VERSION"
OVERRIDES = {"linux": (LINUX_OVERRIDES, LINUX_VERSION_FILE),
             "macos": (MACOS_OVERRIDES, MACOS_VERSION_FILE),
             "windows": (WINDOWS_OVERRIDES, WINDOWS_VERSION_FILE)}


class PinError(ValueError):
    """Repository pins are incomplete, inconsistent, or not literal values."""


def load_shared_pins(repo: Path) -> dict:
    """Return the validated shared baseline without applying platform overrides."""
    return _load_pins(repo, None)


def load_pins(repo: Path, platform: str) -> dict:
    """Return pins with the selected platform's common-key overrides applied.

    Overrides and the platform version file must be present together. A
    repository with neither retains the shared CHROMIUM_VERSION identity.
    Unrelated platform pins are returned but are not validated for this target.
    """
    if platform not in PLATFORMS:
        raise PinError(f"unsupported platform: {platform}")
    return _load_pins(repo, platform)


def _load_pins(repo: Path, platform: str | None) -> dict:
    repo = Path(repo)
    text = (repo / "build/ungoogled-revisions.psd1").read_text(encoding="utf-8")
    assignments = {}
    pins = {}
    for match in re.finditer(r"^[ \t]*(\w+)[ \t]*=[ \t]*([^\r\n]*)", text, re.M):
        key, value = match.groups()
        assignments.setdefault(key, []).append(value)
        literal = re.fullmatch(r'"([^"\r\n]+)"\s*(?:#.*)?', value)
        if literal:
            pins[key] = literal[1]

    platform_fields = tuple("Ungoogled" + PLATFORMS[platform] + suffix
                            for suffix in ("Version", "Commit")) if platform else ()
    required = (*COMMON_FIELDS, *platform_fields)
    version_file = "CHROMIUM_VERSION"
    if platform in OVERRIDES:
        overrides, filename = OVERRIDES[platform]
        present = set(overrides) & assignments.keys()
        platform_file = repo / filename
        if present or platform_file.exists() or platform_file.is_symlink():
            if present != set(overrides) or not platform_file.is_file():
                raise PinError(f"{PLATFORMS[platform]} pin overrides and {filename} must all be present")
            required += overrides
            version_file = filename
    for key in required:
        if key not in pins or len(assignments.get(key, ())) != 1:
            raise PinError(f"missing, duplicate, or nonliteral pin: {key}")
    if version_file != "CHROMIUM_VERSION":
        pins.update({field: pins[PLATFORMS[platform] + field] for field in COMMON_FIELDS})

    version = pins["ChromiumVersion"]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", version):
        raise PinError("invalid ChromiumVersion")
    core = pins["UngoogledVersion"]
    downstream_linux157 = platform == "linux" and version == "157.0.8103.78"
    if downstream_linux157:
        try:
            from .linux157_source import validate_pins
        except ImportError:
            from linux157_source import validate_pins
        validate_pins(pins, repo)
    for key in ("UngoogledVersion", *(platform_fields[:1] if not downstream_linux157 else ())):
        value = pins[key]
        if (not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}-[0-9]+(?:\.[0-9]+)*", value)
                or value.split("-", 1)[0] != version):
            raise PinError(f"{key} does not match ChromiumVersion")
    if (platform_fields and not downstream_linux157 and pins[platform_fields[0]] != core
            and not pins[platform_fields[0]].startswith(core + ".")):
        raise PinError(f"{platform_fields[0]} does not match UngoogledVersion")
    for key in ("UngoogledCommit", *platform_fields[1:]):
        if not re.fullmatch(r"[a-f0-9]{40}", pins[key]):
            raise PinError(f"invalid commit pin: {key}")
    if (repo / version_file).read_text(encoding="utf-8").strip() != version:
        raise PinError(f"{version_file} does not match ChromiumVersion")
    return pins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--platform", choices=tuple(PLATFORMS), required=True)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--field")
    output.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        pins = load_pins(args.repo, args.platform)
        if args.as_json:
            value = json.dumps(pins, sort_keys=True)
        else:
            if args.field not in pins:
                raise PinError(f"unknown pin field: {args.field}")
            value = pins[args.field]
    except (OSError, ValueError) as exc:
        print(f"platform pins: {exc}", file=sys.stderr)
        return 1
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
