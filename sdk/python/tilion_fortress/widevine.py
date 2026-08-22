"""widevine — locate or fetch the Widevine CDM for Fortress.

The engine (patch 0048, args.gn enable_widevine) accepts a component-layout
CDM directory via ``--uxr-widevine-cdm=<dir>``:

    <dir>/manifest.json
    <dir>/_platform_specific/<os>_<arch>/widevinecdm.{dll,so,dylib}

Sources, in order:
  1. ``CLOAKBROWSER_WIDEVINE_CDM`` / ``TILION_WIDEVINE_CDM`` env (explicit dir;
     the CloakBrowser name is honored for drop-in parity).
  2. An installed Google Chrome — its version directory ships WidevineCdm in
     exactly the required layout, so we point at it directly (no copy).
  3. Linux only: the stable Chrome .deb (fixed URL) — downloaded once, the
     WidevineCdm/ payload extracted into the SDK cache.

No Google API keys and no component-updater round trip.
"""
from __future__ import annotations
import io
import json
import lzma
import os
import shutil
import struct
import sys
import tarfile
import urllib.request
from pathlib import Path

__all__ = ["ensure_widevine", "find_local_chrome_cdm", "fetch_widevine_from_deb"]

_DEB_URL = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"

# os_arch -> CDM library name (component layout component naming).
_LIB_NAMES = {
    ("win", "x64"): ("win_x64", "widevinecdm.dll"),
    ("linux", "x64"): ("linux_x64", "libwidevinecdm.so"),
    ("darwin", "x64"): ("mac_x64", "libwidevinecdm.dylib"),
    ("darwin", "arm64"): ("mac_arm64", "libwidevinecdm.dylib"),
}

# Installed-Chrome locations that bundle WidevineCdm/<ver>/ in the version dir.
_CHROME_ROOTS = {
    "win": [r"C:\Program Files\Google\Chrome\Application",
            r"C:\Program Files (x86)\Google\Chrome\Application"],
    "darwin": ["/Applications/Google Chrome.app/Contents/Versions"],
}


def _platform_key() -> tuple[str, str] | None:
    import platform
    sysname, mach = platform.system(), platform.machine().lower()
    os_key = {"Windows": "win", "Linux": "linux", "Darwin": "darwin"}.get(sysname)
    if not os_key:
        return None
    arch = "x64" if mach in ("x86_64", "amd64") else ("arm64" if mach in ("arm64", "aarch64") else None)
    if not arch:
        return None
    return (os_key, arch)


def _valid_cdm_dir(path: Path, plat: tuple[str, str]) -> bool:
    subdir, lib = _LIB_NAMES[plat]
    return (path / "manifest.json").is_file() and (path / "_platform_specific" / subdir / lib).is_file()


def find_local_chrome_cdm(plat: tuple[str, str]) -> Path | None:
    """Point at an installed Chrome's bundled WidevineCdm (no copy needed)."""
    roots = _CHROME_ROOTS.get(plat[0], [])
    best: Path | None = None
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for ver_dir in root_path.iterdir():
            if not ver_dir.is_dir() or not ver_dir.name[0].isdigit():
                continue
            cdm = ver_dir / "WidevineCdm"
            if _valid_cdm_dir(cdm, plat):
                if best is None or ver_dir.name > best.parent.name:
                    best = cdm
    return best


def _copy_tree(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest / src.name, dirs_exist_ok=True)


def fetch_widevine_from_deb(cache: Path, timeout: float = 300) -> Path | None:
    """Download the stable Chrome .deb and extract WidevineCdm/ into `cache`."""
    cache.mkdir(parents=True, exist_ok=True)
    marker = cache / ".widevine-extracted"
    if marker.is_file():
        for d in cache.glob("WidevineCdm"):
            return d
    deb = cache / "google-chrome-stable_amd64.deb"
    if not deb.is_file():
        sys.stderr.write(f"[tilion-fortress] downloading {_DEB_URL} ...\n")
        urllib.request.urlretrieve(_DEB_URL, deb)
    try:
        member_dir = _extract_widevine_from_deb(deb, cache)
    finally:
        deb.unlink(missing_ok=True)
    if member_dir:
        marker.write_text("ok")
    return member_dir


def _extract_widevine_from_deb(deb: Path, dest: Path) -> Path | None:
    """Pull opt/google/chrome/WidevineCdm out of the deb (ar + data.tar.xz)."""
    with open(deb, "rb") as f:
        if f.read(8) != b"!<arch>\n":
            sys.stderr.write("[tilion-fortress] not an ar archive\n")
            return None
        payload = None
        while True:
            hdr = f.read(60)
            if len(hdr) < 60:
                break
            name = hdr[0:16].decode("ascii", "replace").rstrip()
            size = int(hdr[48:58].decode("ascii", "replace").strip() or "0")
            body = f.read(size)
            if size % 2:
                f.seek(1, os.SEEK_CUR)
            if name.startswith("data.tar"):
                payload = body
                break
        if not payload:
            sys.stderr.write("[tilion-fortress] deb has no data.tar payload\n")
            return None
        raw = lzma.decompress(payload) if name.endswith(".xz") else payload
        with tarfile.open(fileobj=io.BytesIO(raw)) as t:
            members = [m for m in t.getmembers()
                       if m.name.endswith("WidevineCdm") or "/WidevineCdm/" in m.name]
            if not members:
                sys.stderr.write("[tilion-fortress] deb contains no WidevineCdm payload\n")
                return None
            try:
                t.extractall(dest, members=members, filter="data")
            except TypeError:  # Python < 3.12: no filter kwarg
                t.extractall(dest, members=members)
            # Normalize "./opt/google/chrome/WidevineCdm" -> "WidevineCdm"
            for m in members:
                if m.isdir() and m.name.endswith("WidevineCdm"):
                    got = dest / m.name.lstrip("./")
                    flat = dest / "WidevineCdm"
                    if got != flat and got.is_dir():
                        shutil.move(str(got), flat)
                    if flat.is_dir():
                        return flat
    return None


def ensure_widevine(cache: Path | None = None) -> Path | None:
    """Return a usable CDM directory, or None when no source is available."""
    plat = _platform_key()
    if not plat:
        return None
    for env in ("CLOAKBROWSER_WIDEVINE_CDM", "TILION_WIDEVINE_CDM", "FORTRESS_WIDEVINE_CDM"):
        p = os.environ.get(env)
        if p:
            path = Path(p)
            if _valid_cdm_dir(path, plat):
                return path
            sys.stderr.write(f"[tilion-fortress] {env}={p} is not a valid CDM dir; ignoring\n")
    local = find_local_chrome_cdm(plat)
    if local:
        return local
    if plat == ("linux", "x64"):
        from . import _CACHE
        return fetch_widevine_from_deb(cache or (_CACHE / "widevine"))
    return None


def widevine_flag(cdm_dir: Path) -> str:
    """The engine switch that activates a CDM directory."""
    return f"--uxr-widevine-cdm={cdm_dir}"
