#!/usr/bin/env python3
"""Provision hash-pinned LLVM Python bindings for the actual Windows metagen action."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile
import urllib.request

LLVM_COMMIT = "ea7d852a70e8bdfaf601d6626a760f9771b2c4b4"  # llvmorg-23.1.0
LLVM_VERSION = "23.1.0"
V8_REVISION = "56832c166a78bd1eb61068ac69876d6880eaabdf"
CHROMIUM_VERSION = "155.0.8059.39"
HASHES = {
    "__init__.py": "961d8ad419dc7aa10d8d7b75e5fcd624cc681403a55ce99ad14ae4fe4a7be749",
    "cindex.py": "0d572be5704bd7065420166bbe5e5b3fb5943423bd73ba6e40e4f8e9253c3f08",
}
SOURCE_PACKAGES = (
    "third_party/llvm-build/Release+Asserts/bindings/python/clang",
    "third_party/llvm-libclang/bindings/python/clang",
)
TARGET = "gen/v8/metagen/instance-types.h"
MAX_FILE = 1024 * 1024
PINS = {
    "154.0.8037.97": None,
    CHROMIUM_VERSION: {"v8_revision": V8_REVISION, "target_cpus": ("x64", "arm64")},
    "155.0.8059.79": {
        "v8_revision": "d89479ac5446a374c55a35be19b5a23437558d99",
        "target_cpus": ("x64", "arm64"),
    },
}
AMD64 = 0x8664


def source_contract(src: Path, out: Path, version: str) -> str | None:
    if version not in PINS:
        raise ValueError("unsupported Chromium metagen bindings pin")
    pin = PINS[version]
    if pin is None:
        return None
    version_text = (src / "chrome/VERSION").read_text(encoding="utf-8")
    values = re.findall(r"(?m)^(MAJOR|MINOR|BUILD|PATCH)=([0-9]+)$", version_text)
    if (len(version_text.splitlines()) != 4 or len(values) != 4
            or dict(values) != dict(zip(("MAJOR", "MINOR", "BUILD", "PATCH"), version.split(".")))):
        raise ValueError("source chrome/VERSION does not match exact Chromium pin")
    args = (out / "args.gn").read_text(encoding="utf-8")
    selected = {}
    for key in ("target_cpu", "v8_target_cpu", "host_cpu", "target_os"):
        assignments = re.findall(r"(?m)^\s*" + key + r"\s*=([^\r\n]*)", args)
        if not assignments and key != "target_cpu":
            continue
        if len(assignments) != 1:
            raise ValueError(f"expected exactly one literal GN {key}")
        match = re.fullmatch(r'\s*"([a-z0-9]+)"\s*(?:#.*)?', assignments[0])
        if not match:
            raise ValueError(f"expected exactly one literal GN {key}")
        selected[key] = match[1]
    arch = selected["target_cpu"]
    if (arch not in pin["target_cpus"] or selected.get("v8_target_cpu", arch) != arch
            or selected.get("host_cpu", "x64") != "x64" or selected.get("target_os", "win") != "win"):
        raise ValueError("metagen requires a Windows x64 or arm64 target on an x64 host")
    return arch


def runtime_info() -> dict:
    result = dict(platform=sys.platform, bits=struct.calcsize("P") * 8,
                  site_packages=list(set(sysconfig.get_path(k) for k in ("purelib", "platlib"))))
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.IsWow64Process2.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT),
                                          ctypes.POINTER(wintypes.USHORT))
        kernel.IsWow64Process2.restype = wintypes.BOOL
        process, native = wintypes.USHORT(), wintypes.USHORT()
        if not kernel.IsWow64Process2(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
            raise ctypes.WinError(ctypes.get_last_error())
        result.update(process_machine=process.value or native.value, native_machine=native.value)
    return result


def validate_runtime(runtime: dict) -> None:
    if (runtime.get("platform") != "win32" or runtime.get("bits") != 64
            or runtime.get("process_machine") != AMD64 or runtime.get("native_machine") != AMD64):
        raise ValueError("metagen requires a 64-bit Windows AMD64 Python on a native AMD64 host")


def validate_library(library: Path) -> None:
    with library.open("rb") as stream:
        dos = stream.read(64)
        if len(dos) != 64 or dos[:2] != b"MZ":
            raise ValueError("metagen libclang.dll requires a PE AMD64 DLL")
        offset = struct.unpack_from("<I", dos, 60)[0]
        size = library.stat().st_size
        if offset < 64 or offset + 26 > size:
            raise ValueError("metagen libclang.dll has an invalid PE header offset")
        stream.seek(offset)
        header = stream.read(26)
        machine, sections = struct.unpack_from("<HH", header, 4)
        optional_size, flags, magic = struct.unpack_from("<HHH", header, 20)
        if (header[:4] != b"PE\0\0" or machine != AMD64 or magic != 0x20B
                or flags & 0x2002 != 0x2002 or not 1 <= sections <= 96
                or optional_size < 112 or offset + 24 + optional_size + sections * 40 > size):
            raise ValueError("metagen libclang.dll requires a PE32+ AMD64 DLL")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for parent in (path, *path.parents):
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError(f"linked bindings path: {parent}")
    return path


def checked(data: bytes, name: str) -> bytes:
    if len(data) > MAX_FILE or digest(data) != HASHES[name]:
        raise ValueError(f"LLVM {LLVM_VERSION} bindings SHA256 mismatch: {name}")
    return data


def read_package(package: Path) -> dict[str, bytes]:
    local(package)
    result = {}
    for name in HASHES:
        path = local(package / name)
        if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > MAX_FILE:
            raise ValueError(f"invalid bindings file: {path}")
        result[name] = checked(path.read_bytes(), name)
    return result


def split_command(command: str) -> list[str]:
    # GN emits Windows paths with literal backslashes, not POSIX escapes.
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)


def action_contract(commands: str, out: Path) -> dict:
    actions = []
    for line in commands.splitlines():
        if "metagen.py" not in line:
            continue
        argv = split_command(line)
        if len(argv) < 2 or not argv[1].replace("\\", "/").endswith("/v8/tools/metagen/metagen.py"):
            raise ValueError("unsupported metagen Python command")
        options = {}
        for index, word in enumerate(argv[2:], 2):
            if word in ("--libclang-so", "--libclang-bindings-dir", "--expect-libclang-major",
                        "--libclang-dir", "--clang-resource-dir"):
                if word in options or index + 1 == len(argv) or argv[index + 1].startswith("--"):
                    raise ValueError(f"invalid metagen option: {word}")
                options[word] = argv[index + 1]
        def absolute(value):
            path = Path(value)
            return Path(os.path.abspath(path if path.is_absolute() else out / path))
        resource = absolute(options.get("--clang-resource-dir", ""))
        if resource.name != "23":
            raise ValueError("metagen requires the pinned clang resource major 23")
        if "--libclang-dir" in options:
            if any(key in options for key in ("--libclang-so", "--libclang-bindings-dir", "--expect-libclang-major")):
                raise ValueError("mixed bundled/native metagen options")
            root = absolute(options["--libclang-dir"])
            library, bindings = root / "bin/libclang.dll", root / "bindings/python"
        else:
            if options.get("--expect-libclang-major") != "23" or not all(
                    options.get(key) for key in ("--libclang-so", "--libclang-bindings-dir")):
                raise ValueError("metagen requires explicit native library, bindings and major 23")
            library = absolute(options["--libclang-so"])
            bindings = absolute(options["--libclang-bindings-dir"])
        python = shutil.which(argv[0]) if not Path(argv[0]).is_absolute() else argv[0]
        if not python:
            raise ValueError("metagen runtime Python not found")
        actions.append(dict(python=str(python), script=absolute(argv[1]), library=library,
                            bindings=bindings, resource=resource))
    if len(actions) != 1:
        raise ValueError(f"expected exactly one metagen action, found {len(actions)}")
    return actions[0]


def run_json(argv: list[str], *, cwd: Path) -> dict:
    result = subprocess.run(argv, cwd=cwd, check=True, text=True, capture_output=True, timeout=120)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return json.loads(result.stdout)


def validate_paths(contract: dict, src: Path, runtime: dict) -> None:
    validate_runtime(runtime)
    if contract["script"].resolve() != (src / "v8/tools/metagen/metagen.py").resolve():
        raise ValueError("metagen script is outside the source tree")
    allowed_libraries = [src / "third_party/llvm-build/Release+Asserts/bin/libclang.dll",
                         src / "third_party/llvm-libclang/bin/libclang.dll"]
    if not contract["library"].is_file() or contract["library"].resolve() not in [p.resolve() for p in allowed_libraries]:
        raise ValueError("metagen DLL is not the source toolchain library")
    validate_library(contract["library"])
    if contract["resource"].resolve() != (src / "third_party/llvm-build/Release+Asserts/lib/clang/23").resolve():
        raise ValueError("metagen resource directory is not the source clang 23 toolchain")
    if not (contract["resource"] / "include/stddef.h").is_file():
        raise ValueError("metagen clang builtin headers are missing")
    bindings = local(contract["bindings"])
    allowed = [Path(value) for value in runtime["site_packages"]]
    allowed += [src / relative for relative in (str(Path(p).parent) for p in SOURCE_PACKAGES)]
    if bindings not in [local(p) for p in allowed]:
        raise ValueError("metagen bindings directory is neither runtime Python site-packages nor a source bindings directory")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("bindings download redirects are forbidden")


def fetch_package() -> dict[str, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    package = {}
    for name in HASHES:
        url = f"https://raw.githubusercontent.com/llvm/llvm-project/{LLVM_COMMIT}/clang/bindings/python/clang/{name}"
        with opener.open(url, timeout=60) as response:
            if response.status != 200 or response.geturl() != url:
                raise ValueError("unexpected bindings download response")
            package[name] = checked(response.read(MAX_FILE + 1), name)
    return package


def provision(src: Path, bindings: Path, *, download: bool) -> str:
    package = local(bindings / "clang")
    if package.exists():
        unexpected = set(p.name for p in package.iterdir()) - set(HASHES) - {"__pycache__"}
        if unexpected:
            raise ValueError(f"unexpected files in clang package; refusing to overwrite a bundled runtime: {sorted(unexpected)}")
        for path in package.rglob("*"):
            local(path)
        try:
            read_package(package)
            cache = package / "__pycache__"
            if cache.exists():
                shutil.rmtree(cache)
            return "already_installed"
        except (ValueError, OSError):
            pass
    files = None
    origin = ""
    for relative in SOURCE_PACKAGES:
        candidate = src / relative
        if candidate == package or not candidate.exists():
            continue
        files = read_package(candidate)
        origin = str(candidate)
        break
    if files is None:
        if not download:
            raise ValueError("missing pinned LLVM bindings; provide source bindings or explicitly allow --download")
        files = fetch_package()
        origin = f"llvm-project@{LLVM_COMMIT}"
    # Never publish unchecked bytes, even when the caller supplies a local package.
    for name in HASHES:
        checked(files[name], name)
        path = local(package / name)
        if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
            raise ValueError(f"invalid destination bindings file: {path}")
    package.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".metagen-bindings-", dir=bindings) as temporary:
        for name, data in files.items():
            staged = Path(temporary) / name
            staged.write_bytes(data)
            os.replace(staged, package / name)
    cache = package / "__pycache__"
    if cache.exists():
        shutil.rmtree(cache)
    read_package(package)
    return origin


def probe(bindings: Path, library: Path, src: Path) -> dict:
    validate_runtime(runtime_info())
    validate_library(library)
    read_package(bindings / "clang")
    sys.path.insert(0, str(bindings))
    cindex = importlib.import_module("clang.cindex")
    if Path(cindex.__file__).resolve() != (bindings / "clang/cindex.py").resolve():
        raise ValueError("clang.cindex imported from an unexpected location")
    cindex.Config.set_library_file(str(library))
    cindex.Config.set_compatibility_check(True)
    version = cindex.Config().get_clang_version()  # Registers all required C API symbols.
    if re.search(r"\b23\.1\.0\b", version) is None:
        raise ValueError(f"expected libclang {LLVM_VERSION}, loaded {version}")
    index = cindex.Index.create()
    unit = index.parse("chromix-metagen-preflight.cc", args=["-x", "c++", "-std=c++20"],
                       unsaved_files=[("chromix-metagen-preflight.cc", "enum class ChromixProbe { Value = 23 }; ")])
    if any(d.severity >= cindex.Diagnostic.Error for d in unit.diagnostics):
        raise ValueError("libclang API parse preflight failed")
    enums = [cursor for cursor in unit.cursor.get_children() if cursor.kind == cindex.CursorKind.ENUM_DECL]
    if len(enums) != 1 or next(enums[0].get_children()).enum_value != 23:
        raise ValueError("libclang API cursor preflight failed")
    sys.path.insert(0, str(src / "v8/tools"))
    importlib.import_module("metagen.cpp_hier")
    return {"python": sys.executable, "bindings": cindex.__file__, "library": str(library),
            "libclang_version": version, "api_preflight": "passed"}


def configure(src: Path, out: Path, version: str) -> dict:
    if source_contract(src, out, version) is None:
        return {"status": "unchanged"}
    path = out / "args.gn"
    text = path.read_text(encoding="utf-8")
    pattern = r'(?m)^(\s*v8_metagen_libclang_so\s*=\s*)"([^"\r\n]*)"(\s*(?:#[^\r\n]*)?)$'
    matches = list(re.finditer(pattern, text))
    if not matches:
        if "v8_metagen_libclang_so" in text:
            raise ValueError("nonliteral metagen library GN argument")
        return {"status": "bundled"}
    if len(matches) != 1:
        raise ValueError("duplicate metagen library GN argument")
    match = matches[0]
    relative = "third_party/llvm-build/Release+Asserts/bin/libclang.dll"
    old = "C:/ungoogled-chromium-windows/build/src/" + relative
    if match[2] != old:
        return {"status": "unchanged"}
    library = src.resolve() / relative
    if not library.is_file():
        raise ValueError("restored source libclang DLL is missing")
    validate_library(library)
    replacement = match[1] + '"' + library.as_posix() + '"' + match[3]
    path.write_text(text[:match.start()] + replacement + text[match.end():], encoding="utf-8")
    return {"status": "rebased", "library": str(library)}


def install(src: Path, out: Path, ninja: Path, version: str, *, download: bool) -> dict:
    src, out = src.resolve(), out.resolve()
    arch = source_contract(src, out, version)
    if arch is None:
        return {"status": "not_required", "chromium_version": version}
    if not (src / "v8/tools/metagen/metagen.py").exists():
        raise ValueError("expected V8 metagen source is missing")
    deps = (src / "DEPS").read_text(encoding="utf-8")
    if re.findall(r"['\"]v8_revision['\"]\s*:\s*['\"]([0-9a-f]{40})['\"]", deps) != [PINS[version]["v8_revision"]]:
        raise ValueError("unexpected exact DEPS V8 revision")
    commands = subprocess.run([str(ninja), "-C", str(out), "-t", "commands", TARGET],
                              check=True, text=True, capture_output=True, timeout=120).stdout
    contract = action_contract(commands, out)
    runtime = run_json([contract["python"], "-I", "-c",
                        "import json,runpy; print(json.dumps(runpy.run_path("
                        + repr(str(Path(__file__).resolve())) + ")['runtime_info']()))"], cwd=out)
    validate_paths(contract, src, runtime)
    library_hash = digest(contract["library"].read_bytes())
    origin = provision(src, contract["bindings"], download=download)
    result = run_json([contract["python"], "-I", "-S", "-B", str(Path(__file__).resolve()), "--probe",
                       "--bindings", str(contract["bindings"]), "--library", str(contract["library"]),
                       "--src", str(src)], cwd=out)
    if digest(contract["library"].read_bytes()) != library_hash:
        raise ValueError("source libclang DLL changed during bindings provisioning")
    return dict(result, status="verified", origin=origin, target_cpu=arch, llvm_commit=LLVM_COMMIT,
                bindings_sha256=HASHES, library_sha256=library_hash)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--ninja", type=Path)
    parser.add_argument("--chromium-version")
    parser.add_argument("--download", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--probe", action="store_true")
    mode.add_argument("--configure", action="store_true")
    parser.add_argument("--bindings", type=Path)
    parser.add_argument("--library", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.probe:
            if not args.bindings or not args.library:
                parser.error("--probe requires --bindings and --library")
            result = probe(args.bindings, args.library, args.src)
        elif args.configure:
            if not args.out or not args.chromium_version:
                parser.error("--configure requires --out and --chromium-version")
            result = configure(args.src, args.out, args.chromium_version)
        else:
            if not args.out or not args.ninja or not args.chromium_version:
                parser.error("installation requires --out, --ninja and --chromium-version")
            result = install(args.src, args.out, args.ninja, args.chromium_version, download=args.download)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, ImportError, subprocess.SubprocessError) as error:
        print(f"Windows metagen bindings preflight failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stdout or "", file=sys.stderr)
            print(error.stderr or "", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
