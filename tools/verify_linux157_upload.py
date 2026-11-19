#!/usr/bin/env python3
"""Read-only Linux157 checkpoint and packed-source admission before upload."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

import linux157_source as source
import patch_selection
import restore_ninja
import verify_patch_stack
from fingerprint_acceptance import check_source
from verify_snapshot_tar import SnapshotTarInfo
from restore_upstream_cache import gn_assignments_last_wins
from import_upstream_cache import validate_gn_target


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate(work: Path, arch: str, repo: Path = source.ROOT) -> dict:
    work = work.absolute()
    if work.resolve(strict=True) != work or work.is_symlink():
        raise ValueError("upload workdir must be a resolved directory")
    src = work / "src"
    admission = source.checkpoint(work, arch, repo)
    if patch_selection.source_version(src) != source.VERSION:
        raise ValueError("Linux157 upload source version mismatch")
    receipt = source.verify_receipt(src, repo)
    core = work / "tooling/ungoogled-chromium"
    tooling = work / "tooling/ungoogled-chromium-portablelinux"
    manifest = source.load_manifest(repo)
    expected = receipt.get("substituted_outputs") if (src / ".chromix-domain-substituted").exists() else receipt["prepared_outputs"]
    files = {"src/" + name: value for name, value in expected.items()}
    for name in manifest["toolchain_inputs"]:
        files["src/" + name] = sha(source.regular(src, name))
    files.update({"tooling/ungoogled-chromium/" + name: value for name, value in manifest["core_inputs"].items()})
    files.update({"tooling/ungoogled-chromium-portablelinux/" + name: value
                  for name, value in manifest["platform_inputs"].items()})
    files.update({"tooling/ungoogled-chromium-portablelinux/patches/" + entry["upstream_path"]:
                  entry["upstream_sha256"] for entry in manifest["patches"]})
    files["tooling/ungoogled-chromium-portablelinux/patches/series"] = manifest["upstream_series_sha256"]
    for checkout in (core, tooling):
        metadata = checkout / ".git"
        if metadata.is_symlink() or not metadata.is_dir():
            raise ValueError("checkpoint tooling must retain its real Git directory")
        for path in metadata.rglob("*"):
            if path.is_symlink():
                raise ValueError("linked checkpoint Git metadata")
            if path.is_file():
                name = path.relative_to(work).as_posix()
                files[name] = sha(source.regular(work, name))
    for name in ("src/" + source.RECEIPT, "src/.chromix-source-ready", "src/chrome/VERSION"):
        files[name] = sha(source.regular(work, name))
    final = "fingerprint-diagnostics/source-final.json"
    source_check = None
    if (work / final).exists() or (work / final).is_symlink():
        source.regular(work, final)
        inputs, patches = verify_patch_stack.load_stack(repo, platform="linux", src=src, core=core)
        targets = {entry[0] for _, _, entries in patches for entry in entries}
        source_check = check_source(work / final, src, inputs, targets)
        report = json.loads((work / final).read_bytes())
        files.update({"src/" + name: value for name, value in report["outputs"].items()})
        files[final] = source_check["sha256"]
    else:
        files[final] = None
    out = src / "out/Chromix"
    state_names = ("args.gn", "build.ninja", ".ninja_log", ".ninja_deps")
    present = {name for name in state_names if (out / name).exists() or (out / name).is_symlink()}
    ninja = None
    if present:
        if source_check is None:
            raise ValueError("generated output requires current source-final.json")
        if "args.gn" not in present:
            raise ValueError("Linux157 output has no GN target identity")
        args = source.regular(out, "args.gn")
        validate_gn_target(gn_assignments_last_wins(args), "linux", arch)
        if present & {".ninja_log", ".ninja_deps"} and "build.ninja" not in present:
            raise ValueError("Ninja state has no build graph")
        for name in present:
            files["src/out/Chromix/" + name] = sha(source.regular(out, name))
        if ".ninja_deps" in present:
            with (out / ".ninja_deps").open("rb") as stream:
                if stream.read(16) != b"# ninjadeps\n\x04\x00\x00\x00":
                    raise ValueError("invalid Linux157 Ninja deps identity")
        if "build.ninja" in present:
            selected = shutil.which("ninja")
            if not selected:
                raise ValueError("Linux157 checkpoint requires the current Ninja executable")
            binary = Path(selected).resolve(strict=True)
            if restore_ninja.host_identity() != ("linux", "x64") or "x64" not in restore_ninja.binary_architectures(binary, "linux"):
                raise ValueError("Linux157 Ninja must be native Linux x64")
            version = restore_ninja.probe_version(binary, work)
            log_format = restore_ninja.version_format(version)
            if log_format is None:
                raise ValueError("unsupported Linux157 Ninja version")
            if ".ninja_log" in present:
                with (out / ".ninja_log").open("rb") as stream:
                    header = stream.readline(restore_ninja.HEADER_LIMIT + 1)
                if header.rstrip(b"\r\n") != f"# ninja log v{log_format}".encode():
                    raise ValueError("Linux157 Ninja log/compiler identity mismatch")
            ninja = {"path": str(binary), "sha256": sha(binary), "version": version,
                     "log_format": log_format, "host": ["linux", "x64"]}
    for name in set(state_names) - present:
        files["src/out/Chromix/" + name] = None
    # Recheck source/provenance after reading output identities; never publish a mixed view.
    if source.checkpoint(work, arch, repo) != admission:
        raise ValueError("Linux157 checkpoint changed during upload validation")
    for name, expected_hash in files.items():
        path = work / name
        if expected_hash is None:
            if path.exists() or path.is_symlink():
                raise ValueError("unexpected upload source file: " + name)
        elif sha(source.regular(work, name)) != expected_hash:
            raise ValueError("upload input changed: " + name)
    if ninja and sha(Path(ninja["path"])) != ninja["sha256"]:
        raise ValueError("Linux157 Ninja changed during upload validation")
    return {"schema_version": 1, "status": "verified", "arch": arch,
            "checkpoint": admission, "source_final": source_check, "ninja": ninja,
            "files": files, "runtime_verified": False,
            "qualification": "checkpoint upload admission only, not compilation or native runtime success"}


def verify_archive(stream, report: dict) -> None:
    if (report.get("schema_version") != 1 or report.get("status") != "verified"
            or not isinstance(report.get("files"), dict) or not report["files"]
            or report.get("runtime_verified") is not False):
        raise ValueError("invalid Linux157 upload admission report")
    expected = report["files"]
    seen = set()
    try:
        with tarfile.open(fileobj=stream, mode="r|", bufsize=1024 * 1024,
                          tarinfo=SnapshotTarInfo) as archive:
            for member in archive:
                name = member.name.removeprefix("./")
                if name in expected:
                    if name in seen or expected[name] is None or not member.isfile():
                        raise ValueError("invalid or unexpected packed identity: " + name)
                    value = hashlib.sha256()
                    payload = archive.extractfile(member)
                    while block := payload.read(1024 * 1024):
                        value.update(block)
                    if value.hexdigest() != expected[name]:
                        raise ValueError("packed checkpoint differs from validated live source: " + name)
                    seen.add(name)
                archive.members.clear()
        missing = {name for name, value in expected.items() if value is not None} - seen
        if missing:
            raise ValueError("packed checkpoint missing validated inputs: " + ", ".join(sorted(missing)))
    finally:
        while stream.read(1024 * 1024):
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--arch", choices=("x64", "arm64"))
    parser.add_argument("--repo", type=Path, default=source.ROOT)
    parser.add_argument("--archive-report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.archive_report:
            verify_archive(sys.stdin.buffer, json.loads(args.archive_report.read_bytes()))
        elif args.workdir and args.arch:
            print(json.dumps(validate(args.workdir, args.arch, args.repo), sort_keys=True))
        else:
            parser.error("workdir/arch or archive-report is required")
    except (OSError, ValueError, RuntimeError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
        print("Linux157 upload checkpoint refused: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
