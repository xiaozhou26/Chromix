"""widevine — locate or fetch the Widevine CDM for Chromix.

The engine (patch 0048, args.gn enable_widevine) accepts a component-layout
CDM directory via ``--uxr-widevine-cdm=<dir>``:

    <dir>/manifest.json
    <dir>/_platform_specific/<os>_<arch>/widevinecdm.{dll,so,dylib}

``find_cdm()`` (used automatically at launch) only locates an existing CDM:
  1. ``CLOAKBROWSER_WIDEVINE_CDM`` env (explicit dir; the CloakBrowser name is
     honored for drop-in parity),
  2. an installed Google Chrome — its version directory ships WidevineCdm in
     exactly the required layout, so we point at it directly (no copy),
  3. a CDM previously fetched into the SDK cache (``python -m chromix widevine``).

``ensure_widevine()`` additionally downloads the CDM on Linux x64 by pulling
the stable Chrome .deb (fixed URL) once and extracting WidevineCdm/ into the
cache. Disable all of it at launch with ``CLOAKBROWSER_WIDEVINE=0``.

No Google API keys and no component-updater round trip.
"""
from __future__ import annotations
import io
import lzma
import os
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

__all__ = ["find_cdm", "ensure_widevine", "find_local_chrome_cdm", "fetch_widevine_from_deb"]

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


def fetch_widevine_from_deb(cache: Path, timeout: float = 300) -> Path | None:
    """Download the stable Chrome .deb and extract WidevineCdm/ into `cache`."""
    cache.mkdir(parents=True, exist_ok=True)
    marker = cache / ".widevine-extracted"
    if marker.is_file():
        for d in cache.glob("WidevineCdm"):
            return d
    deb = cache / "google-chrome-stable_amd64.deb"
    if not deb.is_file():
        sys.stderr.write(f"[chromix] downloading {_DEB_URL} ...\n")
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
            sys.stderr.write("[chromix] not an ar archive\n")
            return None
        payload = None
        name = ""
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
            sys.stderr.write("[chromix] deb has no data.tar payload\n")
            return None
        raw = lzma.decompress(payload) if name.endswith(".xz") else payload
        with tarfile.open(fileobj=io.BytesIO(raw)) as t:
            members = [m for m in t.getmembers()
                       if m.name.endswith("WidevineCdm") or "/WidevineCdm/" in m.name]
            if not members:
                sys.stderr.write("[chromix] deb contains no WidevineCdm payload\n")
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


def find_cdm() -> Path | None:
    """Locate an existing CDM without any network access, or None."""
    plat = _platform_key()
    if not plat:
        return None
    explicit = os.environ.get("CLOAKBROWSER_WIDEVINE_CDM")
    if explicit:
        path = Path(explicit)
        if _valid_cdm_dir(path, plat):
            return path
        sys.stderr.write(f"[chromix] CLOAKBROWSER_WIDEVINE_CDM={explicit} "
                         "is not a valid CDM dir; ignoring\n")
    local = find_local_chrome_cdm(plat)
    if local:
        return local
    from ._binary import _CACHE
    cached = _CACHE / "widevine" / "WidevineCdm"
    if cached.is_dir() and _valid_cdm_dir(cached, plat):
        return cached
    return None


def ensure_widevine() -> Path | None:
    """Locate a CDM, fetching it (Linux x64) if none is present yet."""
    found = find_cdm()
    if found:
        return found
    plat = _platform_key()
    if plat == ("linux", "x64"):
        from ._binary import _CACHE
        return fetch_widevine_from_deb(_CACHE / "widevine")
    return None


def widevine_flag(cdm_dir: Path) -> str:
    """The engine switch that activates a CDM directory."""
    return f"--uxr-widevine-cdm={cdm_dir}"
