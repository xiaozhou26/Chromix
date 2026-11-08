#!/usr/bin/env python3
"""Read-only identity gate for Linux source/output checkpoints, not build success."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import stat
import subprocess
import sys

# verify_patch_stack uses the tools directory for its standalone imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from . import patch_selection, platform_pins, restore_ninja, restore_upstream_cache as restore
    from . import verify_patch_stack
except ImportError:
    import patch_selection
    import platform_pins
    import restore_ninja
    import restore_upstream_cache as restore
    import verify_patch_stack

REPO = Path(__file__).resolve().parents[1]


def regular(root: Path, name: str) -> Path:
    path = restore.safe_path(root, Path(name))
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"checkpoint input is not a regular file: {name}")
    return path


def verify(work: Path, arch: str, *, repo: Path = REPO) -> dict:
    work = restore.local_path(work)
    src = restore.safe_path(work, Path("src"))
    pins = platform_pins.load_pins(repo, "linux")
    key = "|".join(("linux", arch, pins["ChromiumVersion"], pins["UngoogledCommit"],
                    pins["UngoogledLinuxCommit"], patch_selection.preparation_key(repo, "linux", "posix")))
    if regular(src, ".chromix-source-ready").read_text().strip() != key:
        raise ValueError("checkpoint source preparation identity does not match current pins")
    if patch_selection.source_version(src) != pins["ChromiumVersion"]:
        raise ValueError("checkpoint source version does not match current pins")
    restored = (src / restore.MARKER).exists() or (src / restore.MARKER).is_symlink()
    receipt = restore.verify_restored(work, "linux", arch, repo=repo) if restored else None
    out = restore.safe_path(src, Path("out/Default" if restored else "out/Chromix"))
    args = regular(out, "args.gn")
    restore.importer.validate_gn_target(restore.gn_assignments_last_wins(args), "linux", arch)
    regular(out, "build.ninja")
    with regular(out, ".ninja_log").open("rb") as stream:
        header = stream.readline(restore_ninja.HEADER_LIMIT + 1)
    match = re.fullmatch(rb"# ninja log v([567])\r?\n", header)
    if not match:
        raise ValueError("checkpoint Ninja log identity is unsupported")
    log_format = int(match[1])
    with regular(out, ".ninja_deps").open("rb") as stream:
        if stream.read(16) != b"# ninjadeps\n\x04\x00\x00\x00":
            raise ValueError("checkpoint Ninja deps identity is unsupported")
    host = restore_ninja.host_identity()
    if host not in (("linux", arch), ("linux", "x64")):
        raise ValueError("checkpoint requires a supported Linux host/target pair")
    if restored:
        report = json.loads(regular(work, restore_ninja.REPORT).read_text())
        if (report.get("schema_version") != 1 or report.get("status") != "selected"
                or report.get("platform") != "linux" or report.get("arch") != arch
                or report.get("host") != list(host) or report.get("log", {}).get("format") != log_format):
            raise ValueError("checkpoint Ninja selection identity does not match the tree")
        selected = report["selected"]
        candidates = [(selected["path"], selected["version"])]
    else:
        candidates = [(str(path.resolve()), None) for _, path in restore_ninja.candidates(src, "linux")]
    ninja = None
    for name, expected_version in candidates:
        path = Path(name)
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError("checkpoint Ninja path is not absolute and resolved")
        if host[1] not in restore_ninja.binary_architectures(path, "linux"):
            continue
        version = restore_ninja.probe_version(path, work)
        if restore_ninja.version_format(version) == log_format and expected_version in (None, version):
            ninja = {"path": str(path), "version": version, "log_format": log_format, "host": list(host)}
            break
    if ninja is None:
        raise ValueError("checkpoint Ninja executable/version does not match its log identity")
    core = restore.safe_path(work, Path("tooling/ungoogled-chromium"))
    tooling = restore.safe_path(work, Path("tooling/ungoogled-chromium-portablelinux"))
    for directory, commit in ((core, pins["UngoogledCommit"]), (tooling, pins["UngoogledLinuxCommit"])):
        actual = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        if actual != commit:
            raise ValueError("checkpoint tooling provenance does not match current pins")
    source = verify_patch_stack.verify(src, repo, core=core, tooling=tooling, platform="linux")
    if source["status"] != "verified":
        raise ValueError("checkpoint source is not verified")
    return {"schema_version": 1, "status": "verified", "platform": "linux", "arch": arch,
            "source_verified": True, "source": source, "provenance": receipt or {"preparation_key": key},
            "output": str(out.relative_to(work)), "ninja": ninja,
            "qualification": "Resumable source/output identity only; compilation/runtime may have failed."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(verify(args.workdir, args.arch), sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, restore.LocalError,
            verify_patch_stack.arp.ApplyError, subprocess.SubprocessError) as error:
        print(f"Linux checkpoint refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
