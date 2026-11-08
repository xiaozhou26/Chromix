#!/usr/bin/env python3
"""Install exact macOS npm packages without running npm or lifecycle scripts."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error

try:
    from . import macos_restored_generators as generators
    from . import install_linux_generators as downloads_api
    from . import prepare_restored_build as prepare
    from . import restore_upstream_cache as restore
except ImportError:
    import macos_restored_generators as generators
    import install_linux_generators as downloads_api
    import prepare_restored_build as prepare
    import restore_upstream_cache as restore


def ensure_archive(downloads: Path, key: str, *, download=False) -> Path:
    entry = generators.ARCHIVES[key]
    package, version = entry["package"], entry["version"]
    filename = package.rsplit("/", 1)[-1] + "-" + version + ".tgz"
    if entry["url"] != f"https://registry.npmjs.org/{package}/-/{filename}":
        raise ValueError("untrusted macOS generator archive endpoint")
    downloads = restore.local_path(downloads)
    path = downloads / filename
    if downloads_api.archive_identity(path, entry["sha256"]):
        return path
    if not download or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("missing exact macOS generator archive; populate offline cache or use --download in GitHub Actions")
    data = downloads_api.fetch_archive(entry["url"], entry["sha256"])
    generators.archive_files(data, key)
    restore.local_path(downloads).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mac154-download-", dir=downloads) as temporary:
        staged = Path(temporary) / filename
        staged.write_bytes(data)
        restore.local_path(downloads)
        os.link(staged, path)
        staged.unlink()
    downloads_api.archive_identity(path, entry["sha256"])
    return path


def install(workdir: Path, arch: str, downloads: Path, *, repo: Path = prepare.ROOT,
            download=False) -> dict:
    workdir, repo = restore.local_path(workdir), restore.local_path(repo)
    receipt = restore.verify_restored(workdir, "macos", arch, repo=repo)
    src = workdir / "src"
    receipt_bytes = generators.safe.read_verified(src, restore.MARKER)
    generators.check_pins(repo)
    platform, host = prepare.host_identity()
    if platform != "macos" or host != arch:
        raise ValueError("macOS generator installation requires the native target host")
    state = generators.prepare(src, host_arch=host, repo=repo)
    if not state["install_needed"]:
        return {"status": "already_installed", "source_identity": receipt["identity"], "generators": state}
    files = {}
    for key in generators.package_keys(host):
        if state["packages"][key] is None:
            path = ensure_archive(downloads, key, download=download)
            data = generators.safe.read_verified(path.parent, path.name)
            files[key] = generators.archive_files(data, key)
    if (generators.safe.read_verified(src, restore.MARKER) != receipt_bytes
            or restore.verify_restored(workdir, "macos", arch, repo=repo) != receipt):
        raise ValueError("restored receipt changed during macOS generator provisioning")
    if generators.prepare(src, host_arch=host, repo=repo) != state:
        raise ValueError("macOS generator inputs changed during provisioning")
    for key, payloads in files.items():
        generators.install_package(src, key, payloads)
    result = generators.prepare(src, host_arch=host, repo=repo)
    if result["install_needed"]:
        raise ValueError("incomplete macOS generator installation")
    return {"status": "installed", "source_identity": receipt["identity"], "generators": result}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=prepare.ROOT)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--downloads", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = install(args.workdir, args.arch, args.downloads, repo=args.repo, download=args.download)
    except (OSError, ValueError, restore.Miss, restore.LocalError, urllib.error.URLError) as error:
        print(f"macOS generator installation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
