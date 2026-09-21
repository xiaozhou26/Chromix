#!/usr/bin/env python3
"""Fetch pinned upstream snapshots without building or executing their contents.

The default extraction scope for every supported target is ``source-and-objects``:
all files below the pinned source roots are retained, including source, ``out``,
object/generated files, and Ninja state.  ``ToolchainSelection`` remains available
for compatibility with older callers and tests, but is not used by ``fetch``.
Extraction validates traversal, escaping, links, case collisions on case-folding
platforms, and platform-specific path syntax.  POSIX targets may retain literal
backslashes; Windows archives must use strict POSIX-style archive names. Absolute
symlinks below the target's fixed upstream source root are rewritten as relative
links only when their resolved targets exist in the archive. Other absolute links
are omitted and recorded. The 300 GiB limit and disk-headroom checks still apply.

GH_TOKEN authenticates GitHub API requests only. Availability/validation failures
write a miss and exit zero; invalid arguments or destination paths exit nonzero.
Schema 1 sources default to available; ``available: false`` requires explicit
platform identity and forbids run/artifact fields. All sources remain validated.
Windows ARM64 may pin a distinct run/workflow and a successful producer checkpoint;
only that checkpoint permits an in-progress attempt, with verified creation times.
Linux/macOS require a host zstd executable. The destination must be dedicated to
this tool; result.json records ownership and the absolute source path on a hit.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import http.client
import json
import os
import posixpath
import re
import shutil
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tarfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections import deque
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

try:
    from .platform_pins import PinError, load_pins, load_shared_pins
except ImportError:
    from platform_pins import PinError, load_pins, load_shared_pins

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "build/upstream-cache.json"
OWNER = "chromix-upstream-cache-v1"
API = "https://api.github.com"
CHUNK = 1024 * 1024
TIMEOUT = 60
DOWNLOAD_SECONDS = 45 * 60
ATTEMPTS = 3
MAX_EXTRACTED = 300 * 1024**3
MAX_SELECTED = 30 * 1024**3
DISK_HEADROOM = 4 * 1024**3
SOURCE_SCOPE = "source-and-objects"
TOOLCHAIN_SCOPE = "toolchains-and-args"
PROGRESS_SECONDS = 30
MAX_MEMBERS = 3_000_000
SOURCES = {
    "linux": ("portablelinux", "UngoogledLinuxCommit", ["build/src"]),
    "macos": ("macos", "UngoogledMacOSCommit", ["src"]),
    "windows": ("windows", "UngoogledWindowsCommit", ["src", "build/src"]),
}
UNAVAILABLE_SOURCE_FIELDS = {
    "available", "chromium_version", "ungoogled_commit", "repository", "repository_id",
    "head_sha", "head_branch", "event", "workflow_path", "source_roots",
}
ARTIFACT_FIELDS = {"id", "name", "size_in_bytes", "digest", "expires_at", "inner_archive"}
ARTIFACT_OVERRIDES = {"run_id", "workflow_path"}
CHECKPOINT_FIELDS = {"run_attempt", "producer_job_id", "producer_job_name"}
ORIGINAL_SOURCE_ROOTS = {
    "linux": "/repo/build/src",
    "macos": "/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/build/src",
    "windows": "C:/ungoogled-chromium-windows/build/src",
}


class CacheMiss(Exception):
    """An unavailable, untrusted, or incompatible cache."""

    def __init__(self, reason, *, details=None):
        super().__init__(reason)
        self.details = details or {}


class LocalError(Exception):
    """An invalid local argument or unsafe destination."""


def require(condition, reason):
    if not condition:
        raise CacheMiss(reason)


def sha256(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            result.update(chunk)
    return "sha256:" + result.hexdigest()


def timestamp(value, reason="invalid_expiry"):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, reason)
        return parsed
    except (AttributeError, TypeError, ValueError) as exc:
        raise CacheMiss(reason) from exc


def load_manifest(platform, arch, run_id=None, root=ROOT):
    path = root / "build/upstream-cache.json"
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw)
        global_pins = load_shared_pins(root)
        pins = load_pins(root, platform)
        version = pins["ChromiumVersion"]
        require(manifest["schema_version"] == 1, "manifest_schema")
        require(manifest["chromium_version"] == global_pins["ChromiumVersion"]
                and manifest["ungoogled_commit"] == global_pins["UngoogledCommit"], "pin_mismatch")
        require(set(manifest["sources"]) == set(SOURCES), "manifest_targets")
        for target, (repo, pin_key, roots) in SOURCES.items():
            source = manifest["sources"][target]
            target_pins = load_pins(root, target)
            target_version = target_pins["ChromiumVersion"]
            target_core = target_pins["UngoogledCommit"]
            available = source.get("available", True)
            require(type(available) is bool, "manifest_availability")
            if not available:
                require(set(source) == UNAVAILABLE_SOURCE_FIELDS, "manifest_unavailable_source")
            require(source.get("chromium_version", manifest["chromium_version"]) == target_version
                    and source.get("ungoogled_commit", manifest["ungoogled_commit"]) == target_core,
                    "pin_mismatch")
            require(source["repository"] == f"ungoogled-software/ungoogled-chromium-{repo}"
                    and source["head_sha"] == target_pins[pin_key]
                    and re.fullmatch(r"[a-f0-9]{40}", source["head_sha"]), "pin_mismatch")
            require(source["source_roots"] == roots
                    and source["event"] in ("push", "workflow_dispatch")
                    and source["head_branch"] in (target_version, target_pins[pin_key.replace("Commit", "Version")])
                    and source["workflow_path"] == (
                        ".github/workflows/build-x64.yml" if target == "windows"
                        else ".github/workflows/build.yml"), "untrusted_manifest_source")
            require(type(source["repository_id"]) is int and source["repository_id"] > 0, "manifest_id")
            if not available:
                continue
            require(not CHECKPOINT_FIELDS.intersection(source), "manifest_checkpoint")
            require(type(source["run_id"]) is int and source["run_id"] > 0, "manifest_id")
            targets = set(source["artifacts"])
            require(targets in ({"x64"}, {"x64", "arm64"}) if target == "windows"
                    else targets == {"x64", "arm64"}, "manifest_targets")
            for artifact_arch, artifact in source["artifacts"].items():
                fields = ARTIFACT_FIELDS
                if target == "windows" and artifact_arch == "arm64":
                    fields = fields | ARTIFACT_OVERRIDES
                    checkpoint = CHECKPOINT_FIELDS.intersection(artifact)
                    require(not checkpoint or checkpoint == CHECKPOINT_FIELDS, "manifest_checkpoint")
                    if checkpoint:
                        fields = fields | CHECKPOINT_FIELDS
                        require(all(type(artifact[key]) is int and artifact[key] > 0
                                    for key in ("run_attempt", "producer_job_id"))
                                and isinstance(artifact["producer_job_name"], str)
                                and re.fullmatch(r"build / build-[1-9][0-9]*", artifact["producer_job_name"]),
                                "manifest_checkpoint")
                    require(type(artifact["run_id"]) is int and artifact["run_id"] > 0
                            and artifact["run_id"] != source["run_id"], "manifest_id")
                    require(artifact["workflow_path"] == ".github/workflows/build-arm.yml"
                            and artifact["name"] == "build-artifact-arm", "untrusted_manifest_artifact")
                require(set(artifact) == fields, "manifest_artifact_fields")
                require(type(artifact["id"]) is int and artifact["id"] > 0
                        and type(artifact["size_in_bytes"]) is int
                        and 0 < artifact["size_in_bytes"] <= MAX_EXTRACTED, "manifest_artifact")
                require(isinstance(artifact["name"], str) and artifact["name"], "manifest_artifact")
                require(re.fullmatch(r"sha256:[a-f0-9]{64}", artifact["digest"] or ""),
                        "missing_pinned_digest")
                inner = artifact["inner_archive"]
                require(safe_name(inner) == inner and "/" not in inner
                        and inner.endswith(".zip" if target == "windows" else ".tar.zst"),
                        "manifest_archive")
                timestamp(artifact["expires_at"])
        source = manifest["sources"][platform]
        identity = {
            "path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
            "schema_version": 1, "target": f"{platform}-{arch}", "chromium_version": version,
            "repository": source["repository"], "head_sha": source["head_sha"],
        }
        if source.get("available", True) is False:
            identity.update(available=False, ungoogled_commit=source["ungoogled_commit"])
            raise CacheMiss("source_unavailable", details={"manifest": identity})
        require(arch in source["artifacts"], "unsupported_target")
        pin = {key: value for key, value in source.items() if key != "artifacts"}
        pin["artifact"] = source["artifacts"][arch]
        overrides = {key: value for key, value in pin["artifact"].items()
                     if key in ARTIFACT_OVERRIDES | CHECKPOINT_FIELDS}
        pin.update(overrides)
        require(run_id is None or run_id == pin["run_id"], "run_id_mismatch")
        pin["chromium_version"] = version
        identity.update(overrides)
        identity.update(run_id=pin["run_id"], artifact_id=pin["artifact"]["id"],
                        artifact_digest=pin["artifact"]["digest"])
        return pin, identity
    except PinError as exc:
        raise CacheMiss("pin_mismatch") from exc
    except (OSError, AttributeError, KeyError, TypeError, ValueError) as exc:
        raise CacheMiss("invalid_manifest_or_pins") from exc


def validate_metadata(pin, run, artifact, now=None, producer=None):
    now = now or datetime.now(timezone.utc)
    expected = pin["artifact"]
    require(isinstance(run, dict) and isinstance(artifact, dict), "invalid_api_metadata")
    checkpoint = CHECKPOINT_FIELDS.intersection(pin)
    require(not checkpoint or (checkpoint == CHECKPOINT_FIELDS
            and pin["repository"] == "ungoogled-software/ungoogled-chromium-windows"
            and pin["workflow_path"] == ".github/workflows/build-arm.yml"
            and expected["name"] == "build-artifact-arm"), "untrusted_checkpoint")
    successful = run.get("status") == "completed" and run.get("conclusion") == "success"
    in_progress = run.get("status") == "in_progress" and run.get("conclusion") is None
    require(all(run.get(key) == pin[key] for key in ("head_sha", "head_branch", "event"))
            and run.get("id") == pin["run_id"]
            and run.get("path") == pin["workflow_path"]
            and (successful or (checkpoint and in_progress)), "untrusted_run")
    for key in ("repository", "head_repository"):
        repo = run.get(key) or {}
        require(isinstance(repo, dict) and repo.get("full_name") == pin["repository"]
                and repo.get("id") == pin["repository_id"]
                and repo.get("private") is False, "untrusted_repository")
    require(all(artifact.get(key) == expected[key] for key in
                ("id", "name", "size_in_bytes", "digest")), "artifact_mismatch")
    workflow = artifact.get("workflow_run") or {}
    require(isinstance(workflow, dict) and workflow.get("id") == pin["run_id"]
            and workflow.get("head_sha") == pin["head_sha"]
            and workflow.get("head_branch") == pin["head_branch"]
            and workflow.get("repository_id") == pin["repository_id"]
            and workflow.get("head_repository_id") == pin["repository_id"], "artifact_provenance")
    require(artifact.get("expired") is False
            and timestamp(artifact.get("expires_at")) > now, "artifact_expired")
    if checkpoint:
        require(type(run.get("run_attempt")) is int
                and run["run_attempt"] == pin["run_attempt"], "untrusted_run_attempt")
        require(isinstance(producer, dict)
                and type(producer.get("id")) is int and producer["id"] == pin["producer_job_id"]
                and producer.get("name") == pin["producer_job_name"]
                and type(producer.get("run_id")) is int and producer["run_id"] == pin["run_id"]
                and type(producer.get("run_attempt")) is int
                and producer["run_attempt"] == pin["run_attempt"]
                and producer.get("head_sha") == pin["head_sha"]
                and producer.get("head_branch") == pin["head_branch"]
                and producer.get("workflow_name") == "build-arm"
                and producer.get("status") == "completed"
                and producer.get("conclusion") == "success", "untrusted_producer")
        steps = producer.get("steps")
        require(isinstance(steps, list) and all(isinstance(step, dict) for step in steps),
                "untrusted_producer_steps")
        stages = [step for step in steps if step.get("name") == "Run Stage"]
        require(len(stages) == 1 and stages[0].get("status") == "completed"
                and stages[0].get("conclusion") == "success", "untrusted_producer_steps")
        reason = "artifact_creation_window"
        require(timestamp(run.get("run_started_at"), reason)
                <= timestamp(producer.get("started_at"), reason)
                <= timestamp(stages[0].get("started_at"), reason)
                <= timestamp(artifact.get("created_at"), reason)
                <= timestamp(stages[0].get("completed_at"), reason)
                <= timestamp(producer.get("completed_at"), reason) <= now, reason)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def valid_url(url, api_only=False):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        allowed = host == "api.github.com" if api_only else (
            host == "api.github.com" or host.endswith(".blob.core.windows.net")
            or host.endswith(".actions.githubusercontent.com"))
        require(parsed.scheme == "https" and allowed and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and not parsed.fragment,
                "unsafe_download_url")
    except ValueError as exc:
        raise CacheMiss("unsafe_download_url") from exc


def http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def exception_evidence(exc):
    # Labels are constants; exception messages, class names and response headers are untrusted.
    classes = ((urllib.error.HTTPError, "HTTPError"), (urllib.error.URLError, "URLError"),
               (http.client.IncompleteRead, "IncompleteRead"),
               (http.client.RemoteDisconnected, "RemoteDisconnected"),
               (http.client.BadStatusLine, "BadStatusLine"),
               (http.client.HTTPException, "HTTPException"),
               (ssl.SSLCertVerificationError, "SSLCertVerificationError"), (ssl.SSLError, "SSLError"),
               (socket.gaierror, "gaierror"), (TimeoutError, "TimeoutError"),
               (ConnectionResetError, "ConnectionResetError"),
               (ConnectionAbortedError, "ConnectionAbortedError"), (BrokenPipeError, "BrokenPipeError"),
               (ConnectionRefusedError, "ConnectionRefusedError"), (ConnectionError, "ConnectionError"),
               (OSError, "OSError"), (CacheMiss, "CacheMiss"))

    def label(error):
        return next((name for kind, name in classes if isinstance(error, kind)), "Exception")

    evidence = {"exception_class": label(exc), "errno": None, "http_status": None}
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else None
    if isinstance(cause, BaseException):
        evidence["cause_class"] = label(cause)
    for error in (exc, cause):
        number = getattr(error, "errno", None) if isinstance(error, OSError) else None
        if type(number) is int and -65535 <= number <= 65535:
            evidence["errno"] = number
    if isinstance(exc, urllib.error.HTTPError):
        evidence["http_status"] = http_status(exc.code)
    return evidence


def request_timeout(deadline=None):
    if deadline is None:
        return TIMEOUT
    remaining = deadline - time.monotonic()
    require(remaining > 0, "download_timeout")
    return min(TIMEOUT, remaining)


class GitHub:
    LOCAL_DOWNLOAD_PHASES = ("download_prepare", "download_write", "download_reset",
                             "download_verify_file", "download_close")

    def __init__(self, token=None):
        self.token = token if token is not None else os.environ.get("GH_TOKEN", "")
        self.opener = urllib.request.build_opener(NoRedirect())

    def open(self, url, download=False, *, offset=0, deadline=None, state=None):
        valid_url(url, api_only=True)
        for hop in range(6):
            headers = {"User-Agent": OWNER, "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28"}
            # Never carry credentials (or API headers) onto a signed blob redirect.
            if hop == 0 and self.token:
                headers["Authorization"] = "Bearer " + self.token
            if hop:
                headers = {"User-Agent": OWNER}
            blob = urllib.parse.urlsplit(url).hostname != "api.github.com"
            ranged = download and blob and offset > 0
            if ranged:
                headers["Range"] = f"bytes={offset}-"
            if state is not None:
                state["phase"] = "blob_request" if blob else "api_request"
                state["http_status"] = None
            try:
                response = self.opener.open(urllib.request.Request(url, headers=headers),
                                            timeout=request_timeout(deadline))
                if state is not None:
                    state["http_status"] = http_status(response.status)
                if response.status != 200 and not (ranged and response.status == 206):
                    response.close()
                    raise CacheMiss("unexpected_http_status")
                return response
            except urllib.error.HTTPError as exc:
                try:
                    location = exc.headers.get("Location")
                    code = exc.code
                finally:
                    exc.close()
                if state is not None:
                    state["http_status"] = http_status(code)
                if download and code in (301, 302, 303, 307, 308) and location:
                    url = urllib.parse.urljoin(url, location)
                    valid_url(url)
                    continue
                raise
        raise CacheMiss("too_many_redirects")

    def retry(self, operation, *, state=None, deadline=None):
        state = state if state is not None else {"phase": "metadata_request", "bytes_written": 0}
        attempts = []
        state["retry_attempts"] = attempts
        for attempt in range(1, ATTEMPTS + 1):
            started = time.monotonic()
            state.update(attempt=attempt, attempt_started=started)
            state["http_status"] = None
            state["phase"] = "download_request" if deadline is not None else "metadata_request"
            try:
                if deadline is not None:
                    if started >= deadline:
                        state["phase"] = "download_deadline"
                    require(started < deadline, "download_timeout")
                return operation()
            except Exception as exc:
                finished = time.monotonic()
                evidence = exception_evidence(exc)
                if not isinstance(exc, urllib.error.HTTPError):
                    evidence["http_status"] = state.get("http_status")
                evidence.update(attempt=attempt, phase=state["phase"],
                                bytes_written=state["bytes_written"],
                                elapsed_seconds=round(max(0, finished - started), 3))
                attempts.append(evidence)
                local_failure = state["phase"] in self.LOCAL_DOWNLOAD_PHASES
                if local_failure:
                    retryable = False
                    error = exc if isinstance(exc, CacheMiss) else CacheMiss("cache_unusable_OSError")
                elif isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                    status = evidence["http_status"]
                    retryable = status in (408, 429, 500, 502, 503, 504)
                    error = CacheMiss(f"github_http_{status}" if status else "github_http_error")
                elif isinstance(exc, CacheMiss):
                    retryable, error = False, exc
                elif isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError,
                                      http.client.HTTPException, ssl.SSLError, socket.gaierror)):
                    retryable, error = True, CacheMiss("network_unavailable")
                elif isinstance(exc, OSError):
                    retryable, error = False, CacheMiss("cache_unusable_OSError")
                else:
                    retryable, error = False, CacheMiss("network_error")
                if not local_failure and deadline is not None and finished >= deadline:
                    retryable, error = False, CacheMiss("download_timeout")
                error.details.update(retry_attempts=attempts,
                                     retry_exhausted=retryable and attempt == ATTEMPTS)
                if not retryable or attempt == ATTEMPTS:
                    if error is exc:
                        raise
                    raise error from exc
                delay = 2**(attempt - 1)
                if deadline is not None:
                    delay = min(delay, max(0, deadline - finished))
                time.sleep(delay)

    def json(self, path):
        state = {"phase": "metadata_request", "bytes_written": 0}

        def request():
            with self.open(API + path, state=state) as response:
                state["phase"] = "metadata_read"
                data = response.read(4 * CHUNK + 1)
                require(len(data) <= 4 * CHUNK, "oversized_api_response")
                state["phase"] = "metadata_decode"
                try:
                    return json.loads(data)
                except (ValueError, UnicodeError) as exc:
                    raise CacheMiss("invalid_api_response") from exc
        return self.retry(request, state=state)

    def download(self, pin, path):
        artifact = pin["artifact"]
        expected = artifact["size_in_bytes"]
        url = f"{API}/repos/{pin['repository']}/actions/artifacts/{artifact['id']}/zip"
        deadline = time.monotonic() + DOWNLOAD_SECONDS
        size = 0
        result = hashlib.sha256()
        state = {"phase": "download_prepare", "bytes_written": 0}

        def write_chunk(chunk):
            nonlocal size
            state["phase"] = "download_write"
            require(size + len(chunk) <= expected, "download_size_mismatch")
            require_space(path.parent, len(chunk))
            remaining = memoryview(chunk)
            while remaining:
                request_timeout(deadline)
                written = output.write(remaining)
                require(type(written) is int and 0 < written <= len(remaining), "download_write_failed")
                result.update(remaining[:written])
                size += written
                state["bytes_written"] = size
                remaining = remaining[written:]

        output = None
        files = ExitStack()

        def close_output():
            try:
                files.close()
            except Exception as exc:
                state["phase"] = "download_close"
                reconcile_file()
                entry = exception_evidence(exc)
                entry.update(attempt=state.get("attempt", 1), phase="download_close",
                             bytes_written=size,
                             elapsed_seconds=round(max(0, time.monotonic() - state.get("attempt_started", deadline)), 3))
                entries = state.get("retry_attempts", [])
                if entries and entries[-1]["attempt"] == entry["attempt"]:
                    entries[-1]["cleanup_failure"] = entry
                else:
                    entries.append(entry)
                raise CacheMiss("cache_unusable_OSError", details={
                    "retry_attempts": entries, "retry_exhausted": False,
                }) from exc

        def reconcile_file():
            nonlocal size, result
            if output is None or state["phase"] not in self.LOCAL_DOWNLOAD_PHASES:
                return
            # Local failures invalidate the digest even if the file size is unchanged.
            result = None
            try:
                actual = os.fstat(output.fileno()).st_size
            except (OSError, ValueError):
                state["file_size_confirmed"] = False
            else:
                state["file_size_confirmed"] = True
                if actual != size:
                    size = actual
                    state["bytes_written"] = size

        def request():
            nonlocal size, result, output
            state["phase"] = "download_prepare"
            if output is None:
                request_timeout(deadline)
                # Only this invocation's writes and hash state can be resumed.
                output = files.enter_context(path.open("wb", buffering=0))
            state["phase"] = "download_request"
            request_timeout(deadline)
            offset = size
            with self.open(url, download=True, offset=offset, deadline=deadline, state=state) as response:
                state["phase"] = "download_response"
                state["http_status"] = http_status(response.status)
                if response.status == 206:
                    value = response.headers.get("Content-Range")
                    match = re.fullmatch(r"bytes ([0-9]{1,20})-([0-9]{1,20})/([0-9]{1,20})", value) \
                        if type(value) is str and len(value) <= 80 else None
                    require(offset > 0 and match is not None, "invalid_content_range")
                    start, end, total = map(int, match.groups())
                    require(start == offset and start <= end == expected - 1 and total == expected,
                            "invalid_content_range")
                    length = response.headers.get("Content-Length")
                    if length is not None:
                        require(type(length) is str and re.fullmatch(r"[0-9]{1,20}", length)
                                and int(length) == expected - offset, "invalid_content_range")
                else:
                    require(response.status == 200, "unexpected_http_status")
                    # A server ignoring Range returns a new complete representation.
                    state["phase"] = "download_reset"
                    output.seek(0)
                    output.truncate()
                    size, result = 0, hashlib.sha256()
                    state["bytes_written"] = 0
                while True:
                    state["phase"] = "download_read"
                    timeout = request_timeout(deadline)
                    if isinstance(response, http.client.HTTPResponse):
                        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                        if sock is not None:
                            sock.settimeout(timeout)
                        # read1 avoids waiting to fill CHUNK across many socket reads.
                        read = response.read1
                    else:
                        read = response.read
                    try:
                        chunk = read(CHUNK)
                    except http.client.IncompleteRead as exc:
                        # HTTPResponse.read1 partials may contain chunk framing, not body bytes.
                        if exc.partial and not isinstance(response, http.client.HTTPResponse):
                            write_chunk(exc.partial)
                        state["phase"] = "download_read"
                        if size == expected:
                            break
                        raise
                    if not chunk:
                        break
                    write_chunk(chunk)
                state["phase"] = "download_verify"
                request_timeout(deadline)
                if size != expected:
                    raise http.client.IncompleteRead(b"", expected - size)
                require("sha256:" + result.hexdigest() == artifact["digest"], "checksum_mismatch")
                state["phase"] = "download_verify_file"
                require(output.tell() == size and os.fstat(output.fileno()).st_size == size,
                        "download_size_mismatch")
            state["phase"] = "download_close"
            files.close()
            return size
        def attempt():
            try:
                return request()
            except Exception:
                reconcile_file()
                raise

        try:
            try:
                return self.retry(attempt, state=state, deadline=deadline)
            finally:
                close_output()
        except CacheMiss as error:
            error.details.update(download_partial_bytes=size,
                                 download_expected_bytes=expected,
                                 download_timeout_seconds=DOWNLOAD_SECONDS)
            if state.get("file_size_confirmed") is False:
                error.details["download_file_size_confirmed"] = False
            raise


def safe_name(name, *, posix=False):
    posix = posix and os.name == "posix"
    require(isinstance(name, str) and not name.startswith("/")
            and not any(ord(c) < 32 for c in name), "unsafe_archive_path")
    if not posix:
        require(not any(c in name for c in '\\:<>"|?*'), "unsafe_archive_path")
    parts = PurePosixPath(name).parts
    require(".." not in parts, "unsafe_archive_path")
    if not posix:
        for part in parts:
            require(not part.endswith((".", " ")) and not re.fullmatch(
                r"(?i)(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", part),
                "unsafe_archive_path")
    return "/".join(parts)


def zip_mtime(info):
    value = calendar.timegm(info.date_time + (0, 0, 0)) * 1_000_000_000
    extra = info.extra
    while len(extra) >= 4:
        kind, length = struct.unpack_from("<HH", extra)
        require(length <= len(extra) - 4, "invalid_zip_timestamp")
        data, extra = extra[4:4 + length], extra[4 + length:]
        if kind == 0x5455 and len(data) >= 5 and data[0] & 1:
            value = struct.unpack_from("<I", data, 1)[0] * 1_000_000_000
        if kind == 0x000A and len(data) >= 4:
            data = data[4:]
            while len(data) >= 4:
                tag, size = struct.unpack_from("<HH", data)
                payload, data = data[4:4 + size], data[4 + size:]
                if tag == 1 and len(payload) >= 8:
                    return (struct.unpack_from("<Q", payload)[0] - 116444736000000000) * 100
    return value


def require_space(path, additional=0):
    free = shutil.disk_usage(path).free
    required = DISK_HEADROOM + additional
    if free < required:
        raise CacheMiss("insufficient_disk_space", details={
            "disk_free_bytes": free, "disk_required_bytes": required,
            "disk_headroom_bytes": DISK_HEADROOM, "disk_path": str(path),
        })


def report_progress(destination, result, phase):
    result["phase"] = phase
    result["disk_free_bytes"] = shutil.disk_usage(destination).free
    write_result(destination, result)
    print(f"upstream cache: phase={phase}; free_bytes={result['disk_free_bytes']}; "
          f"duration_seconds={result.get('duration_seconds', 0)}; "
          f"members={result.get('members', 0)}; extracted_bytes={result.get('extracted_bytes', 0)}",
          file=sys.stderr, flush=True)


class FetchProgress:
    """Persist bounded progress outside the extracted tree, including partial writes."""

    def __init__(self, destination, result, started):
        self.destination = destination
        self.result = result
        self.started = started
        self.phase_started = started
        self.next_report = started

    def phase(self, name):
        now = time.monotonic()
        previous = self.result.get("phase")
        if previous:
            self.result.setdefault("phase_seconds", {})[previous] = round(now - self.phase_started, 3)
        self.result["phase"] = name
        self.phase_started = now
        self(force=True)

    def __call__(self, values=None, *, force=False):
        if values:
            self.result.update(values)
        now = time.monotonic()
        if not force and now < self.next_report:
            return
        self.result["duration_seconds"] = round(now - self.started, 3)
        self.result["phase_duration_seconds"] = round(now - self.phase_started, 3)
        report_progress(self.destination, self.result, self.result["phase"])
        self.next_report = now + PROGRESS_SECONDS


class SourceSelection:
    """Keep complete pinned source roots, with the target's path restrictions."""

    def __init__(self, source_roots, *, platform=None):
        self.platform = platform
        self.posix = platform != "windows" and os.name == "posix"
        self.trees = tuple(safe_name(root) for root in source_roots)
        require(self.trees and all(self.trees), "invalid_source_roots")
        self.parents = {str(parent) for root in self.trees
                        for parent in PurePosixPath(root).parents if str(parent) != "."}

    def __call__(self, name, kind="file"):
        return (any(name == root or name.startswith(root + "/") for root in self.trees)
                or (kind == "dir" and name in self.parents))

    def remap_absolute(self, name, target):
        original = ORIGINAL_SOURCE_ROOTS.get(self.platform)
        if original is None:
            return None
        absolute = target.replace("\\", "/") if self.platform == "windows" else target
        if absolute != original and not absolute.startswith(original + "/"):
            return None
        roots = [root for root in self.trees if root in SOURCES[self.platform][2]
                 and (name == root or name.startswith(root + "/"))]
        if not roots:
            return None
        require(len(roots) == 1, "ambiguous_internal_symlink")
        suffix = absolute[len(original):].lstrip("/")
        suffix = safe_name(suffix, posix=self.posix)
        target_name = roots[0] + ("/" + suffix if suffix else "")
        return posixpath.relpath(target_name, posixpath.dirname(name))


class ToolchainSelection:
    """Legacy toolchain-only selection for explicit callers, never the default."""

    def __init__(self, source_roots):
        self.files = tuple(f"{root}/{name}" for root in source_roots for name in
                           ("BUILD.gn", "chrome/VERSION", "out/Default/args.gn"))
        self.trees = tuple(f"{root}/{name}" for root in source_roots for name in
                          ("tools/clang", "tools/rust", "third_party/llvm-build/Release+Asserts",
                           "third_party/rust-toolchain"))
        self.parents = {str(parent) for name in self.files + self.trees
                        for parent in PurePosixPath(name).parents if str(parent) != "."}

    def __call__(self, name, kind="file"):
        if "__pycache__" in name.split("/") or name.endswith((".pyc", ".pyo")):
            return False
        return (name in self.files or any(name == tree or name.startswith(tree + "/") for tree in self.trees)
                or (kind == "dir" and name in self.parents))


def unpack_outer(outer, inner, expected_name):
    with zipfile.ZipFile(outer) as archive:
        seen = set()
        selected = None
        for info in archive.infolist():
            name = safe_name(info.filename)
            require(name not in seen and len(seen) < MAX_MEMBERS, "duplicate_archive_path")
            seen.add(name)
            mode = info.external_attr >> 16
            require(stat.S_IFMT(mode) in (0, stat.S_IFDIR, stat.S_IFREG), "unsafe_outer_member")
            if name == expected_name:
                require(not info.is_dir() and not info.flag_bits & 1
                        and 0 < info.file_size <= MAX_EXTRACTED, "invalid_inner_archive")
                selected = info
        require(selected is not None, "missing_inner_archive")
        require_space(inner.parent, selected.file_size)
        with archive.open(selected) as source, inner.open("xb") as output:
            while chunk := source.read(CHUNK):
                require_space(inner.parent, len(chunk))
                output.write(chunk)
        require(inner.stat().st_size == selected.file_size, "inner_size_mismatch")
    return inner.stat().st_size


class Extractor:
    """Create regular files first and links last; never traverse archive links."""

    def __init__(self, root, selection=None, progress=None):
        self.root = root
        self.selection = selection
        self.progress = progress
        self.posix = isinstance(selection, SourceSelection) and selection.posix and os.name == "posix"
        platform = getattr(selection, "platform", None)
        self.case_insensitive = platform in ("windows", "macos") or os.name == "nt" or sys.platform == "darwin"
        self.cases = {}
        self.names = {}
        self.parents = set()
        self.directories = {}
        self.links = []
        self.bytes = 0
        self.written_bytes = 0
        self.archive_bytes = 0
        self.skipped = 0
        self.external_symlinks = []
        self.remapped_symlinks = {}
        self.current_member = None
        self.created_parents = set()

    def ensure_parent(self, path):
        # path() still checks every ancestor before using a previously created directory.
        parent = path.parent
        if parent not in self.created_parents:
            parent.mkdir(parents=True, exist_ok=True)
            self.created_parents.add(parent)

    def report(self, *, force=False):
        if self.progress is not None:
            self.progress({"members": len(self.names), "extracted_bytes": self.written_bytes,
                           "archive_bytes": self.archive_bytes,
                           "extraction_member": self.current_member}, force=force)

    def report_step(self, name):
        if self.progress is not None:
            self.progress({"extraction_step": name}, force=True)

    def safe_name(self, name):
        return safe_name(name, posix=self.posix)

    def check_case(self, name, *, register=False):
        if not self.case_insensitive:
            return
        parts = name.split("/")
        for end in range(1, len(parts) + 1):
            prefix = "/".join(parts[:end])
            key = unicodedata.normalize("NFD", prefix).casefold()
            require(self.cases.get(key, prefix) == prefix, "archive_case_collision")
            if register:
                self.cases[key] = prefix

    def selected(self, name, kind="file"):
        return self.selection is None or self.selection(name, kind)

    @staticmethod
    def is_link(path):
        try:
            metadata = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return False
        return (stat.S_ISLNK(metadata.st_mode) or
                bool(getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))

    def path(self, name):
        path = self.root / name
        for parent in path.parents:
            if parent == self.root:
                break
            require(not self.is_link(parent), "archive_link_parent")
        return path

    def begin_member(self, name):
        self.current_member = name
        self.report()

    def add(self, name, kind, size, mode, mtime_ns, stream=None, target=None):
        self.begin_member(name)
        name = self.safe_name(name)
        if not name:
            require(kind == "dir", "empty_archive_path")
            return
        require(name not in self.names and len(self.names) < MAX_MEMBERS, "duplicate_archive_path")
        self.check_case(name, register=True)
        require(kind in ("file", "dir", "sym", "hard") and size >= 0, "unsupported_archive_member")
        require(kind == "dir" or name not in self.parents, "archive_link_collision")
        for parent in PurePosixPath(name).parents:
            parent = str(parent)
            require(self.names.get(parent, "dir") == "dir", "archive_link_parent")
            self.parents.add(parent)
        self.names[name] = kind
        self.current_member = name
        self.report()
        if kind == "file":
            self.archive_bytes += size
            require(self.archive_bytes <= MAX_EXTRACTED, "archive_too_large")
        if kind in ("sym", "hard"):
            self.links.append((name, kind, target, mode, mtime_ns))
            return
        if not self.selected(name, kind):
            return
        path = self.path(name)
        self.ensure_parent(path)
        require(not self.is_link(path), "archive_link_parent")
        if kind == "dir":
            path.mkdir(exist_ok=True)
            self.created_parents.add(path)
            self.directories[name] = (mode, mtime_ns)
        else:
            self.bytes += size
            limit = (MAX_EXTRACTED if self.selection is None or isinstance(self.selection, SourceSelection)
                     else MAX_SELECTED)
            require(self.bytes <= limit, "archive_too_large")
            with path.open("xb") as output:
                remaining = size
                while remaining:
                    chunk = stream.read(min(CHUNK, remaining))
                    require(chunk, "truncated_archive_member")
                    if self.selection:
                        try:
                            require_space(self.root, len(chunk))
                        except CacheMiss as exc:
                            exc.details.update(extracted_bytes=self.written_bytes,
                                               extraction_member=name, members=len(self.names))
                            raise
                    output.write(chunk)
                    self.written_bytes += len(chunk)
                    remaining -= len(chunk)
                    self.report()
            self.metadata(path, mode, mtime_ns)

    @staticmethod
    def metadata(path, mode, mtime_ns, symlink=False):
        if not symlink:
            os.chmod(path, mode & 0o777)
        if not symlink or os.utime in os.supports_follow_symlinks:
            os.utime(path, ns=(mtime_ns, mtime_ns), follow_symlinks=not symlink)

    def validate_links(self):
        symlinks = {name: target for name, kind, target, _, _ in self.links if kind == "sym"}
        hardlinks = {}
        resolved_symlinks = {}
        for name, kind, target, _, _ in self.links:
            self.current_member = name
            self.report()
            require(isinstance(target, str) and target and not any(ord(c) < 32 for c in target), "unsafe_link")
            if kind == "sym" and isinstance(self.selection, SourceSelection):
                remapped = self.selection.remap_absolute(name, target)
                if remapped is not None:
                    self.remapped_symlinks[name] = target = remapped
                    symlinks[name] = target
            for part in target.split("/"):
                if part not in ("", ".", ".."):
                    self.safe_name(part)
            if kind == "hard":
                hardlinks[name] = self.safe_name(target)
                require(hardlinks[name], "unsafe_link")
        for name, target in symlinks.items():
            self.current_member = name
            self.report()
            retained = self.selection is not None and self.selected(name)
            if target.startswith("/"):
                require(not retained or isinstance(self.selection, SourceSelection), "excluded_link_target")
                continue
            pending = deque(PurePosixPath(name).parent.parts + PurePosixPath(target).parts)
            resolved = []
            expansions = 0
            while pending:
                part = pending.popleft()
                if part == "..":
                    require(resolved, "escaping_symlink")
                    resolved.pop()
                    continue
                self.safe_name(part)
                resolved.append(part)
                link_name = "/".join(resolved)
                self.check_case(link_name)
                link = symlinks.get(link_name)
                if link is not None:
                    require(not link.startswith("/"), "external_symlink_chain")
                    require(not retained or self.selected(link_name), "excluded_link_target")
                    expansions += 1
                    require(expansions <= 128, "cyclic_symlink")
                    resolved.pop()
                    pending.extendleft(reversed(PurePosixPath(link).parts))
                elif pending and name in self.remapped_symlinks:
                    require(self.names.get(link_name) == "dir" or link_name in self.parents,
                            "missing_internal_symlink_target")
            target_name = "/".join(resolved)
            require(not retained or self.selected(target_name), "excluded_link_target")
            if name in self.remapped_symlinks:
                require(target_name in self.names or target_name in self.parents, "missing_internal_symlink_target")
            resolved_symlinks[name] = target_name
        for name, target in hardlinks.items():
            self.current_member = name
            self.report()
            seen = {name}
            while True:
                require(target not in seen, "unresolved_hardlink")
                seen.add(target)
                self.check_case(target)
                require(not (self.selection and self.selected(name)) or self.selected(target),
                        "excluded_link_target")
                for parent in PurePosixPath(target).parents:
                    require(self.names.get(str(parent), "dir") == "dir", "unsafe_hardlink")
                kind = self.names.get(target)
                if kind != "hard":
                    require(kind == "file", "unsafe_hardlink")
                    break
                target = hardlinks[target]
        return resolved_symlinks

    def finish(self):
        self.report(force=True)
        self.report_step("validate_links")
        resolved_symlinks = self.validate_links()
        self.report_step("create_links")
        hard = []
        for name, kind, target, mode, mtime_ns in self.links:
            self.current_member = name
            self.report()
            target = self.remapped_symlinks.get(name, target)
            if kind == "sym" and target.startswith("/"):
                self.skipped += 1
                if self.selected(name):
                    self.external_symlinks.append(name)
                continue
            if not self.selected(name):
                continue
            path = self.path(name)
            self.ensure_parent(path)
            require(not path.exists() and not path.is_symlink(), "archive_link_collision")
            if kind == "hard":
                target_name = self.safe_name(target)
                require(target_name, "unsafe_link")
                hard.append((path, target_name))
                continue
            target_name = resolved_symlinks[name]
            is_directory = not target_name or self.names.get(target_name) == "dir" or target_name in self.parents
            os.symlink(target, path, target_is_directory=is_directory)
            self.metadata(path, mode, mtime_ns, symlink=True)
        while hard:
            pending = []
            for path, target_name in hard:
                target = self.path(target_name)
                require(not target.is_symlink(), "unsafe_hardlink")
                if not target.exists():
                    pending.append((path, target_name))
                    continue
                require(target.is_file(), "unsafe_hardlink")
                os.link(target, path)
            require(len(pending) < len(hard), "unresolved_hardlink")
            hard = pending
        self.report_step("directory_metadata")
        for name in sorted(self.directories, key=lambda n: n.count("/"), reverse=True):
            self.current_member = name
            self.report()
            self.metadata(self.path(name), *self.directories[name])
        self.report(force=True)
        return {"extracted_bytes": self.bytes, "members": len(self.names),
                "skipped_external_symlinks": self.skipped,
                "external_symlink_paths": self.external_symlinks,
                "remapped_internal_symlinks": len(self.remapped_symlinks)}


def extract_tar(stream, tree, selection=None, progress=None):
    extractor = Extractor(tree, selection, progress)
    extractor.report_step("members")
    with tarfile.open(fileobj=stream, mode="r|", bufsize=CHUNK) as archive:
        for info in archive:
            extractor.begin_member(info.name)
            if info.isdir():
                kind = "dir"
            elif info.issym():
                kind = "sym"
            elif info.islnk():
                kind = "hard"
            else:
                require(info.isfile() and not info.issparse(), "unsupported_archive_member")
                kind = "file"
            mtime = Decimal(info.pax_headers.get("mtime", str(info.mtime)))
            extractor.add(info.name, kind, info.size, info.mode, int(mtime * 1_000_000_000),
                          archive.extractfile(info) if kind == "file" else None, info.linkname)
            # Streaming tarfile otherwise retains every TarInfo in a multi-GB tree.
            archive.members.clear()
    return extractor.finish()


def extract_zip(inner, tree, selection=None, progress=None):
    extractor = Extractor(tree, selection, progress)
    extractor.report_step("zip_index")
    with zipfile.ZipFile(inner) as archive:
        extractor.report_step("members")
        for info in archive.infolist():
            extractor.begin_member(info.filename)
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            require(kind in (0, stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK)
                    and not info.flag_bits & 1, "unsupported_archive_member")
            if info.is_dir():
                extractor.add(info.filename, "dir", 0, mode or 0o755, zip_mtime(info))
            elif kind == stat.S_IFLNK:
                require(info.file_size <= 4096, "oversized_link")
                extractor.add(info.filename, "sym", 0, mode, zip_mtime(info),
                              target=archive.read(info).decode("utf-8"))
            else:
                with archive.open(info) as stream:
                    extractor.add(info.filename, "file", info.file_size, mode or 0o644,
                                  zip_mtime(info), stream)
    return extractor.finish()


def extract_inner(inner, tree, zstd=None, selection=None, progress=None):
    tree.mkdir()
    if selection:
        require_space(tree)
    if zstd is None:
        return extract_zip(inner, tree, selection, progress)
    with subprocess.Popen([zstd, "-d", "-c", "--", str(inner)], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, cwd=inner.parent) as process:
        try:
            result = extract_tar(process.stdout, tree, selection, progress)
            while process.stdout.read(CHUNK):
                pass
            require(process.wait(timeout=TIMEOUT) == 0, "zstd_failed")
            return result
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()


def source_path(tree, pin):
    candidates = []
    for name in pin["source_roots"]:
        path = tree / name
        if (path / "BUILD.gn").is_file():
            candidates.append(path)
    require(len(candidates) == 1, "invalid_source_shape")
    source = candidates[0]
    require(not source.is_symlink() and source.resolve().is_relative_to(tree.resolve()),
            "invalid_source_shape")
    version_file = source / "chrome/VERSION"
    require((source / "BUILD.gn").resolve().is_relative_to(tree.resolve())
            and version_file.resolve().is_relative_to(tree.resolve()), "invalid_source_shape")
    require(version_file.is_file() and version_file.stat().st_size <= 4096, "missing_source_version")
    values = dict(re.findall(r"^(MAJOR|MINOR|BUILD|PATCH)=(\d+)\s*$", version_file.read_text(), re.M))
    require(".".join(values.get(key, "") for key in ("MAJOR", "MINOR", "BUILD", "PATCH"))
            == pin["chromium_version"], "source_version_mismatch")
    return source.resolve()


def write_result(destination, result):
    temporary = destination / ".result.tmp"
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
    temporary.replace(destination / "result.json")


def destination_path(value):
    if not value.strip():
        raise LocalError("destination must be a dedicated directory")
    path = Path(os.path.abspath(Path(value).expanduser()))
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise LocalError("destination cannot contain symlinks or junctions")
    if path == ROOT or path in ROOT.parents or path == Path.home():
        raise LocalError("destination must be a dedicated subdirectory")
    path.mkdir(parents=True, exist_ok=True)
    children = {p.name for p in path.iterdir()}
    previous = None
    if children:
        allowed = {"result.json", "tree", ".download.zip", ".inner", ".result.tmp"}
        if not children <= allowed or "result.json" not in children:
            raise LocalError("destination is neither empty nor result-owned (or is locked)")
        for name in children:
            child = path / name
            if (child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction())
                    or (name == "tree" and not child.is_dir())
                    or (name != "tree" and not child.is_file())):
                raise LocalError("invalid result-owned destination")
        try:
            previous = json.loads((path / "result.json").read_text())
        except (ValueError, UnicodeError) as exc:
            raise LocalError("invalid ownership result") from exc
        if not isinstance(previous, dict) or previous.get("owner") != OWNER \
                or previous.get("destination") != str(path):
            raise LocalError("destination is not owned by this tool")
    return path, previous


def cleanup(destination):
    tree = destination / "tree"
    if tree.exists():
        def writable(function, path, exc):
            os.chmod(path, 0o700)
            function(path)
        shutil.rmtree(tree, onerror=writable)
    for name in (".download.zip", ".inner", ".result.tmp"):
        (destination / name).unlink(missing_ok=True)


def fetch(platform, arch, destination, run_id=None, root=ROOT, client=None):
    started = time.monotonic()
    scope = SOURCE_SCOPE
    destination, previous = destination_path(str(destination))
    lock = destination / ".lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError as exc:
        raise LocalError("destination is locked") from exc
    result = {"owner": OWNER, "status": "miss", "source": None, "destination": str(destination),
              "platform": platform, "arch": arch, "manifest": {"path": str(root / "build/upstream-cache.json")},
              "download_bytes": 0, "inner_bytes": 0, "extracted_bytes": 0,
              "skipped_external_symlinks": 0, "remapped_internal_symlinks": 0,
              "duration_seconds": 0,
              "extraction_scope": scope}
    progress = FetchProgress(destination, result, started)
    try:
        if previous is None:
            write_result(destination, result)
        try:
            result["manifest"]["sha256"] = sha256(root / "build/upstream-cache.json").split(":", 1)[1]
            pin, identity = load_manifest(platform, arch, run_id, root)
            result["manifest"] = identity
            if (previous and previous.get("status") == "hit" and previous.get("manifest") == identity
                    and previous.get("extraction_scope") == scope):
                source = source_path(destination / "tree", pin)
                require(previous.get("source") == str(source), "invalid_previous_source")
                return previous
            cleanup(destination)
            write_result(destination, result)
            zstd = None
            if platform != "windows":
                zstd = shutil.which("zstd")
                require(zstd is not None, "zstd_unavailable")
                zstd = str(Path(zstd).resolve())
                require(not Path(zstd).is_relative_to(destination), "unsafe_decompressor")
            client = client or GitHub()
            progress.phase("metadata")
            base = f"/repos/{pin['repository']}/actions"
            run_path = f"{base}/runs/{pin['run_id']}"
            if "run_attempt" in pin:
                run_path += f"/attempts/{pin['run_attempt']}"
            run = client.json(run_path)
            artifact = client.json(f"{base}/artifacts/{pin['artifact']['id']}")
            producer = (client.json(f"{base}/jobs/{pin['producer_job_id']}")
                        if "producer_job_id" in pin else None)
            validate_metadata(pin, run, artifact, producer=producer)
            require_space(destination, 2 * pin["artifact"]["size_in_bytes"])
            outer, inner = destination / ".download.zip", destination / ".inner"
            # Download verifies the pinned outer ZIP digest before opening either archive.
            progress.phase("download")
            result["download_bytes"] = client.download(pin, outer)
            progress.phase("unpack_outer")
            result["inner_bytes"] = unpack_outer(outer, inner, pin["artifact"]["inner_archive"])
            outer.unlink()
            progress.phase("extract_source_and_objects")
            result.update(extract_inner(inner, destination / "tree", zstd,
                                        SourceSelection(pin["source_roots"], platform=platform), progress))
            inner.unlink()
            progress.phase("verify_source")
            result["source"] = str(source_path(destination / "tree", pin))
            progress.phase("complete")
            result["status"] = "hit"
        except CacheMiss as exc:
            result["reason"] = str(exc)
            result.update(exc.details)
        except (OSError, tarfile.TarError, zipfile.BadZipFile, zipfile.LargeZipFile,
                EOFError, UnicodeError, ValueError, OverflowError, InvalidOperation, zlib.error,
                NotImplementedError, subprocess.SubprocessError) as exc:
            result["reason"] = "cache_unusable_" + type(exc).__name__
        if result["status"] == "miss":
            result["source"] = None
            result["cleanup_in_progress"] = True
            result["duration_seconds"] = round(time.monotonic() - started, 3)
            write_result(destination, result)
            print(f"upstream cache: miss={result['reason']}; cleanup_in_progress=true",
                  file=sys.stderr, flush=True)
            cleanup(destination)
            result.pop("cleanup_in_progress")
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        write_result(destination, result)
        return result
    finally:
        lock.unlink(missing_ok=True)


def positive_id(value):
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("run ID must be a positive integer")
    return int(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=tuple(SOURCES))
    parser.add_argument("--arch", required=True, choices=("x64", "arm64"))
    parser.add_argument("--destination", required=True)
    parser.add_argument("--run-id", type=positive_id, help="must equal the pinned run ID")
    args = parser.parse_args(argv)
    try:
        result = fetch(args.platform, args.arch, args.destination, args.run_id)
    except (LocalError, OSError) as exc:
        print(f"upstream cache: invalid local destination ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    print(f"upstream cache: {result['status']}; skipped external symlinks: "
          f"{result['skipped_external_symlinks']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
