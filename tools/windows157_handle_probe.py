#!/usr/bin/env python3
"""Native Windows handle-identity experiment; never restore or build Chromium."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import windows157_tools as production

STAT_FIELDS = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
IDENTITY_FIELDS = ("volume_serial", "file_id", "size", "last_write", "creation", "links", "attributes")


class FileTime(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class ByHandleFileInformation(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("creation", FileTime),
                ("access", FileTime), ("write", FileTime), ("volume", wintypes.DWORD),
                ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
                ("links", wintypes.DWORD), ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]


def combined(high: int, low: int) -> int:
    return high << 32 | low


def handle_info(stream) -> dict:
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel.GetFileInformationByHandle
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    query.restype = wintypes.BOOL
    info = ByHandleFileInformation()
    if not query(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"volume_serial": info.volume, "file_id": combined(info.index_high, info.index_low),
            "size": combined(info.size_high, info.size_low),
            "last_write": combined(info.write.high, info.write.low),
            "creation": combined(info.creation.high, info.creation.low),
            "links": info.links, "attributes": info.attributes,
            "last_access": combined(info.access.high, info.access.low)}


def stable_identity(info: dict) -> tuple:
    if (info["attributes"] & (0x400 | 0x10) or info["links"] not in (1, 2)
            or info["size"] <= 0 or info["file_id"] == 0):
        raise ValueError("not an ordinary bounded-link file handle")
    return tuple(info[key] for key in IDENTITY_FIELDS)


def path_info(path: Path) -> dict:
    with production.locked_file(path) as stream:
        return handle_info(stream)


def stat_info(info) -> dict:
    return {name: getattr(info, name) for name in STAT_FIELDS}


def require(value, message):
    if not value:
        raise ValueError(message)


def mutation_child(path: Path, operation: str) -> dict:
    try:
        if operation == "write":
            with path.open("r+b") as stream:
                stream.seek(64)
                stream.write(b"MUTATION")
        elif operation == "delete":
            path.unlink()
        else:
            raise ValueError("unknown child mutation")
        return {"operation": operation, "path": str(path), "blocked": False}
    except OSError as error:
        return {"operation": operation, "path": str(path), "blocked": True,
                "winerror": getattr(error, "winerror", None), "errno": error.errno,
                "exception": type(error).__name__, "message": str(error)}


def attempt_mutation(path: Path, operation: str) -> dict:
    process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", operation,
                              "--path", str(path)], check=True, capture_output=True, text=True, timeout=30)
    return json.loads(process.stdout)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def probe(root: Path, report: dict) -> None:
    require(sys.platform == "win32", "native Windows is required; simulation is not evidence")
    runtime = production.metagen.runtime_info()
    production.metagen.validate_runtime(runtime)
    report["runtime"] = runtime
    require(ctypes.sizeof(ByHandleFileInformation) == 52, "Win32 structure layout mismatch")
    source = root / "out/crubit/x86_64-pc-windows-msvc/release/cc_bindings_from_rs.exe"
    alias = source.parent / "build/cc_bindings_from_rs/b8c8cf717b68bfe0/out/cc_bindings_from_rs.exe"
    alias.parent.mkdir(parents=True)
    shutil.copyfile(sys.executable, alias)
    production.pe_executable(alias)
    os.link(alias, source)
    require(source.stat().st_nlink == 2 and source.samefile(alias), "real dual hardlink creation failed")
    require(production.cargo_aliases(source) == [alias], "production alias ownership rejected probe")
    expected_hash = file_hash(source)
    report["paths"] = {"source": str(source), "alias": str(alias)}
    report["original_sha256"] = expected_hash

    # Record the exact production comparison without treating its failure as a fix.
    try:
        with production.private_cargo_output(source) as private:
            report["production_private_copy"] = {"status": "passed", "sha256": file_hash(private)}
    except (OSError, ValueError) as error:
        report["production_private_copy"] = {"status": "failed", "error": str(error),
                                             "exception": type(error).__name__}
    private = root / "private-copy.exe"
    with production.locked_file(source) as held:
        path_stat = stat_info(source.stat())
        handle_stat = stat_info(os.fstat(held.fileno()))
        before = handle_info(held)
        report["before"] = {"path_stat": path_stat, "fstat": handle_stat,
                            "legacy_differences": {k: {"path": path_stat[k], "handle": handle_stat[k]}
                                                   for k in STAT_FIELDS if path_stat[k] != handle_stat[k]},
                            "held_win32": before, "path_win32": path_info(source), "alias_win32": path_info(alias)}
        identity = stable_identity(before)
        require(before["links"] == 2, "Win32 link count differs from Cargo dual-link contract")
        for name in ("path_win32", "alias_win32"):
            require(stable_identity(report["before"][name]) == identity, "same-API file identity mismatch: " + name)
        with private.open("xb") as destination:
            shutil.copyfileobj(held, destination)
            destination.flush()
            os.fsync(destination.fileno())
        production.pe_executable(private)
        private_info = path_info(private)
        require(private_info["links"] == 1 and private_info["file_id"] != before["file_id"],
                "private copy retained source file identity")
        require(file_hash(private) == expected_hash, "private copy bytes changed")
        report["private_copy"] = {"win32": private_info, "sha256": file_hash(private), "pe": "AMD64-PE32+"}
        report["mutations"] = []
        for path in (source, alias):
            for operation in ("write", "delete"):
                result = attempt_mutation(path, operation)
                report["mutations"].append(result)
                require(result.get("blocked") is True and result.get("winerror") == 32,
                        "expected ERROR_SHARING_VIOLATION for actual child mutation")
        report["after"] = {"held_win32": handle_info(held), "path_win32": path_info(source),
                           "alias_win32": path_info(alias)}
        for info in report["after"].values():
            require(stable_identity(info) == identity, "same-API identity changed under share-deny lock")
        require(file_hash(source) == expected_hash, "source bytes changed under lock")
    result = attempt_mutation(alias, "write")
    report["unlocked_write"] = result
    require(result["blocked"] is False, "mutation test did not actually exercise a writable file")
    require(file_hash(source) != expected_hash, "unlocked alias mutation did not reach source")
    require(file_hash(private) == expected_hash, "private copy still aliases source")
    report["unlocked_identity"] = path_info(source)
    require(stable_identity(report["unlocked_identity"]) != identity,
            "same-API comparison missed an actual post-unlock write")

    # An already-open writable handle must prevent acquisition of a share-deny read lock.
    with alias.open("r+b"):
        try:
            with production.locked_file(source):
                raise ValueError("share-deny lock admitted an existing external writer")
        except OSError as error:
            report["existing_writer_lock_denied"] = {"errno": error.errno,
                                                    "winerror": getattr(error, "winerror", None)}
            require(error.errno == 32 or getattr(error, "winerror", None) == 32,
                    "existing-writer failure was not sharing violation")
    report["same_api_comparison"] = "passed"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--child", choices=("write", "delete"))
    parser.add_argument("--path", type=Path)
    args = parser.parse_args(argv)
    if args.child:
        if not args.path:
            parser.error("child requires --path")
        print(json.dumps(mutation_child(args.path, args.child), sort_keys=True))
        return 0
    if not args.report or not args.directory:
        parser.error("probe requires --report and --directory")
    report = {"schema_version": 1, "status": "failed", "native_windows": sys.platform == "win32",
              "python": sys.version, "executable": sys.executable,
              "workflow": {key: os.environ.get(key) for key in ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
                           "GITHUB_JOB", "GITHUB_WORKFLOW_REF", "ImageOS", "ImageVersion")},
              "probe_sha256": file_hash(Path(__file__)),
              "production_sha256": file_hash(Path(production.__file__))}
    try:
        require(sys.platform == "win32", "native Windows is required; no POSIX substitute")
        require(not args.directory.exists(), "probe directory must be new")
        args.directory.mkdir(parents=True)
        probe(args.directory.resolve(), report)
        report["status"] = "passed"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True, indent=2)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
