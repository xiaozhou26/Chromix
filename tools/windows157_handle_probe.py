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
IDENTITY_FIELDS = production.IDENTITY_FIELDS
ByHandleFileInformation = production.ByHandleFileInformation
combined = production.combined
handle_info = production.windows_handle_info
stable_identity = production.windows_stable_identity
path_info = production.windows_path_info


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


def win32_mutation(path: Path, operation: str) -> dict:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    result = {"operation": operation, "path": str(path), "api": "Win32", "attempted": True}
    if operation == "delete":
        delete = kernel.DeleteFileW
        delete.argtypes = [wintypes.LPCWSTR]
        delete.restype = wintypes.BOOL
        success = bool(delete(str(path)))
        return {**result, "blocked": not success, "winerror": None if success else ctypes.get_last_error()}
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x40000000, 7, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        return {**result, "blocked": True, "winerror": ctypes.get_last_error(), "stage": "CreateFileW"}
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    try:
        seek = kernel.SetFilePointerEx
        seek.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD]
        seek.restype = wintypes.BOOL
        if not seek(handle, 64, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        write = kernel.WriteFile
        write.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        write.restype = wintypes.BOOL
        payload = ctypes.create_string_buffer(b"MUTATION")
        count = wintypes.DWORD()
        success = bool(write(handle, payload, 8, ctypes.byref(count), None))
        return {**result, "blocked": not success, "winerror": None if success else ctypes.get_last_error(),
                "stage": "WriteFile", "bytes_written": count.value}
    finally:
        close(handle)


def denial_verified(result: dict) -> bool:
    python = result["python"]
    raw = result["win32"]
    return (python.get("blocked") is True and python.get("exception") == "PermissionError"
            and python.get("errno") == 13 and python.get("winerror") in (None, 5, 32)
            and raw.get("attempted") is True and raw.get("blocked") is True and raw.get("winerror") == 32)


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

    # Production copy and publication must pass before the independent lock experiment.
    try:
        with production.private_cargo_output(source) as private:
            report["production_private_copy"] = {"status": "passed", "sha256": file_hash(private)}
    except (OSError, ValueError) as error:
        report["production_private_copy"] = {"status": "failed", "error": str(error),
                                             "exception": type(error).__name__}
    require(report["production_private_copy"]["status"] == "passed", "production private copy failed")
    installed = root / "installed/cc_bindings_from_rs.exe"
    report["production_atomic_install"] = production.publish_cargo_output(source, installed)
    require(file_hash(installed) == expected_hash and installed.stat().st_nlink == 1
            and not installed.samefile(source), "production atomic install identity mismatch")
    try:
        production.publish_cargo_output(source, installed)
    except ValueError as error:
        report["existing_destination_rejected"] = str(error)
    else:
        raise ValueError("production install overwrote an existing destination")
    report["negative_cases"] = []
    outside = root / "external.exe"
    os.link(source, outside)
    try:
        with production.private_cargo_output(source):
            raise AssertionError("third link unexpectedly admitted")
    except ValueError as error:
        report["negative_cases"].append({"case": "third_link", "rejected": True, "error": str(error)})
    finally:
        outside.unlink()
    alias.unlink()
    os.link(source, outside)
    try:
        with production.private_cargo_output(source):
            raise AssertionError("external-only alias unexpectedly admitted")
    except ValueError as error:
        report["negative_cases"].append({"case": "external_alias", "rejected": True, "error": str(error)})
    finally:
        outside.unlink()
        os.link(source, alias)
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
                require(all(result[api].get("path") == str(path) and result[api].get("operation") == operation
                            for api in ("python", "win32")), "child mutation reported another operation or path")
                require(denial_verified(result), "Python denial not corroborated by raw Win32 sharing violation")
                require(file_hash(source) == expected_hash and source.samefile(alias)
                        and stable_identity(handle_info(held)) == identity
                        and stable_identity(path_info(source)) == identity
                        and stable_identity(path_info(alias)) == identity,
                        "actual child mutation changed bytes, names or identity")
                result["unchanged_after_attempt"] = True
        report["after"] = {"held_win32": handle_info(held), "path_win32": path_info(source),
                           "alias_win32": path_info(alias)}
        for info in report["after"].values():
            require(stable_identity(info) == identity, "same-API identity changed under share-deny lock")
        require(file_hash(source) == expected_hash, "source bytes changed under lock")
    result = attempt_mutation(alias, "write")
    report["unlocked_write"] = result
    require(result["python"]["blocked"] is False and result["win32"]["blocked"] is False
            and result["win32"]["bytes_written"] == 8,
            "mutation test did not actually exercise a writable file")
    require(file_hash(source) != expected_hash, "unlocked alias mutation did not reach source")
    require(file_hash(private) == expected_hash and file_hash(installed) == expected_hash,
            "private or published copy still aliases source")
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
    report["negative_cases"].append({"case": "unlocked_source_tamper", "detected": True,
                                      "installed_copy_unchanged": file_hash(installed) == expected_hash})
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
        result = mutation_child(args.path, args.child)
        if sys.platform == "win32":
            result = {"python": result, "win32": win32_mutation(args.path, args.child)}
        print(json.dumps(result, sort_keys=True))
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
