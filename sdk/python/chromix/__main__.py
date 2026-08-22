"""CLI: ``python -m chromix`` — pre-download / inspect / manage the binary."""
import argparse
import json
import sys

from . import __version__, binary_info, clear_cache, ensure_binary


def main():
    ap = argparse.ArgumentParser(
        prog="chromix",
        description="Manage the Chromix stealth Chromium binary.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("install", help="download the stealth binary now")
    sub.add_parser("info", help="show binary / cache info as JSON")
    sub.add_parser("clear-cache", help="delete all cached binaries")
    sub.add_parser("widevine", help="fetch the Widevine CDM (Linux x64) for DRM playback")
    ap.add_argument("--version", action="version", version=f"chromix {__version__}")
    args = ap.parse_args()

    if args.cmd == "install":
        print(ensure_binary())
    elif args.cmd == "info":
        print(json.dumps(binary_info(), indent=2))
    elif args.cmd == "clear-cache":
        clear_cache()
        print("cache cleared")
    elif args.cmd == "widevine":
        from .widevine import ensure_widevine
        cdm = ensure_widevine()
        if cdm:
            print(f"CDM ready: {cdm}")
        else:
            sys.exit("no Widevine CDM source available on this platform")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
