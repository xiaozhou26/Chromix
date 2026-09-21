#!/usr/bin/env python3
"""Select a native Ninja without letting an incompatible reader discard restored logs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import stat
import struct
import subprocess
import sys
import tempfile

REPORT = "upstream-cache-ninja.json"
HEADER_LIMIT = 128
PATH_LIMIT = 4096
CANDIDATE_LIMIT = 64
LOG_FAMILIES = {5: (11,), 6: (12,), 7: (13,)}


def host_identity() -> tuple[str, str]:
    system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system(), "unknown")
    machine = platform.machine().lower()
    return system, {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine, machine)


def binary_architectures(path: Path, system: str) -> set[str]:
    """Bound header reads before any restored executable is run."""
    cpus = {"linux": {62: "x64", 183: "arm64"},
            "macos": {0x1000007: "x64", 0x100000C: "arm64"},
            "windows": {0x8664: "x64", 0xAA64: "arm64"}}[system]
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64:
            return set()
        if system == "linux" and header[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", header, 16)[0] in (2, 3):
            cpu = struct.unpack_from("<H", header, 18)[0]
        elif (system == "macos" and header[:4] == b"\xcf\xfa\xed\xfe"
              and struct.unpack_from("<I", header, 12)[0] == 2):
            cpu = struct.unpack_from("<I", header, 4)[0]
        elif system == "macos" and header[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
            count = struct.unpack_from(">I", header, 4)[0]
            if not 0 < count <= 32:
                return set()
            width = 32 if header[:4] == b"\xca\xfe\xba\xbf" else 20
            stream.seek(8)
            entries = stream.read(count * width)
            if len(entries) != count * width:
                return set()
            return {cpus[cpu] for offset in range(0, len(entries), width)
                    if (cpu := struct.unpack_from(">I", entries, offset)[0]) in cpus}
        elif system == "windows" and header[:2] == b"MZ":
            offset = struct.unpack_from("<I", header, 60)[0]
            if not 64 <= offset <= 1024 * 1024:
                return set()
            stream.seek(offset)
            pe = stream.read(6)
            if len(pe) != 6 or pe[:4] != b"PE\0\0":
                return set()
            cpu = struct.unpack_from("<H", pe, 4)[0]
        else:
            return set()
    return {cpus[cpu]} if cpu in cpus else set()


def version_format(version: str) -> int | None:
    match = re.fullmatch(r"1\.(11|12|13)\.[0-9]+(?:\.chromium\.[0-9]+)?", version)
    if match:
        minor = int(match[1])
        return next(log for log, minors in LOG_FAMILIES.items() if minor in minors)
    return None


def probe_version(path: Path, work: Path) -> str:
    # Keep noisy or hung version probes out of the shell capture and build tree.
    with tempfile.TemporaryFile() as output:
        completed = subprocess.run([str(path), "--version"], cwd=work, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, timeout=5, check=False)
        output.seek(0)
        raw = output.read(1025)
    if len(raw) > 1024:
        raise ValueError("version output exceeds 1024 bytes")
    if completed.returncode:
        raise ValueError(f"version probe exited {completed.returncode}: {raw[:128]!r}")
    version = raw.decode("ascii").strip()
    if len(version) > 128 or not re.fullmatch(r"[0-9A-Za-z.+_-]+", version):
        raise ValueError(f"invalid version output: {raw[:128]!r}")
    return version


def candidates(src: Path, system: str):
    name = "ninja.exe" if system == "windows" else "ninja"
    bundled = ("bundled", src / "third_party/ninja" / name)
    if system == "windows":
        yield bundled
    for directory in os.environ.get("PATH", "").split(os.pathsep)[:CANDIDATE_LIMIT]:
        if directory and len(directory) <= PATH_LIMIT:
            path = Path(directory) / name
            if path.is_file():
                yield "PATH", path
    if system != "windows":
        yield bundled


def write_report(work: Path, report: dict) -> None:
    destination = work / REPORT
    if destination.is_symlink():
        raise ValueError("Ninja report is a symlink")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=work, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def select_ninja(work: Path, system: str, arch: str) -> Path:
    work = work.resolve(strict=True)
    src = work / "src"
    report = {"schema_version": 1, "status": "failed", "platform": system, "arch": arch,
              "host": list(host_identity()), "candidates": []}
    try:
        if (system, arch) not in (("linux", "x64"), ("linux", "arm64"),
                                 ("macos", "x64"), ("macos", "arm64"),
                                 ("windows", "x64"), ("windows", "arm64")):
            raise ValueError("unsupported restored build target")
        native_arch = "x64" if system == "windows" else arch
        if (report["host"] != [system, native_arch]
                and (system, arch, *report["host"]) != ("linux", "arm64", "linux", "x64")):
            raise ValueError(f"a native {system} {native_arch} runner is required (Linux ARM64 also supports Linux x64 hosts)")
        host_arch = report["host"][1]
        if not (src / ".chromix-upstream-restored.json").is_file():
            raise ValueError("restored source receipt is required")
        log = src / "out/Default/.ninja_log"
        report["log"] = {"path": "src/out/Default/.ninja_log"}
        if any(path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
               for path in (log, *log.parents)) or not stat.S_ISREG(log.stat().st_mode):
            raise ValueError("restored Ninja log must be a regular, unlinked file")
        with log.open("rb") as stream:
            header = stream.readline(HEADER_LIMIT + 1)
        report["log"].update(header_hex=header[:HEADER_LIMIT].hex(),
                             header_truncated=len(header) > HEADER_LIMIT)
        match = re.fullmatch(rb"# ninja log v([567])\r?\n", header)
        if not match:
            raise ValueError(f"unknown restored Ninja log header: {header[:48]!r}")
        log_format = int(match[1])
        report["log"]["format"] = log_format
        seen = set()
        for origin, candidate in candidates(src, system):
            entry = {"source": origin, "path": str(candidate)[:PATH_LIMIT]}
            try:
                path = candidate.resolve(strict=True)
                if path in seen:
                    continue
                seen.add(path)
                entry["path"] = str(path)[:PATH_LIMIT]
                report["candidates"].append(entry)
                if len(str(path)) > PATH_LIMIT or any(ord(char) < 32 for char in str(path)):
                    raise ValueError("invalid executable path")
                if origin == "bundled" and not path.is_relative_to(src.resolve()):
                    raise ValueError("bundled Ninja resolves outside the restored source")
                if not path.is_file() or (system != "windows" and not os.access(path, os.X_OK)):
                    raise ValueError("candidate is not an executable file")
                architectures = binary_architectures(path, system)
                entry["architectures"] = sorted(architectures)
                if host_arch not in architectures:
                    raise ValueError("candidate header does not match the native platform/architecture")
                version = probe_version(path, work)
                supported = version_format(version)
                entry.update(version=version, log_format=supported)
                if supported != log_format:
                    raise ValueError(f"Ninja {version} is not verified for log v{log_format}")
                entry["status"] = "selected"
                report.update(status="selected", selected=dict(entry))
                write_report(work, report)
                return path
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                if not any(item is entry for item in report["candidates"]):
                    report["candidates"].append(entry)
                entry.update(status="rejected", reason=str(error)[:512])
        families = "/".join(f"1.{minor}.x" for minor in LOG_FAMILIES[log_format])
        raise ValueError(f"no compatible native Ninja for log v{log_format}; requires {families}")
    except (OSError, ValueError, RuntimeError) as error:
        report["reason"] = str(error)[:512]
        write_report(work, report)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--platform", choices=("linux", "macos", "windows"), required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    args = parser.parse_args(argv)
    try:
        path = select_ninja(args.workdir, args.platform, args.arch)
        report = json.loads((args.workdir / REPORT).read_text(encoding="utf-8"))
        print(f"restored Ninja: {report['selected']['version']} for log v{report['log']['format']}; "
              f"report: {str(args.workdir / REPORT)[:256]}", file=sys.stderr)
        print(path)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"restored Ninja refused: {str(error)[:512]}; report: "
              f"{str(args.workdir / REPORT)[:256]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
