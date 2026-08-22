#!/usr/bin/env python3
"""
fetch-widevine — fetch/install the Widevine CDM for Fortress.

Sources (first available wins):
  1. --from-dir <path>       explicit component-layout CDM directory
  2. installed Google Chrome pointed at directly (win/mac, no copy)
  3. Linux: stable Chrome .deb downloaded and WidevineCdm/ extracted

Installs into --dest (default ~/.cache/tilion-fortress/widevine) unless
--link is given, and prints the engine flag to pass:

    --uxr-widevine-cdm=<dir>

The SDK does all of this automatically via Fortress(widevine=True); this CLI
exists for Docker images, CI, and manual bundle preparation.

    python tools/fetch-widevine.py [--dest DIR] [--from-dir DIR] [--link]
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))

from tilion_fortress import widevine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", type=Path,
                    default=Path.home() / ".cache" / "tilion-fortress" / "widevine",
                    help="where to install/copy the CDM (default: SDK cache)")
    ap.add_argument("--from-dir", type=Path,
                    help="explicit CDM directory to install from")
    ap.add_argument("--link", action="store_true",
                    help="print the source dir without copying (uses it in place)")
    args = ap.parse_args()

    plat = widevine._platform_key()
    if not plat:
        sys.exit("unsupported platform for Widevine fetch")
    valid = widevine._valid_cdm_dir

    src: Path | None = None
    if args.from_dir:
        src = args.from_dir if valid(args.from_dir, plat) else None
        if not src:
            sys.exit(f"--from-dir {args.from_dir} is not a valid CDM layout for {plat}")
    else:
        src = widevine.find_local_chrome_cdm(plat)
        if not src and plat == ("linux", "x64"):
            src = widevine.fetch_widevine_from_deb(args.dest)
    if not src:
        sys.exit("no Widevine source available (install Google Chrome, or pass --from-dir)")

    if args.link or src.parent == args.dest or src.parent.parent == args.dest:
        target = src
    else:
        target = args.dest / "WidevineCdm"
        widevine._copy_tree(src, args.dest)
        # copytree produced <dest>/<src.name>/; normalize to <dest>/WidevineCdm
        made = args.dest / src.name
        if made != target and made.is_dir():
            made.rename(target)

    if not valid(target, plat):
        sys.exit(f"installed CDM at {target} failed validation")

    import json
    version = json.loads((target / "manifest.json").read_text()).get("version", "?")
    print(f"Widevine CDM {version} ready at {target}")
    print(f"engine flag: --uxr-widevine-cdm={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
