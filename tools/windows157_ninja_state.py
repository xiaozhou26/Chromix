#!/usr/bin/env python3
"""Read-only Ninja1.12.1 log-v6/deps-v4 validation for the pinned Windows donor."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import struct

NINJA_SHA256 = "68865c3276d449d746cea5065fdec2baf755d7813e161ab04205b0907b2629b8"
LOG_HEADER = b"# ninja log v6\n"
DEPS_HEADER = b"# ninjadeps\n\x04\x00\x00\x00"
MAX_RECORD = (1 << 19) - 1


def header_diagnostic(raw: bytes) -> str:
    line = raw.split(b"\n", 1)[0]
    if re.fullmatch(rb"# ninja log v[0-9]{1,4}\r?", line):
        return repr(line[:32] + (b"\n" if b"\n" in raw else b""))
    # Arbitrary first-line bytes might contain credentials; do not echo them.
    return "<unrecognized header; bytes=" + str(min(len(raw), 32)) + "; sha256=" + hashlib.sha256(raw[:32]).hexdigest() + ">"


def checked_path(raw: bytes, *, output: bool) -> str:
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("invalid UTF8 retained Ninja path") from error
    if not name or any(ord(c) < 32 or ord(c) == 127 for c in name) or "\\" in name or ":" in name or name.startswith("/"):
        raise ValueError("unsafe retained Ninja path")
    parts = name.split("/")
    if any(p in ("", ".") for p in parts):
        raise ValueError("noncanonical retained Ninja path")
    if output:
        if ".." in parts:
            raise ValueError("retained Ninja output escapes output directory")
    else:
        # Source dependencies are relative to src/out/Chromix; never escape src.
        depth = 2
        for part in parts:
            depth += -1 if part == ".." else 1
            if depth < 0:
                raise ValueError("retained Ninja dependency escapes source tree")
    return name


def parse_log(raw: bytes) -> dict:
    if not raw.startswith(LOG_HEADER):
        raise ValueError("unsupported retained Ninja log; expected1.12.1/v6; header=" + header_diagnostic(raw))
    if not raw.endswith(b"\n"):
        raise ValueError("truncated retained Ninja log")
    records = raw[len(LOG_HEADER):].split(b"\n")[:-1]
    if not records:
        raise ValueError("empty retained Ninja command log")
    outputs = {}
    for number, line in enumerate(records, 2):
        fields = line.split(b"\t")
        if len(fields) != 5:
            raise ValueError(f"invalid retained Ninja log field count at record {number}")
        start, end, timestamp, path, command = fields
        if (not all(re.fullmatch(rb"[0-9]+", value) for value in (start, end, timestamp))
                or not re.fullmatch(rb"[0-9a-f]{1,16}", command)):
            raise ValueError(f"invalid retained Ninja numeric/hash field at record {number}")
        begin, finish, mtime = int(start), int(end), int(timestamp)
        if not 0 <= begin <= finish <= 0x7fffffff or not 0 <= mtime <= 0x7fffffffffffffff:
            raise ValueError(f"invalid retained Ninja duration/timestamp at record {number}")
        name = checked_path(path, output=True)
        outputs[name] = (begin, finish, mtime, command.decode())
    return {"version": 6, "records": len(records), "outputs": outputs, "sha256": hashlib.sha256(raw).hexdigest()}


def parse_deps(raw: bytes) -> dict:
    if not raw.startswith(DEPS_HEADER):
        raise ValueError("unsupported retained Ninja deps; expected1.12.1/v4; header_sha256=" + hashlib.sha256(raw[:16]).hexdigest())
    offset = len(DEPS_HEADER)
    paths = []
    seen = set()
    outputs = set()
    records = 0
    while offset < len(raw):
        if offset + 4 > len(raw):
            raise ValueError("truncated retained Ninja deps size")
        tagged = struct.unpack_from("<I", raw, offset)[0]
        offset += 4
        size = tagged & 0x7fffffff
        is_deps = bool(tagged & 0x80000000)
        if size == 0 or size > MAX_RECORD or size % 4 or offset + size > len(raw):
            raise ValueError("invalid/truncated retained Ninja deps record")
        data = memoryview(raw)[offset:offset + size]
        offset += size
        if is_deps:
            if size < 12:
                raise ValueError("short retained Ninja dependency record")
            values = struct.unpack_from("<" + "I" * (size // 4), data)
            out_id, low, high = values[:3]
            if out_id >= len(paths) or any(index >= len(paths) for index in values[3:]) or high > 0x7fffffff:
                raise ValueError("invalid retained Ninja dependency reference/timestamp")
            checked_path(paths[out_id].encode(), output=True)
            outputs.add(paths[out_id])
            records += 1
        else:
            if size < 8:
                raise ValueError("short retained Ninja path record")
            padded = bytes(data[:-4])
            name_bytes = padded.rstrip(b"\x00")
            if len(padded) - len(name_bytes) > 3 or b"\x00" in name_bytes:
                raise ValueError("invalid retained Ninja path padding")
            checksum = struct.unpack_from("<I", data, size - 4)[0]
            if (~checksum & 0xffffffff) != len(paths):
                raise ValueError("retained Ninja path checksum mismatch")
            name = checked_path(name_bytes, output=False)
            if name in seen:
                raise ValueError("duplicate retained Ninja path identity")
            seen.add(name)
            paths.append(name)
    if not paths or not records:
        raise ValueError("empty retained Ninja dependencies")
    return {"version": 4, "paths": len(paths), "records": records, "outputs": outputs,
            "sha256": hashlib.sha256(raw).hexdigest()}


def validate(out: Path, ninja: Path) -> dict:
    before = {}
    payloads = {}
    for name in (".ninja_log", ".ninja_deps"):
        path = out / name
        info = path.stat()
        before[name] = (info.st_size, info.st_mtime_ns, info.st_ino, info.st_nlink)
        if path.is_symlink() or info.st_nlink != 1:
            raise ValueError("linked retained Ninja state")
        payloads[name] = path.read_bytes()
    with ninja.open("rb") as stream:
        ninja_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if ninja_hash != NINJA_SHA256:
        raise ValueError("retained Windows Ninja differs from official1.12.1 binary")
    log = parse_log(payloads[".ninja_log"])
    deps = parse_deps(payloads[".ninja_deps"])
    if not deps["outputs"].issubset(log["outputs"]):
        raise ValueError("retained deps outputs missing from command log")
    objects = 0
    for name in log["outputs"]:
        path = out / name
        current = path
        while current != out:
            try:
                info = current.lstat()
            except FileNotFoundError:
                current = current.parent
                continue
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("retained Ninja output path contains a symlink/reparse point")
            current = current.parent
        if path.exists():
            if not path.is_file():
                raise ValueError("retained Ninja output is not a regular file")
            if name.endswith(".obj") and path.stat().st_size:
                objects += 1
    if objects == 0:
        raise ValueError("retained log has no existing compiled objects")
    for name, expected in before.items():
        path = out / name
        info = path.stat()
        if (info.st_size, info.st_mtime_ns, info.st_ino, info.st_nlink) != expected or path.read_bytes() != payloads[name]:
            raise ValueError("retained Ninja state changed during read-only validation")
    return {"ninja_version": "1.12.1", "ninja_sha256": ninja_hash,
            "log_version": 6, "log_records": log["records"], "log_sha256": log["sha256"],
            "deps_version": 4, "deps_paths": deps["paths"], "deps_records": deps["records"],
            "deps_sha256": deps["sha256"], "retained_objects": objects,
            "header_repr": repr(LOG_HEADER), "state_unchanged": True}
