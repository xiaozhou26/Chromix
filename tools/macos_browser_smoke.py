#!/usr/bin/env python3
"""Capture bounded macOS browser smoke evidence without changing CI browser flags."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import datetime as dt
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time

VERSION_TIMEOUT = 30
HEADLESS_TIMEOUT = 60
MAX_OUTPUT_BYTES = 1024 * 1024
KILL_TIMEOUT = 5
DIAGNOSTIC_TIMEOUT = 3
DOM_MARKER = "<p>chromix-smoke-ok</p>"
SMOKE_URL = "data:text/html," + DOM_MARKER


def _capture(command, *, timeout, limit, before_kill=None, env=None,
             kill_timeout=KILL_TIMEOUT):
    started = time.monotonic()
    result = {
        "command": list(command), "cwd": str(Path.cwd()),
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "timeout_seconds": timeout, "status": "launch_error", "returncode": None,
    }
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    process = None
    captured = 0
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, start_new_session=True, env=env)
        result.update(pid=process.pid, process_group=process.pid, status="running")
        deadline = started + timeout
        with selectors.DefaultSelector() as selector:
            for name in streams:
                pipe = getattr(process, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map() and result["status"] == "running":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result["status"] = "timeout"
                    break
                for key, _ in selector.select(remaining):
                    try:
                        chunk = os.read(key.fileobj.fileno(), min(8192, limit - captured + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = limit - captured
                    streams[key.data].extend(chunk[:available])
                    captured += min(len(chunk), available)
                    if len(chunk) > available:
                        result["status"] = "output_limit"
                        break
            if result["status"] == "running":
                try:
                    code = process.wait(timeout=max(0, deadline - time.monotonic()))
                    result["status"] = "passed" if code == 0 else "nonzero_exit"
                except subprocess.TimeoutExpired:
                    result["status"] = "timeout"
    except OSError as error:
        result.update(status="launch_error" if process is None else "capture_error",
                      error=str(error))
    finally:
        result["elapsed_seconds"] = time.monotonic() - started
        if process is not None:
            result["returncode"] = process.poll()
            try:
                if result["status"] == "timeout" and before_kill is not None:
                    try:
                        result["diagnostics"] = before_kill(process.pid, limit - captured)
                    except Exception as error:
                        result["diagnostics"] = {"status": "failed", "error": str(error)}
            finally:
                cleanup_errors = []
                try:
                    # The group can outlive its leader and keep captured pipes open.
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    cleanup_errors.append(str(error))
                try:
                    process.wait(timeout=kill_timeout)
                except (OSError, subprocess.TimeoutExpired) as error:
                    cleanup_errors.append(str(error))
                finally:
                    process.stdout.close()
                    process.stderr.close()
                result["returncode_after_cleanup"] = process.returncode
                if cleanup_errors:
                    result["cleanup_errors"] = cleanup_errors
                    if result["status"] == "passed":
                        result["status"] = "cleanup_error"
        result["captured_bytes"] = captured
        result["total_elapsed_seconds"] = time.monotonic() - started
    return result, streams


def _evidence_files(stack, output, name):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("invalid command evidence name")
    paths = {stream: Path(output) / f"{name}.{stream}.log" for stream in ("stdout", "stderr")}
    paths["result"] = Path(output) / f"{name}.json"
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise FileExistsError(f"command evidence already exists: {name}")
    files = {key: stack.enter_context(path.open("xb")) for key, path in paths.items()}
    return paths, files


def _save_result(paths, files, result, streams):
    for stream in ("stdout", "stderr"):
        files[stream].write(streams[stream])
        result[stream + "_path"] = str(paths[stream])
    files["result"].write((json.dumps(result, indent=2) + "\n").encode("utf-8"))


def _process_tree(data, pid):
    rows = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        fields = line.split(None, 4)
        if len(fields) == 5:
            try:
                rows.append((int(fields[0]), int(fields[1]), int(fields[2]), line))
            except ValueError:
                continue
    selected = {pid} | {child for child, _, group, _ in rows if group == pid}
    while True:
        descendants = {child for child, parent, _, _ in rows if parent in selected}
        if descendants <= selected:
            break
        selected.update(descendants)
    return "".join(line + "\n" for child, _, _, line in rows if child in selected).encode("utf-8")


def _macos_timeout_diagnostics(pid, output, name, limit):
    if sys.platform != "darwin":
        return {"status": "not_supported"}
    deadline = time.monotonic() + DIAGNOSTIC_TIMEOUT
    result = {"status": "attempted", "commands": {}}
    commands = (
        ("ps", ["/bin/ps", "-axo", "pid=,ppid=,pgid=,stat=,comm="], 0.5, 64 * 1024),
        ("sample", ["/usr/bin/sample", str(pid), "1", "10", "-mayDie",
                    "-file", "/dev/stdout"], 2, 256 * 1024),
    )
    for label, command, timeout, cap in commands:
        remaining = deadline - time.monotonic() - 0.2
        if remaining <= 0 or limit <= 0:
            result["commands"][label] = {"status": "skipped", "reason": "diagnostic_budget"}
            continue
        with ExitStack() as stack:
            paths, files = _evidence_files(stack, output, f"{name}-{label}")
            evidence, streams = _capture(
                command, timeout=min(timeout, remaining), limit=min(cap, limit),
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, kill_timeout=0.2)
            if label == "ps":
                streams["stdout"] = _process_tree(streams["stdout"], pid)
            # Decoding a partial multibyte ps line must not expand the byte budget.
            available = min(cap, limit)
            for stream in ("stdout", "stderr"):
                streams[stream] = streams[stream][:available]
                available -= len(streams[stream])
            evidence["captured_bytes"] = sum(map(len, streams.values()))
            limit -= evidence["captured_bytes"]
            _save_result(paths, files, evidence, streams)
            result["commands"][label] = evidence
    return result


def run_command(command, output, name, *, timeout, max_output_bytes=MAX_OUTPUT_BYTES,
                expected_version=None, marker=None):
    """Run a POSIX fixture/browser in the caller's cwd and exclusively save evidence."""
    if timeout <= 0 or not 0 < max_output_bytes <= MAX_OUTPUT_BYTES:
        raise ValueError("timeout must be positive and output limit must be at most 1 MiB")
    with ExitStack() as stack:
        paths, files = _evidence_files(stack, output, name)
        result, streams = _capture(
            [os.fspath(arg) for arg in command], timeout=timeout, limit=max_output_bytes,
            before_kill=lambda pid, limit: _macos_timeout_diagnostics(pid, output, name, limit))
        stdout = streams["stdout"].decode("utf-8", errors="replace")
        if expected_version is not None:
            result["version_matches"] = expected_version in stdout.split()
            if result["status"] == "passed" and not result["version_matches"]:
                result["status"] = "version_mismatch"
        if marker is not None:
            result["marker_present"] = marker in stdout
            if result["status"] == "passed" and not result["marker_present"]:
                result["status"] = "marker_missing"
        _save_result(paths, files, result, streams)
    return result


def run_smoke(launcher, output, profile, chromium_version, *,
              version_timeout=VERSION_TIMEOUT, headless_timeout=HEADLESS_TIMEOUT):
    """Own a new profile, preserve caller cwd/profile spelling, and save report.json."""
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", chromium_version):
        raise ValueError("expected a complete four-component Chromium version")
    if version_timeout <= 0 or headless_timeout <= 0:
        raise ValueError("smoke timeouts must be positive")
    launcher = os.path.abspath(launcher)
    profile_argument = os.fspath(profile)
    profile_path, output = Path(profile).absolute(), Path(output).absolute()
    if profile_path.exists() or profile_path.is_symlink():
        raise FileExistsError("profile must be a new directory; refusing to remove existing data")
    if output.resolve().is_relative_to(profile_path.resolve()):
        raise ValueError("output must not be inside the disposable profile")
    if output.is_symlink() or output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FileExistsError("output must be a new or empty directory; refusing to overwrite evidence")
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report = {
        "status": "failed", "passed": False, "cwd": str(Path.cwd()),
        "launcher": launcher, "profile": profile_argument, "chromium_version": chromium_version,
        "version": None, "headless": None, "profile_cleaned": False,
    }
    with (output / "report.json").open("x", encoding="utf-8") as report_file:
        owned_profile = False
        try:
            profile_path.mkdir(mode=0o700, parents=True, exist_ok=False)
            owned_profile = True
            report["version"] = run_command(
                [launcher, "--version"], output, "version", timeout=version_timeout,
                expected_version=chromium_version)
            command = [launcher, "--headless", "--disable-gpu", "--no-first-run",
                       "--no-default-browser-check", f"--user-data-dir={profile_argument}",
                       "--dump-dom", SMOKE_URL]
            if report["version"]["status"] == "passed":
                report["headless"] = run_command(
                    command, output, "headless", timeout=headless_timeout, marker=DOM_MARKER)
            else:
                with ExitStack() as stack:
                    paths, files = _evidence_files(stack, output, "headless")
                    report["headless"] = {
                        "status": "not_run", "reason": "version_failed", "command": command,
                        "cwd": str(Path.cwd()), "timeout_seconds": headless_timeout,
                        "returncode": None, "captured_bytes": 0,
                    }
                    _save_result(paths, files, report["headless"], {"stdout": b"", "stderr": b""})
        except (OSError, ValueError) as error:
            report["error"] = str(error)
        finally:
            if owned_profile:
                try:
                    shutil.rmtree(profile_path)
                    report["profile_cleaned"] = True
                except OSError as error:
                    report["profile_cleanup_error"] = str(error)
            report["passed"] = (
                "error" not in report and report["profile_cleaned"]
                and all(report[key] is not None and report[key]["status"] == "passed"
                        for key in ("version", "headless")))
            report["status"] = "passed" if report["passed"] else "failed"
            report["elapsed_seconds"] = time.monotonic() - started
            json.dump(report, report_file, indent=2)
            report_file.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--chromium-version", required=True)
    args = parser.parse_args(argv)
    try:
        report = run_smoke(args.launcher, args.output, args.profile, args.chromium_version)
    except (OSError, ValueError) as error:
        print(f"macOS browser smoke failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
