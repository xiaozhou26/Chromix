#!/usr/bin/env python3
"""Check restored host tools and invalidate outputs without discarding Ninja state."""
from __future__ import annotations

import argparse
import configparser
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import platform as host_platform
import re
import shutil
import stat
import struct
import subprocess
import sys

try:
    from .import_upstream_cache import CLANG, RUST, Miss, digest_file, validate_rust_libraries
    from .macos_runtime import bindgen_environment, runtime_environment
    from .macos_sdk_identity import sdk_content_identity, validated_sdk_content
    from .platform_pins import load_pins
    from .repair_windows_midl import apply as repair_windows_midl
    from .restore_upstream_cache import linked, verify_restored
    from .upstream_object_cache import ninja_deps, ninja_log, write_json
    from .upstream_script_identity import ENDPOINTS
except ImportError:
    from import_upstream_cache import CLANG, RUST, Miss, digest_file, validate_rust_libraries
    from macos_runtime import bindgen_environment, runtime_environment
    from macos_sdk_identity import sdk_content_identity, validated_sdk_content
    from platform_pins import load_pins
    from repair_windows_midl import apply as repair_windows_midl
    from restore_upstream_cache import linked, verify_restored
    from upstream_object_cache import ninja_deps, ninja_log, write_json
    from upstream_script_identity import ENDPOINTS

ROOT = Path(__file__).resolve().parents[1]
MARKER = ".chromix-restored-build-prepared.json"
INSPECTION = ".chromix-restored-build-inspection.json"
SCHEMA = 2
COMPILED_SUFFIXES = {".o", ".obj", ".a", ".lib", ".rlib", ".rmeta", ".pch", ".gch", ".pcm", ".bc"}
METADATA = {"args.gn", "build.ninja", ".ninja_deps", ".ninja_log"}
DEVTOOLS = "third_party/devtools-frontend/src"
TYPESCRIPT_WRAPPER = DEVTOOLS + "/third_party/typescript/typescript.py"
TYPESCRIPT_PACKAGE = DEVTOOLS + "/node_modules/typescript"
# Complete standard-library declaration set shipped by the pinned TypeScript 6.0.2 package.
TYPESCRIPT_LIBRARIES = """
lib.d.ts lib.decorators.d.ts lib.decorators.legacy.d.ts
lib.dom.asynciterable.d.ts lib.dom.d.ts lib.dom.iterable.d.ts
lib.es2015.collection.d.ts lib.es2015.core.d.ts lib.es2015.d.ts lib.es2015.generator.d.ts
lib.es2015.iterable.d.ts lib.es2015.promise.d.ts lib.es2015.proxy.d.ts lib.es2015.reflect.d.ts
lib.es2015.symbol.d.ts lib.es2015.symbol.wellknown.d.ts
lib.es2016.array.include.d.ts lib.es2016.d.ts lib.es2016.full.d.ts lib.es2016.intl.d.ts
lib.es2017.arraybuffer.d.ts lib.es2017.d.ts lib.es2017.date.d.ts lib.es2017.full.d.ts
lib.es2017.intl.d.ts lib.es2017.object.d.ts lib.es2017.sharedmemory.d.ts lib.es2017.string.d.ts
lib.es2017.typedarrays.d.ts
lib.es2018.asyncgenerator.d.ts lib.es2018.asynciterable.d.ts lib.es2018.d.ts lib.es2018.full.d.ts
lib.es2018.intl.d.ts lib.es2018.promise.d.ts lib.es2018.regexp.d.ts
lib.es2019.array.d.ts lib.es2019.d.ts lib.es2019.full.d.ts lib.es2019.intl.d.ts
lib.es2019.object.d.ts lib.es2019.string.d.ts lib.es2019.symbol.d.ts
lib.es2020.bigint.d.ts lib.es2020.d.ts lib.es2020.date.d.ts lib.es2020.full.d.ts
lib.es2020.intl.d.ts lib.es2020.number.d.ts lib.es2020.promise.d.ts lib.es2020.sharedmemory.d.ts
lib.es2020.string.d.ts lib.es2020.symbol.wellknown.d.ts
lib.es2021.d.ts lib.es2021.full.d.ts lib.es2021.intl.d.ts lib.es2021.promise.d.ts
lib.es2021.string.d.ts lib.es2021.weakref.d.ts
lib.es2022.array.d.ts lib.es2022.d.ts lib.es2022.error.d.ts lib.es2022.full.d.ts
lib.es2022.intl.d.ts lib.es2022.object.d.ts lib.es2022.regexp.d.ts lib.es2022.string.d.ts
lib.es2023.array.d.ts lib.es2023.collection.d.ts lib.es2023.d.ts lib.es2023.full.d.ts lib.es2023.intl.d.ts
lib.es2024.arraybuffer.d.ts lib.es2024.collection.d.ts lib.es2024.d.ts lib.es2024.full.d.ts
lib.es2024.object.d.ts lib.es2024.promise.d.ts lib.es2024.regexp.d.ts
lib.es2024.sharedmemory.d.ts lib.es2024.string.d.ts
lib.es2025.collection.d.ts lib.es2025.d.ts lib.es2025.float16.d.ts lib.es2025.full.d.ts
lib.es2025.intl.d.ts lib.es2025.iterator.d.ts lib.es2025.promise.d.ts lib.es2025.regexp.d.ts
lib.es5.d.ts lib.es6.d.ts
lib.esnext.array.d.ts lib.esnext.collection.d.ts lib.esnext.d.ts lib.esnext.date.d.ts
lib.esnext.decorators.d.ts lib.esnext.disposable.d.ts lib.esnext.error.d.ts lib.esnext.full.d.ts
lib.esnext.intl.d.ts lib.esnext.sharedmemory.d.ts lib.esnext.temporal.d.ts lib.esnext.typedarrays.d.ts
lib.scripthost.d.ts lib.webworker.asynciterable.d.ts lib.webworker.d.ts
lib.webworker.importscripts.d.ts lib.webworker.iterable.d.ts
""".split()
# DevTools 511172b786248e29d7493bf954faa57f834dc5b3 + portablelinux revert-tsgo-usage.patch.
TYPESCRIPT_PORTABLE = "23758f202fffeeb81e6d65fc378fb2d7a59558d682f138f5e7552c1c3139b5de"
TYPESCRIPT_REPAIRED = {
    "x64": "ce2996d03f0896752e2fb443b1b1751b337d48fb939eb09ac2acabcb890384c1",
    "arm64": "35df08a4541b20f917ea4806a547f6ccf2082492e6ef5d05323a266db148c03d",
}


def _sdk_root(path: Path, root: Path) -> bool:
    out = root if tuple(part.lower() for part in root.parts[-2:]) == ("out", "default") else root / "out/Default"
    return path.parent == out and path.name.lower() in ("sdk", "xcode_links")


def inside_path(root: Path, relative: str, *, output=False) -> Path | None:
    value = relative.replace("\\", "/")
    if (not value or "\0" in value or Path(value).is_absolute()
            or PureWindowsPath(value).drive or ":" in value):
        return None
    parts = Path(value).parts
    if output and (".." in parts or Path(value).name in METADATA or value.endswith(".ninja")):
        return None
    path = root / value
    try:
        if not path.resolve().is_relative_to(root.resolve()):
            return None
        if output and any(_sdk_root(parent, root) or linked(parent) for parent in (path, *path.parents)):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return path


def host_identity() -> tuple[str, str]:
    system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(host_platform.system(), "unknown")
    machine = host_platform.machine().lower()
    return system, {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine, machine)


def binary_architectures(path: Path, platform: str) -> set[str]:
    """Read executable headers only; never execute a foreign architecture."""
    cpus = {"linux": {62: "x64", 183: "arm64"},
            "macos": {0x1000007: "x64", 0x100000C: "arm64"},
            "windows": {0x8664: "x64", 0xAA64: "arm64"}}[platform]
    with path.open("rb") as stream:
        header = stream.read(64)
        if platform == "linux" and header[:6] == b"\x7fELF\x02\x01" and len(header) >= 20:
            cpu = struct.unpack_from("<H", header, 18)[0]
        elif platform == "macos" and header[:4] == b"\xcf\xfa\xed\xfe" and len(header) >= 8:
            cpu = struct.unpack_from("<I", header, 4)[0]
        elif (platform == "macos" and len(header) >= 8
              and header[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf")):
            count = struct.unpack_from(">I", header, 4)[0]
            if count > 32:
                return set()
            width = 32 if header[:4] == b"\xca\xfe\xba\xbf" else 20
            stream.seek(8)
            entries = stream.read(count * width)
            if len(entries) != count * width:
                return set()
            return {cpus[cpu] for offset in range(0, len(entries), width)
                    if (cpu := struct.unpack_from(">I", entries, offset)[0]) in cpus}
        elif platform == "windows" and header[:2] == b"MZ" and len(header) == 64:
            stream.seek(struct.unpack_from("<I", header, 60)[0])
            pe = stream.read(6)
            if len(pe) != 6 or pe[:4] != b"PE\0\0":
                return set()
            cpu = struct.unpack_from("<H", pe, 4)[0]
        else:
            return set()
    return {cpus[cpu]} if cpu in cpus else set()


def tool_paths(platform: str, arch: str, *, host_arch: str | None = None) -> dict[str, Path]:
    host_arch = arch if host_arch is None else host_arch
    suffix = ".exe" if platform == "windows" else ""
    clang = {"linux": ("clang", "clang++", "llvm-ar", "llvm-nm", "ld.lld"),
             "macos": ("clang", "clang++", "llvm-ar", "ld64.lld"),
             "windows": ("clang-cl", "lld-link", "llvm-ml")}[platform]
    paths = {name: CLANG / "bin" / (name + suffix) for name in clang}
    paths.update({name: RUST / "bin" / (name + suffix) for name in ("rustc", "cargo", "bindgen")})
    node = {"linux": f"linux/node-linux-{host_arch}/bin/node", "windows": "win/node.exe",
            "macos": "mac_arm64/node-darwin-arm64/bin/node" if host_arch == "arm64" else "mac/node-darwin-x64/bin/node"}[platform]
    paths.update(node=Path("third_party/node") / node, gn=Path("out/Default") / ("gn" + suffix))
    return paths


def inspect_native_tools(src: Path, platform: str, arch: str) -> dict:
    system, machine = host_identity()
    if (platform, arch) not in (("linux", "x64"), ("linux", "arm64"), ("macos", "x64"),
                                ("macos", "arm64"), ("windows", "x64"), ("windows", "arm64")):
        raise ValueError("unsupported restored build target")
    native_arch = "x64" if platform == "windows" else arch
    if ((system, machine) != (platform, native_arch)
            and (platform, arch, system, machine) != ("linux", "arm64", "linux", "x64")):
        raise ValueError(f"a native {platform} {native_arch} runner is required (Linux ARM64 also supports Linux x64 hosts)")
    probe_env = runtime_environment(src, machine) if platform == "macos" else None
    tools = {}
    for name, relative in tool_paths(platform, arch, host_arch=machine).items():
        path = src / relative
        entry = {"path": relative.as_posix(), "native": False, "architectures": [], "exists": path.is_file(),
                 "file_identity": _stat_identity(path)}
        try:
            architectures = binary_architectures(path, platform)
            entry["architectures"] = sorted(architectures)
            entry["wrong_host"] = bool(architectures and machine not in architectures)
            if machine not in architectures:
                raise ValueError("binary does not match the native runner")
            if platform != "windows" and not os.access(path, os.X_OK):
                raise ValueError("tool is not executable")
            probe_arg = "/?" if name == "llvm-ml" else "--version"
            entry["probe_argument"] = probe_arg
            context = (bindgen_environment(src, machine) if platform == "macos" and name == "bindgen"
                       else nullcontext(probe_env))
            with context as env:
                completed = subprocess.run([str(path), probe_arg], cwd=src, text=True, env=env,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           timeout=30, check=False)
            entry["version"] = completed.stdout.strip()[:2000]
            if completed.returncode:
                raise ValueError(f"{probe_arg} exited {completed.returncode}")
            entry["native"] = True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            entry["reason"] = str(error)
        tools[name] = entry
    return {"native_tools": all(value["native"] for value in tools.values()),
            "toolchains_native": all(value["native"] for name, value in tools.items() if name not in ("node", "gn")),
            "compilers_native": all(value["native"] for name, value in tools.items() if name not in ("node", "gn", "bindgen")),
            "host_mismatch": any(value.get("wrong_host", False) for value in tools.values()),
            "host": {"platform": system, "arch": machine}, "tools": tools}


def validate_native_tools(src: Path, platform: str, arch: str) -> dict:
    # verify_restored supplies provenance, not Chromium's compiler stamp format.
    return inspect_native_tools(src, platform, arch)


def _safe_file(root: Path, name: str) -> Path:
    path = inside_path(root, name, output=True)
    if path is None:
        raise ValueError(f"linked or unsafe preparation path: {name}")
    return path


def _read_marker(path: Path) -> dict | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
        raise ValueError(f"invalid restored preparation marker: {path.name}")
    return value


def _stat_identity(path: Path) -> dict:
    try:
        info = path.stat()
        return {"path": str(path.resolve()), "size": info.st_size, "mtime_ns": info.st_mtime_ns}
    except (OSError, RuntimeError):
        return {"path": str(path), "missing": True}


def linux_sysroot_identity(src: Path, arch: str, *, host_arch: str) -> dict:
    """Record pinned host/target sysroots before an installer can replace them."""
    cpus = {"x64": "amd64", "arm64": "arm64"}
    if arch not in cpus or host_arch not in cpus:
        raise ValueError("unsupported Linux sysroot architecture")

    def identity(relative: str, *, directory=False) -> dict:
        path = _safe_file(src, relative)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return {"path": str(path), "missing": True}
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"invalid sysroot directory: {relative}")
            return {"path": str(path), "mode": stat.S_IMODE(info.st_mode), "mtime_ns": info.st_mtime_ns}
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"invalid sysroot stamp: {relative}")
        with path.open("rb") as stream:
            payload = stream.read(4097)
        if len(payload) > 4096:
            raise ValueError(f"oversized sysroot stamp: {relative}")
        after = path.lstat()
        if any(getattr(after, key) != getattr(info, key) for key in
               ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")):
            raise ValueError(f"sysroot stamp changed during inspection: {relative}")
        return {"path": str(path), "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                "sha256": hashlib.sha256(payload).hexdigest()}

    result = {}
    for cpu in sorted({cpus[arch], cpus[host_arch]}):
        relative = f"build/linux/debian_bullseye_{cpu}-sysroot"
        root = identity(relative, directory=True)
        stamp = identity(relative + "/.stamp")
        first_class = {path.name: identity(relative + "/" + path.name)
                       for path in sorted((src / relative).glob(".*_is_first_class_gcs"))}
        result[relative + "/.stamp"] = {"root": root, "stamp": stamp, "first_class": first_class}
    return result


def _relocated_sysroot_identity(identity: dict, names: set[str]) -> dict | None:
    """Normalize only complete hashed identities under one canonical source root."""
    if not isinstance(identity, dict) or set(identity) != names:
        return None
    source_root = None

    def normalize(record, relative, *, directory=False):
        nonlocal source_root
        fields = {"path", "mode", "mtime_ns"} if directory else {"path", "size", "mtime_ns", "sha256"}
        if not isinstance(record, dict) or set(record) != fields:
            raise ValueError("incomplete sysroot identity")
        numeric = "mode" if directory else "size"
        if (type(record[numeric]) is not int or not 0 <= record[numeric] <= (0o7777 if directory else 4096)
                or type(record["mtime_ns"]) is not int or record["mtime_ns"] < 0
                or not directory and (not isinstance(record["sha256"], str)
                                      or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None)):
            raise ValueError("invalid sysroot metadata")
        value = record["path"]
        if not isinstance(value, str) or any(char in value for char in ("\0", "\\", ":")):
            raise ValueError("invalid sysroot path")
        path, suffix = Path(value), Path(relative)
        if (not value.startswith("/") or value.startswith("//") or path.as_posix() != value
                or ".." in path.parts or path.parts[-len(suffix.parts):] != suffix.parts):
            raise ValueError("noncanonical sysroot path")
        root = path.parents[len(suffix.parts) - 1]
        if (root.name != "src" or source_root is not None and root != source_root
                or any(linked(parent) for parent in (path, *path.parents))):
            raise ValueError("inconsistent or linked sysroot root")
        source_root = root
        return dict(record, path=relative)

    result = {}
    try:
        for name, entry in identity.items():
            if (not isinstance(entry, dict) or set(entry) != {"root", "stamp", "first_class"}
                    or not isinstance(entry["first_class"], dict)):
                return None
            relative = name.rsplit("/", 1)[0]
            root = normalize(entry["root"], relative, directory=True)
            stamp = normalize(entry["stamp"], name)
            first_class = {}
            for filename, record in entry["first_class"].items():
                if (not isinstance(filename, str)
                        or re.fullmatch(r"\.[^/\\\x00]+_is_first_class_gcs", filename) is None):
                    return None
                first_class[filename] = normalize(record, relative + "/" + filename)
            result[name] = {"root": root, "stamp": stamp, "first_class": first_class}
    except (OSError, ValueError, RuntimeError, IndexError):
        return None
    return result


def _sysroots_changed(previous: dict | None, current: dict) -> bool | None:
    if previous is None:
        return None
    if "sysroot_identity" in previous:
        if previous["sysroot_identity"] == current:
            return False
        host = previous.get("host")
        if (type(previous.get("schema_version")) is not int or previous["schema_version"] != SCHEMA
                or previous.get("platform") != "linux" or not isinstance(host, dict)
                or host.get("platform") != "linux"
                or (previous.get("arch"), host.get("arch")) not in
                (("x64", "x64"), ("arm64", "arm64"), ("arm64", "x64"))):
            return True
        cpus = {"x64": "amd64", "arm64": "arm64"}
        names = {f"build/linux/debian_bullseye_{cpus[arch]}-sysroot/.stamp"
                 for arch in (previous["arch"], host["arch"])}
        before = _relocated_sysroot_identity(previous["sysroot_identity"], names)
        after = _relocated_sysroot_identity(current, names)
        return before is None or after is None or before != after
    environment = previous.get("environment")
    if not isinstance(environment, dict) or not isinstance(environment.get("sysroots"), dict):
        return None
    # Schema-2 finish markers predate content hashes; retain their stat baseline.
    legacy = environment["sysroots"]
    for name, entry in current.items():
        stamp = entry["stamp"]
        before = legacy.get(name)
        if name in legacy and (not isinstance(before, dict)
                               or not isinstance(before.get("path"), str)
                               or type(before.get("size")) is not int
                               or type(before.get("mtime_ns")) is not int):
            return True
        if stamp.get("missing"):
            if before is not None or not entry["root"].get("missing"):
                return True
        elif before != {key: value for key, value in stamp.items() if key != "sha256"}:
            return True
        if entry["first_class"]:
            return True
    return False


def environment_identity(src: Path, platform: str) -> dict:
    keys = ("ImageOS", "ImageVersion", "RUNNER_OS", "RUNNER_ARCH", "RUNNER_NAME",
            "GITHUB_JOB", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "DEVELOPER_DIR", "SDKROOT",
            "WindowsSdkDir", "WindowsSDKVersion", "VCToolsInstallDir", "VCToolsVersion",
            "UniversalCRTSdkDir", "UCRTVersion", "VSINSTALLDIR", "GYP_MSVS_OVERRIDE_PATH",
            "INCLUDE", "LIB", "CPATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH")
    result = {"host": list(host_identity()), "release": host_platform.release(),
              "version": host_platform.version(), "environment": {key: os.environ.get(key, "") for key in keys}}
    sdk_paths = [Path(os.environ[key]) for key in ("SDKROOT", "WindowsSdkDir", "VCToolsInstallDir") if os.environ.get(key)]
    if platform == "macos":
        commands = (("xcode-select", "--print-path"), ("xcrun", "--sdk", "macosx", "--show-sdk-path"),
                    ("xcrun", "--sdk", "macosx", "--show-sdk-version"),
                    ("xcrun", "--sdk", "macosx", "--show-sdk-build-version"), ("xcodebuild", "-version"))
        for command in commands:
            completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       check=True, timeout=30)
            value = completed.stdout.strip()
            result[" ".join(command)] = value
            if command[-1] == "--show-sdk-path":
                sdk_paths.append(Path(value))
    result["sdks"] = []
    sdk_contents = {}
    for path in sdk_paths:
        root = _stat_identity(path)
        entry = {"root": root, "settings": {
            name: digest_file(path / name) for name in ("SDKSettings.json", "SDKSettings.plist", "System/Library/CoreServices/SystemVersion.plist")
            if (path / name).is_file()}}
        if platform == "macos":
            # SDKROOT and xcrun often name the same tree; reuse only within this inspection.
            key = root["path"]
            if key not in sdk_contents:
                sdk_contents[key] = sdk_content_identity(path)
            entry["content"] = sdk_contents[key]
        result["sdks"].append(entry)
    # Sysroot stamps, unlike GN-generated sdk links, survive graph regeneration.
    result["sysroots"] = {str(path.relative_to(src)): _stat_identity(path)
                          for path in sorted((src / "build/linux").glob("*sysroot/.stamp"))}
    return result


def environments_compatible(previous: dict | None, current: dict, *, allow_sdk_drift=True) -> bool:
    """Compare build inputs without discarding stored scheduling provenance."""
    fields = {"host": list, "release": str, "version": str, "environment": dict,
              "sdks": list, "sysroots": dict}
    ignored = {"RUNNER_NAME", "GITHUB_JOB", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"}
    for identity in (previous, current):
        if (not isinstance(identity, dict)
                or any(not isinstance(identity.get(key), kind) for key, kind in fields.items())
                or len(identity["host"]) != 2
                or not all(isinstance(value, str) and value for value in identity["host"])
                or not all(isinstance(key, str) and isinstance(value, str)
                           for key, value in identity["environment"].items())
                or not any(key not in ignored for key in identity["environment"])):
            return False

    macos = previous["host"][0] == current["host"][0] == "macos"
    if macos:
        for identity in (previous, current):
            for sdk in identity["sdks"]:
                if not isinstance(sdk, dict):
                    return False
                if "content" in sdk and not validated_sdk_content(sdk["content"]):
                    return False

    def verified_sdks(identity):
        commands = ("xcode-select --print-path", "xcrun --sdk macosx --show-sdk-path",
                    "xcrun --sdk macosx --show-sdk-version", "xcrun --sdk macosx --show-sdk-build-version",
                    "xcodebuild -version")
        if (not identity["sdks"] or not identity["environment"].get("ImageVersion")
                or any(not isinstance(identity.get(key), str) or not identity[key] for key in commands)):
            return False
        for sdk in identity["sdks"]:
            root = sdk.get("root")
            if (not validated_sdk_content(sdk.get("content")) or not isinstance(root, dict)
                    or "missing" in root or not isinstance(root.get("path"), str)
                    or not Path(root["path"]).is_absolute()
                    or any(type(root.get(key)) is not int or root[key] < 0 for key in ("size", "mtime_ns"))
                    or not isinstance(sdk.get("settings"), dict) or not sdk["settings"]
                    or any(not isinstance(name, str) or not isinstance(value, str)
                           or re.fullmatch(r"[0-9a-f]{64}", value) is None
                           for name, value in sdk["settings"].items())):
                return False
        return True

    matching_content = (allow_sdk_drift and macos and verified_sdks(previous) and verified_sdks(current)
                        and [sdk["content"] for sdk in previous["sdks"]]
                        == [sdk["content"] for sdk in current["sdks"]])
    if matching_content:
        ignored.add("ImageVersion")

    def compatibility(identity):
        result = dict(identity, environment={key: value for key, value in identity["environment"].items()
                                             if key not in ignored})
        if matching_content:
            result["sdks"] = [dict(sdk, root={key: value for key, value in sdk["root"].items()
                                              if key not in ("size", "mtime_ns")}) for sdk in identity["sdks"]]
        return result

    return compatibility(previous) == compatibility(current)


def tool_fingerprint(src: Path, platform: str, arch: str) -> dict:
    """Identify tool content, including runtime libraries, without stamp conventions."""
    result = {}
    def walk_error(error):
        raise error

    for relative in (CLANG, RUST):
        root = src / relative
        if linked(root) or not root.is_dir():
            raise ValueError(f"missing or linked toolchain root: {relative}")
        digest = hashlib.sha256()
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            dirs.sort()
            for name in sorted(dirs + files):
                path = Path(directory) / name
                rel = path.relative_to(root).as_posix()
                if linked(path):
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(src):
                        raise ValueError(f"external toolchain link: {relative}/{rel}")
                    target = str(resolved.relative_to(src))
                    value = [rel, "link", target]
                elif path.is_file():
                    value = [rel, "file", stat.S_IMODE(path.stat().st_mode), digest_file(path)]
                else:
                    continue
                digest.update(json.dumps(value).encode())
        result[relative.as_posix()] = digest.hexdigest()
    return result


def generator_paths(platform: str, arch: str, *, host_arch: str | None = None) -> dict[str, Path]:
    host_arch = arch if host_arch is None else host_arch
    paths = {"node": tool_paths(platform, arch, host_arch=host_arch)["node"]}
    if platform == "windows":
        paths["go"] = Path("third_party/dawn/tools/golang/windows-amd64/bin/go.exe")
    else:
        system = "mac" if platform == "macos" else "linux"
        cpu = "amd64" if host_arch == "x64" else "arm64"
        paths["go"] = Path(f"third_party/dawn/tools/golang/{system}-{cpu}/bin/go")
        if platform == "linux":
            paths.update(gperf=Path("third_party/gperf/cipd/bin/gperf"),
                         clang_format=Path("buildtools/linux64-format/clang-format"))
    return paths


def generator_fingerprint(src: Path, platform: str, arch: str, *, host_arch: str | None = None) -> dict:
    result = {}
    for name, relative in generator_paths(platform, arch, host_arch=host_arch).items():
        path = src / relative
        result[name] = {"path": relative.as_posix(),
                        "sha256": digest_file(path) if path.is_file() else None}
    return result


def prepare_linux_typescript(src: Path, *, host_arch: str, repair=False, repo: Path = ROOT) -> dict:
    """Replace only the pinned portablelinux DevTools wrapper; never use host tsc."""
    pins = load_pins(repo, "linux")
    if tuple(pins[key] for key in ("ChromiumVersion", "UngoogledCommit", "UngoogledLinuxCommit")) != (
            "153.0.8010.36", "dd8fb9b5c837982faf41ba58cd30a5664e77c329",
            "a5ffa5e4a9fb722b97a5cf7966e29450a150c3dd") or host_arch not in TYPESCRIPT_REPAIRED:
        raise ValueError("unverified Linux TypeScript wrapper pins/host")

    def regular(relative):
        path = _safe_file(src, relative)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_size:
            raise ValueError(f"missing, empty or linked TypeScript input: {relative}")
        return path

    wrapper = regular(TYPESCRIPT_WRAPPER)
    original = wrapper.read_bytes()
    before = hashlib.sha256(original).hexdigest()
    if before not in (TYPESCRIPT_PORTABLE, *TYPESCRIPT_REPAIRED.values()):
        raise ValueError("unknown restored Linux TypeScript wrapper")
    package = regular(TYPESCRIPT_PACKAGE + "/package.json")
    if hashlib.sha256(package.read_bytes()).hexdigest() != (
            "3004f96b830f722041ea418dc29642d934fc64dcc207992e22a1dc37c7b270ae"):
        raise ValueError("unexpected pinned TypeScript package metadata")
    for name in ("tsc.js", "_tsc.js", *TYPESCRIPT_LIBRARIES):
        regular(TYPESCRIPT_PACKAGE + "/lib/" + name)
    digest = hashlib.sha256()
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(package.parent, followlinks=False, onerror=walk_error):
        dirs.sort()
        for name in dirs:
            _safe_file(src, (Path(directory) / name).relative_to(src).as_posix())
        for name in sorted(files):
            path = regular((Path(directory) / name).relative_to(src).as_posix())
            digest.update(json.dumps([path.relative_to(package.parent).as_posix(), digest_file(path)]).encode())

    desired = TYPESCRIPT_REPAIRED[host_arch]
    result = {"path": TYPESCRIPT_WRAPPER, "sha256": before,
              "package_sha256": digest.hexdigest(), "version": "6.0.2",
              "node": tool_paths("linux", host_arch)["node"].as_posix(),
              "repair_needed": before != desired}
    if not repair:
        return result
    node = src / result["node"]
    if host_arch not in binary_architectures(node, "linux") or not os.access(node, os.X_OK):
        raise ValueError("TypeScript Node does not match the native host")
    completed = subprocess.run([str(node), str(src / TYPESCRIPT_PACKAGE / "lib/tsc.js"), "--version"],
                               cwd=src, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               check=False, timeout=30)
    if completed.returncode or completed.stdout.strip() != "Version 6.0.2":
        raise ValueError(f"pinned TypeScript probe failed: {completed.stdout[:2000]}")
    if before != desired:
        lines = original.splitlines(keepends=True)
        start = lines.index(b"def GetBinaryPath():\n")
        end = lines.index(b"def RunTypeScriptRaw(cmd_parts, stdout=None):\n")
        getter = f'''def GetBinaryPath():
    return os_path.normpath(os_path.join(os_path.dirname(__file__), '..', '..',
                                        'node_modules', 'typescript', 'lib', 'tsc.js'))


def GetNodePath():
    return os_path.normpath(os_path.join(os_path.dirname(__file__), '..', '..',
                                        '..', '..', 'node', 'linux',
                                        'node-linux-{host_arch}', 'bin', 'node'))


'''.encode()
        tail = [b"    cmd = [GetNodePath(), GetBinaryPath()] + cmd_parts\n"
                if line == b"    cmd = [GetBinaryPath()] + cmd_parts\n" else
                b"        cmd = [GetNodePath(), GetBinaryPath()] + cmd_parts\n"
                if line == b"        cmd = [GetBinaryPath()] + cmd_parts\n" else line
                for line in lines[end:]]
        restored = b"".join(lines[:start]) + getter + b"".join(tail)
        if hashlib.sha256(restored).hexdigest() != desired:
            raise ValueError("unexpected repaired TypeScript wrapper")
        if regular(TYPESCRIPT_WRAPPER).read_bytes() != original:
            raise ValueError("TypeScript wrapper changed during preparation")
        wrapper.write_bytes(restored)
    result.update(sha256=desired, repair_needed=False)
    return result


def invalidate_generated_outputs(src: Path, *, removed_paths: set[str] | None = None) -> dict:
    """Invalidate logged outputs without compiler deps when generators are unverified."""
    out = src / "out/Default"
    dependencies = ninja_deps(out / ".ninja_deps")
    result = {"removed_outputs": 0, "unknown_outputs": []}
    for name in ninja_log(out / ".ninja_log"):
        if name in dependencies or Path(name.replace("\\", "/")).name in ("gn", "gn.exe"):
            continue
        output = inside_path(out, name, output=True)
        if output is None:
            result["unknown_outputs"].append(name)
        elif _remove_output(output):
            result["removed_outputs"] += 1
            if removed_paths is not None:
                removed_paths.add(output.resolve().as_posix())
    return result


def _remove_output(path: Path | None) -> bool:
    if path is not None and path.is_file() and not linked(path):
        path.unlink()
        return True
    return False


def _sdk_dependency_roots(src: Path, environment: dict, external_inputs) -> dict[Path, Path]:
    """Map verified SDK spellings and restored aliases to their hashed roots."""
    if not isinstance(environment.get("sdks"), list):
        return {}
    roots = {Path(sdk["root"]["path"]) for sdk in environment["sdks"]
             if isinstance(sdk, dict) and validated_sdk_content(sdk.get("content"))
             and isinstance(sdk.get("root"), dict) and isinstance(sdk["root"].get("path"), str)}
    if not roots:
        return {}
    aliases = set(roots)
    if environment["environment"].get("SDKROOT"):
        aliases.add(Path(environment["environment"]["SDKROOT"]))
    aliases.add(Path(environment["xcrun --sdk macosx --show-sdk-path"]))
    link_root = Path("out/Default/sdk/xcode_links")
    for value in external_inputs:
        relative = Path(value)
        if relative.parent == link_root and re.fullmatch(r"MacOSX(?:[0-9]+(?:\.[0-9]+)*)?\.sdk", relative.name):
            alias = src / relative
            if alias.is_symlink():
                aliases.add(alias)
    result = {}
    for alias in sorted(aliases):
        try:
            target = alias.resolve(strict=True)
            if alias.is_absolute() and target in roots and target.is_dir():
                result[alias] = target
        except (OSError, ValueError, RuntimeError):
            continue
    return result


def _covered_sdk_dependency(candidate: Path, roots: dict[Path, Path]) -> bool:
    # Reject lexical escapes and arbitrary external links into an SDK.
    if ".." in candidate.parts:
        return False
    for alias, root in roots.items():
        if not candidate.is_relative_to(alias):
            continue
        mapped = root / candidate.relative_to(alias)
        resolved = candidate.resolve(strict=True)
        if (alias.resolve(strict=True) == root and mapped.resolve(strict=True) == resolved
                and resolved.is_relative_to(root) and resolved.is_file()):
            return True
    return False


def invalidate_external_dependencies(src: Path, *, invalidate_all=False, recheck_external=True,
                                     external_inputs=(), removed_generated=(), verified_sdk_roots=None) -> dict:
    out = src / "out/Default"
    if any(linked(parent) for parent in (out, *out.parents)):
        raise ValueError("linked output root")
    records = ninja_deps(out / ".ninja_deps")
    result = {"dependency_records": len(records), "external_dependency_outputs": 0,
              "missing_dependency_outputs": 0, "removed_outputs": 0,
              "toolchain_invalidated_outputs": 0, "unknown_outputs": [],
              "input_reason_outputs": {}, "input_samples": []}
    inputs = {}
    source_root = src.resolve()
    external_roots = tuple(source_root / value for value in external_inputs)
    removed_generated = set(removed_generated)
    for name, (_, dependencies) in records.items():
        output = inside_path(out, name, output=True)
        if output is None:
            result["unknown_outputs"].append({"output": name, "reason": "unsafe or nonlocal output"})
            continue
        external = missing = False
        reasons = set()
        if recheck_external:
            for dependency in dependencies:
                if dependency not in inputs:
                    value = dependency.replace("\\", "/")
                    try:
                        lexical = Path(os.path.abspath(out / value))
                        omitted = any(lexical == root or lexical.is_relative_to(root) for root in external_roots)
                        candidate = out / value
                        path = candidate.resolve()
                        covered = (verified_sdk_roots and not PureWindowsPath(value).drive and ":" not in value
                                   and _covered_sdk_dependency(candidate, verified_sdk_roots))
                        outside = bool(not covered and (Path(value).is_absolute() or PureWindowsPath(value).drive
                                       or ":" in value or not path.is_relative_to(source_root) or omitted))
                        absent = not outside and not path.is_file()
                        reason = ("omitted_external_input" if omitted and not covered else "external_input" if outside else
                                  "removed_generated_input" if absent and path.as_posix() in removed_generated else
                                  "missing_local_input" if absent else None)
                        inputs[dependency] = (outside, absent, reason)
                    except (OSError, ValueError, RuntimeError):
                        inputs[dependency] = (True, False, "unresolved_input")
                outside, absent, reason = inputs[dependency]
                external |= outside
                missing |= absent
                if reason is not None and reason not in reasons:
                    reasons.add(reason)
                    counts = result["input_reason_outputs"]
                    counts[reason] = counts.get(reason, 0) + 1
                    if len(result["input_samples"]) < 32:
                        result["input_samples"].append({"output": name[:2048],
                                                        "input": dependency[:2048], "reason": reason})
        if external or missing:
            result["external_dependency_outputs" if external else "missing_dependency_outputs"] += 1
        if invalidate_all or external or missing:
            if _remove_output(output):
                result["removed_outputs"] += 1
                result["toolchain_invalidated_outputs"] += int(invalidate_all)
    return result


def invalidate_compiled_outputs(src: Path) -> dict:
    """Cover Rust/archive/host binaries not recorded in GCC-style Ninja deps."""
    out = src / "out/Default"
    if any(linked(parent) for parent in (out, *out.parents)):
        raise ValueError("linked output root")
    result = {"removed_outputs": 0, "unknown_outputs": []}

    def walk_error(error):
        raise error

    for directory, dirs, files in os.walk(out, followlinks=False, onerror=walk_error):
        dirs[:] = [name for name in dirs if not _sdk_root(Path(directory) / name, out)
                   and not linked(Path(directory) / name)]
        for name in files:
            relative = (Path(directory) / name).relative_to(out).as_posix()
            path = inside_path(out, relative, output=True)
            if path is None:
                continue
            compiled = path.suffix.lower() in COMPILED_SUFFIXES | {".exe", ".dll", ".so", ".dylib"}
            if not compiled:
                with path.open("rb") as stream:
                    header = stream.read(4)
                compiled = header in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf") or header[:2] == b"MZ"
            if compiled and _remove_output(path):
                result["removed_outputs"] += 1
    return result


def remove_final_products(src: Path, platform: str) -> list[str]:
    out = src / "out/Default"
    names = {"linux": ("chrome", "chrome_crashpad_handler", "chrome_sandbox"),
             "macos": ("Chromium.app",),
             "windows": ("chrome.exe", "chrome.dll", "chrome_elf.dll")}[platform]
    removed = []
    for name in names:
        path = inside_path(out, name, output=True)
        if path is None:
            raise ValueError("symlinked final build product")
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(name)
        elif _remove_output(path):
            removed.append(name)
    return removed


def restore_tool_endpoints(src: Path) -> list[str]:
    names = ("tools/clang/scripts/update.py", "tools/clang/scripts/build.py",
             "tools/rust/update_rust.py", "tools/rust/build_rust.py", "tools/rust/build_bindgen.py",
             "build/linux/sysroot_scripts/install-sysroot.py",
             "build/linux/sysroot_scripts/sysroots.json")
    changed = []
    for name in names:
        path = _safe_file(src, name)
        text = path.read_text(encoding="utf-8")
        restored = text
        for blocked, endpoint in ENDPOINTS.values():
            restored = restored.replace(blocked, endpoint)
        if restored != text:
            path.write_text(restored, encoding="utf-8")
            changed.append(name)
    return changed


def repair_linux_arm64_tool_script(src: Path) -> None:
    """Complete the four Rust hunks skipped by the pinned malformed overlay."""
    path = _safe_file(src, "tools/rust/build_rust.py")
    text = path.read_text(encoding="utf-8")
    replacements = (
        ("{OPENSSL_CIPD_LINUX_AMD_PATH}", '{OPENSSL_CIPD_LINUX_AMD_PATH.replace("amd64", GetHostSysrootPlatform())}'),
        ("return 'x86_64-unknown-linux-gnu'", "return f'{platform.machine()}-unknown-linux-gnu'"),
        ("DownloadDebianSysroot('amd64', args.skip_checkout)",
         "DownloadDebianSysroot(\n            GetHostSysrootPlatform(), args.skip_checkout)"),
    )
    for before, after in replacements:
        if before in text:
            if text.count(before) != 1:
                raise ValueError("ambiguous pinned Rust ARM64 workaround")
            text = text.replace(before, after)
        elif after not in text and not (before.startswith("DownloadDebianSysroot") and re.search(
                r"DownloadDebianSysroot\(\s*GetHostSysrootPlatform\(\), args\.skip_checkout\)", text)):
            raise ValueError("unknown restored Rust ARM64 build script")
    block = re.compile(r"(?m)^( +)'--disable-asserts',\n\1'--no-tools',")
    matches = list(block.finditer(text))
    if len(matches) == 1:
        text = block.sub(lambda match: (match[1] + "'--disable-asserts',\n" +
                         "".join(match[1] + repr(flag) + ",\n" for flag in
                                 ("--use-system-cmake", "--host-cc=clang", "--host-cxx=clang++")) +
                         match[1] + "'--no-tools',"), text)
    elif matches or not re.search(
            r"(?m)^( +)'--disable-asserts',\n\1'--use-system-cmake',\n\1'--host-cc=clang',\n\1'--host-cxx=clang\+\+',\n\1'--no-tools',", text):
        raise ValueError("unknown restored Rust LLVM build arguments")
    if "GetHostSysrootPlatform, GitRevert" not in text:
        raise ValueError("restored Rust script lacks the pinned host-sysroot import")
    if text != path.read_text(encoding="utf-8"):
        path.write_text(text, encoding="utf-8")


def verify_tooling(work: Path, platform: str, repo: Path = ROOT) -> None:
    pins = load_pins(repo, platform)
    names = {"ungoogled-chromium": pins["UngoogledCommit"]}
    if platform == "macos":
        names["ungoogled-chromium-macos"] = pins["UngoogledMacOSCommit"]
    for name, commit in names.items():
        path = _safe_file(work, "tooling/" + name)
        head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], text=True,
                              stdout=subprocess.PIPE, check=True, timeout=30).stdout.strip()
        if head != commit:
            raise ValueError(f"tooling checkout does not match repository pins: {name}")
        if name == "ungoogled-chromium-macos":
            for filename in ("downloads-x86-64.ini", "downloads-arm64.ini",
                             "downloads-x86-64-rustlib.ini", "downloads-arm64-rustlib.ini"):
                downloads = configparser.ConfigParser()
                downloads.read(path / filename)
                if not downloads.sections():
                    raise ValueError(f"missing pinned platform downloads: {filename}")
                for section in downloads.sections():
                    destination = Path(downloads[section]["output_path"])
                    if ".." in destination.parts or not any(destination == allowed or allowed in destination.parents for allowed in
                               (CLANG, RUST, Path("third_party/node/mac"), Path("third_party/node/mac_arm64"))):
                        raise ValueError(f"platform resource would overwrite source: {section}")
        subprocess.run(["git", "-C", str(path), "diff", "--exit-code", "HEAD", "--", ".", ":(exclude)ungoogled-chromium"],
                       stdout=subprocess.PIPE, check=True, timeout=30)


def prepare_tooling_links(work: Path, platform: str, arch: str, *, host_arch: str | None = None) -> None:
    host_arch = arch if host_arch is None else host_arch

    def link(relative, target):
        path = work / relative
        if any(linked(parent) for parent in path.parents):
            raise ValueError(f"linked tooling parent: {relative}")
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
        elif path.exists():
            raise ValueError(f"refusing to replace non-link tooling entry: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target, target_is_directory=target.is_dir())

    if platform == "macos":
        base = "tooling/ungoogled-chromium-macos/"
        (work / "download_cache").mkdir(exist_ok=True)
        link(base + "ungoogled-chromium", work / "tooling/ungoogled-chromium")
        link(base + "build/src", work / "src")
        link(base + "build/download_cache", work / "download_cache")
        tools = {f"third_party/dawn/tools/golang/mac-{'arm64' if host_arch == 'arm64' else 'amd64'}/bin/go": "go"}
    else:
        tools = {f"third_party/node/linux/node-linux-{cpu}/bin/node": "node" for cpu in ("x64", host_arch)}
        tools.update({"third_party/gperf/cipd/bin/gperf": "gperf", "buildtools/linux64-format/clang-format": "clang-format",
                      f"third_party/dawn/tools/golang/linux-{'arm64' if host_arch == 'arm64' else 'amd64'}/bin/go": "go"})
    for relative, name in tools.items():
        target = shutil.which(name)
        if not target:
            raise ValueError(f"required host tool is missing: {name}")
        path = work / "src" / relative
        if path.is_file() and not linked(path):
            # Only the fixed host-tool paths are replaceable here.
            _safe_file(work / "src", relative).unlink()
        link("src/" + relative, Path(target))


def prepare(workdir: Path, platform: str, arch: str, *, phase="finish", repo: Path = ROOT) -> dict:
    workdir = workdir.absolute()
    report_path = _safe_file(workdir, "upstream-cache-preparation.json")
    data = {"schema_version": SCHEMA, "phase": phase, "platform": platform, "arch": arch,
            "ready_for_gn": False, "operation": "verify_restored"}
    write_json(report_path, data)
    try:
        return _prepare(workdir, platform, arch, phase=phase, repo=repo, report_path=report_path, data=data)
    except (OSError, ValueError, Miss, RuntimeError, subprocess.SubprocessError) as error:
        data.update(ready_for_gn=False, error=str(error))
        write_json(report_path, data)
        raise


def _prepare(workdir: Path, platform: str, arch: str, *, phase: str, repo: Path,
             report_path: Path, data: dict) -> dict:
    receipt = verify_restored(workdir, platform, arch, repo=repo)
    data.update(source_identity=receipt.get("identity"), operation="read_preparation_markers")
    src = workdir / "src"
    marker = _safe_file(src, MARKER)
    pending_path = _safe_file(src, INSPECTION)
    old, pending = _read_marker(marker), _read_marker(pending_path)
    for entry in (old, pending):
        if entry and (entry.get("platform"), entry.get("arch")) != (platform, arch):
            raise ValueError("restored preparation identity changed")
    if old and old.get("source_identity") != receipt.get("identity"):
        raise ValueError("restored preparation source identity changed")
    data["operation"] = "inspect_native_tools"
    inspection = validate_native_tools(src, platform, arch)
    data.update(inspection)
    if platform == "windows":
        data["operation"] = "repair_windows_midl"
        data["windows_midl"] = repair_windows_midl(src, platform, arch, receipt["identity"])
    if (platform, arch) == ("windows", "arm64"):
        data["operation"] = "validate_rust_libraries"
        data["rust_libraries"] = validate_rust_libraries(src, platform, arch)
    incompatible = (not inspection["toolchains_native"] or any(
        entry.get("wrong_host", False) for name, entry in inspection["tools"].items()
        if name not in ("node", "gn")))
    changed_since_inspect = bool(pending and any(
        pending.get("tools", {}).get(name, {}).get("file_identity") != entry.get("file_identity")
        for name, entry in inspection["tools"].items() if name not in ("node", "gn")))
    needs_invalidation = (incompatible or changed_since_inspect
                          or bool(pending and pending.get("needs_invalidation"))
                          or bool(pending and (platform, arch) == ("windows", "arm64")
                                  and pending.get("rust_libraries") != data["rust_libraries"]))
    data["operation"] = "generator_fingerprint"
    generators = generator_fingerprint(src, platform, arch, host_arch=inspection["host"]["arch"])
    if platform == "linux":
        data["operation"] = "inspect_linux_typescript"
        generators["typescript"] = prepare_linux_typescript(src, host_arch=inspection["host"]["arch"], repo=repo)
    generators_changed = (bool(pending and pending.get("generators_changed"))
                          or bool(pending and pending.get("generator_fingerprint") != generators)
                          or bool(old and old.get("generator_fingerprint") != generators))
    data.update(needs_invalidation=needs_invalidation, generator_fingerprint=generators,
                generators_changed=generators_changed, operation="validate_native_tools")
    sysroots_changed = False
    if platform == "linux":
        data["operation"] = "linux_sysroot_identity"
        sysroots = linux_sysroot_identity(src, arch, host_arch=inspection["host"]["arch"])
        old_change = _sysroots_changed(old, sysroots)
        pending_change = _sysroots_changed(pending, sysroots)
        sysroots_changed = (bool(pending and pending.get("sysroots_changed"))
                            or old_change is True or pending_change is True
                            or bool(old and old_change is None)
                            or bool(not old and pending and pending_change is None))
        data.update(sysroot_identity=sysroots, sysroots_changed=sysroots_changed,
                    operation="validate_native_tools")
    if phase not in ("inspect", "finish"):
        raise ValueError(f"unsupported preparation phase: {phase}")
    write_json(report_path, data)
    if phase == "inspect":
        write_json(pending_path, data)
        return data
    if platform == "linux":
        write_json(pending_path, dict(data, phase="inspect"))
    if not inspection["toolchains_native"] or not inspection["tools"]["node"]["native"]:
        failed = [f"{name} ({entry.get('reason', 'probe failed')})"
                  for name, entry in inspection["tools"].items() if name != "gn" and not entry["native"]]
        message = "restored toolchain/node cannot execute on the native host; prepare tools before finish: " + "; ".join(failed)
        data["error"] = message
        write_json(report_path, data)
        write_json(pending_path, dict(data, phase="inspect"))
        raise ValueError(message)
    if platform == "linux":
        data["operation"] = "prepare_linux_typescript"
        typescript = prepare_linux_typescript(src, host_arch=inspection["host"]["arch"], repair=True, repo=repo)
        generators_changed |= generators["typescript"] != typescript
        generators["typescript"] = typescript
        data["generators_changed"] = generators_changed
    data["operation"] = "environment_identity"
    environment = environment_identity(src, platform)
    data["operation"] = "tool_fingerprint"
    fingerprint = tool_fingerprint(src, platform, arch)
    tool_changed = needs_invalidation or bool(old and old.get("tool_fingerprint") != fingerprint)
    environment_changed = not old or not environments_compatible(old.get("environment"), environment)
    sdk_drift_recheck = (not environment_changed and not environments_compatible(
        old.get("environment"), environment, allow_sdk_drift=False))
    first_finish = old is None
    data["operation"] = "invalidate_outputs"
    products = remove_final_products(src, platform) if first_finish else []
    removed_generated = set()
    generated = (invalidate_generated_outputs(src, removed_paths=removed_generated)
                 if first_finish or generators_changed else {"removed_outputs": 0, "unknown_outputs": []})
    invalidate_all = tool_changed or sysroots_changed
    full_recheck = environment_changed or generators_changed or bool(generated["removed_outputs"])
    verified_sdk_roots = {}
    if platform == "macos":
        sdk_roots = _sdk_dependency_roots(src, environment, receipt["external_symlink_paths"])
        binding_keys = {alias: alias.relative_to(src).as_posix() if alias.is_relative_to(src) else str(alias)
                        for alias in sdk_roots}
        data["sdk_dependency_roots"] = {binding_keys[alias]: str(root) for alias, root in sdk_roots.items()}
        if sdk_drift_recheck and not full_recheck and not invalidate_all:
            previous_roots = old.get("sdk_dependency_roots")
            # Alias bindings are inputs too; legacy markers prove only canonical roots.
            verified_sdk_roots = {alias: root for alias, root in sdk_roots.items()
                                  if alias == root or isinstance(previous_roots, dict)
                                  and previous_roots.get(binding_keys[alias]) == str(root)}
    dependencies = invalidate_external_dependencies(
        src, invalidate_all=invalidate_all, recheck_external=full_recheck or sdk_drift_recheck,
        external_inputs=receipt["external_symlink_paths"], removed_generated=removed_generated,
        verified_sdk_roots=verified_sdk_roots)
    compiled = invalidate_compiled_outputs(src) if invalidate_all else {"removed_outputs": 0, "unknown_outputs": []}
    gn = inspection["tools"]["gn"]
    gn_path = _safe_file(src / "out/Default", Path(gn["path"]).name)
    if gn["exists"] and not gn["native"]:
        _remove_output(gn_path)
    removed_gn = gn["exists"] and not gn_path.exists()
    if removed_gn:
        data["native_tools"] = False
        data["tools"]["gn"].update(native=False, exists=False, reason="GN bootstrap required after tool invalidation")
    data.update(phase="finish", operation="complete", ready_for_gn=True, needs_invalidation=False, generators_changed=False,
                source_identity=receipt.get("identity"), environment=environment, tool_fingerprint=fingerprint,
                dependencies=dependencies, compiled_outputs=compiled, generated_outputs=generated, removed_gn=removed_gn,
                removed_final_products=products, counters={
                    "tool_swap_invalidations": int(tool_changed), "environment_rechecks": int(environment_changed),
                    "sdk_drift_external_rechecks": int(sdk_drift_recheck),
                    "generator_rechecks": int(first_finish or generators_changed),
                    "toolchain_invalidated_outputs": dependencies["toolchain_invalidated_outputs"] + compiled["removed_outputs"],
                    "first_finish": int(first_finish)})
    if platform == "linux":
        data["sysroots_changed"] = False
        data["counters"]["sysroot_invalidations"] = int(sysroots_changed)
    write_json(marker, data)
    write_json(report_path, data)
    pending_path.unlink(missing_ok=True)
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("inspect", "finish"), default="finish")
    parser.add_argument("--platform", choices=("linux", "macos", "windows"), required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        data = prepare(args.workdir.absolute(), args.platform, args.arch, phase=args.phase)
        print(json.dumps(data, sort_keys=True))
    except (OSError, ValueError, Miss, RuntimeError, subprocess.SubprocessError) as error:
        print(f"restored build preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
