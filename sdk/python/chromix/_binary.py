"""Binary management for chromix: download, verify, cache the stealth Chromium bundle.

Detects the platform, downloads the matching bundle from the GitHub Release,
verifies it against SHA256SUMS, and caches it under ``~/.cache/chromix``.
"""
from __future__ import annotations
import hashlib
import os
import platform
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

_REPO = "xiaozhou26/Chromix"
# Two release channels. "stable" = Chromium 149 (matches the version the mass of
# real users run). "latest" = 152 (newest engine). See build/versions.txt.
_CHANNELS = {
    "stable": {"tag": "v149.0.7827.200"},
    "latest": {"tag": "v152.0.7977.75"},
}
_CACHE = Path(os.environ.get("CHROMIX_CACHE_DIR",
                             Path.home() / ".cache" / "chromix"))


def _host(tag: str) -> str:
    return os.environ.get("CHROMIX_DOWNLOAD_HOST",
                          f"https://github.com/{_REPO}/releases/download/{tag}")


# platform key -> (release asset, archive kind, launcher relative path)
_ASSETS = {
    "linux-x64":  ("chromix-linux-x64.tar.gz", "tar", "chromix/chromix"),
    "win-x64":    ("chromix-win-x64.zip",      "zip", "chromix/chromix.cmd"),
    "mac-arm64":  ("chromix-mac-arm64.tar.gz", "tar", "chromix/chromix"),
    "mac-x64":    ("chromix-mac-x64.tar.gz",   "tar", "chromix/chromix"),
}


def resolve_platform() -> str | None:
    sysname, mach = platform.system(), platform.machine().lower()
    if sysname == "Linux" and mach in ("x86_64", "amd64"):
        return "linux-x64"
    if sysname == "Windows" and mach in ("amd64", "x86_64"):
        return "win-x64"
    if sysname == "Darwin":
        return "mac-arm64" if mach in ("arm64", "aarch64") else "mac-x64"
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _expected_sha(asset: str, host: str) -> str | None:
    """Fetch SHA256SUMS from the release and return the hash for `asset`."""
    try:
        with urllib.request.urlopen(f"{host}/SHA256SUMS", timeout=30) as r:
            for line in r.read().decode().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("*") == asset:
                    return parts[0].lower()
    except Exception:
        return None
    return None


def _download(plat: str, host: str, tag: str) -> Path:
    """Ensure the bundle for `plat` is present + verified; return the launcher path."""
    asset, kind, launcher_rel = _ASSETS[plat]
    root = _CACHE / tag / plat   # cache per release tag so channels don't collide
    launcher = root / launcher_rel
    if launcher.exists():
        return launcher
    root.mkdir(parents=True, exist_ok=True)
    archive = root / asset
    url = f"{host}/{asset}"
    sys.stderr.write(f"[chromix] downloading {url} ...\n")
    urllib.request.urlretrieve(url, archive)

    expected = _expected_sha(asset, host)
    if expected:
        actual = _sha256(archive)
        if actual != expected:
            archive.unlink(missing_ok=True)
            raise RuntimeError(f"SHA256 mismatch for {asset}: expected {expected}, got {actual}")
        sys.stderr.write("[chromix] SHA256 verified\n")
    else:
        sys.stderr.write("[chromix] WARNING: no SHA256SUMS published; skipping verification\n")

    if kind == "tar":
        with tarfile.open(archive) as t:
            t.extractall(root)
    else:
        with zipfile.ZipFile(archive) as z:
            z.extractall(root)
    archive.unlink(missing_ok=True)
    if launcher.exists() and not launcher.name.endswith(".cmd"):
        launcher.chmod(0o755)
    if not launcher.exists():
        raise RuntimeError(f"bundle extracted but launcher missing: {launcher}")
    return launcher
