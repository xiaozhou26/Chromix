#!/usr/bin/env python3
"""Install the exact Linux157 bundled libclang and exercise the real V8 metagen."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile

try:
    from . import linux157_source as source
    from . import linux157_toolchain as toolchain
except ImportError:
    import linux157_source as source
    import linux157_toolchain as toolchain

MANIFEST = "build/linux/157.0.8103.78/libclang.json"
MANIFEST_SHA256 = "31e7f007180a568dc88e03ae573b3a58c3fc6aef6834504a8e750280d2c3244e"
PACKAGE = "third_party/llvm-libclang"
IN_PROGRESS = ".chromix-linux157-libclang-in-progress"
RESOURCE = "third_party/llvm-build/Release+Asserts/lib/clang/24"
HEADER = '''#include <stddef.h>
namespace v8 { namespace internal {
using Tagged_t = unsigned int;
using IndirectPointerHandle = unsigned int;
using Address = unsigned long;
using ExternalPointer_t = unsigned long;
using CppHeapPointer_t = unsigned long;
using TrustedPointer_t = unsigned long;
class HeapObject { public: unsigned int map_; };
class ProbeObject : public HeapObject { public: int value_; };
}}
'''


def load(repo: Path = source.ROOT) -> dict:
    raw = source.regular(repo, MANIFEST).read_bytes()
    if source.digest(raw) != MANIFEST_SHA256:
        raise ValueError("unreviewed Linux157 libclang manifest")
    return json.loads(raw)


def verify_package(root: Path, manifest: dict, *, allow_missing=False) -> list[str]:
    missing = []
    if root.is_symlink():
        raise ValueError("linked libclang package root")
    actual = set()
    if root.exists():
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("linked libclang package member")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
    extra = actual - set(manifest["files"])
    if extra:
        raise ValueError("unexpected libclang files: " + ", ".join(sorted(extra)))
    for name, pin in manifest["files"].items():
        path = root / name
        if path.exists() and not path.is_file():
            raise ValueError("non-regular libclang input: " + name)
        if not path.exists():
            missing.append(name)
        elif (source.regular(root, name).stat().st_size != pin["size"]
              or toolchain.file_hash(path) != pin["sha256"]):
            raise ValueError("changed libclang input: " + name)
    if missing and not allow_missing:
        raise ValueError("missing libclang inputs: " + ", ".join(missing))
    return missing


def extract(archive: Path, destination: Path, manifest: dict) -> None:
    if archive.stat().st_size != manifest["download"]["size"] or toolchain.file_hash(archive) != manifest["download"]["sha256"]:
        raise ValueError("libclang archive identity mismatch")
    seen = set()
    with tarfile.open(archive, "r:xz") as stream:
        for member in stream:
            name = member.name
            if (name not in manifest["files"] or name in seen or not member.isfile()
                    or member.size != manifest["files"][name]["size"]):
                raise ValueError("unexpected libclang archive inventory")
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(member) as incoming, path.open("xb") as output:
                shutil.copyfileobj(incoming, output)
            path.chmod(member.mode & 0o755)
            seen.add(name)
    verify_package(destination, manifest)


def install(src: Path, repo: Path = source.ROOT) -> dict:
    manifest = load(repo)
    source.hashes(src, manifest["source_inputs"])
    deps = source.regular(src, "DEPS").read_text()
    if (manifest["download"]["sha256"] not in deps
            or manifest["download"]["url"].rsplit("/", 1)[1] not in deps):
        raise ValueError("Linux157 libclang download is not the exact DEPS package")
    # Both architectures run the generator on Linux x64; this is not the target ELF.
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("libclang metagen requires a Linux x64 host")
    root = src
    for part in PACKAGE.split("/"):
        root = root / part
        if root.is_symlink():
            raise ValueError("linked libclang installation path")
    missing = verify_package(root, manifest, allow_missing=True)
    if missing:
        cache = src.parent / "download_cache/linux157-tools"
        archive = toolchain.download(manifest["download"], cache / "libclang157.tar.xz")
        with tempfile.TemporaryDirectory(prefix="libclang157-", dir=cache) as temporary:
            stage = Path(temporary) / "package"
            stage.mkdir()
            extract(archive, stage, manifest)
            root.mkdir(parents=True, exist_ok=True)
            # Existing known files remain intact; unknown bytes were rejected above.
            for name in missing:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    output.write((stage / name).read_bytes())
                target.chmod((stage / name).stat().st_mode & 0o755)
    verify_package(root, manifest)
    library = source.regular(root, "lib/libclang.so")
    with library.open("rb") as stream:
        header = stream.read(20)
    if header[:6] != b"\x7fELF\x02\x01" or header[16:20] != b"\x03\x00\x3e\x00":
        raise ValueError("libclang must be an x86_64 Linux ELF shared library")
    return manifest


def probe(src: Path, manifest: dict) -> dict:
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONPATH", "PYTHONHOME", "LIBCLANG_PATH", "LD_LIBRARY_PATH", "LD_PRELOAD")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    api = '''import json,sys
from pathlib import Path
root=Path(sys.argv[1]); sys.path.insert(0,str(root/'bindings/python'))
from clang import cindex
assert Path(cindex.__file__).resolve()==root/'bindings/python/clang/cindex.py'
cindex.Config.set_library_file(str(root/'lib/libclang.so'))
lib=cindex.conf.lib;lib.clang_getClangVersion.restype=cindex._CXString
version=cindex._CXString.from_result(lib.clang_getClangVersion())
index=cindex.Index.create()
tu=index.parse('probe.cc',args=['-std=c++20'],unsaved_files=[('probe.cc','struct Probe { int x; };')])
assert not list(tu.diagnostics)
assert any(c.spelling=='Probe' and c.type.get_size()==4 for c in tu.cursor.get_children())
print(json.dumps({'version':version,'bindings':cindex.__file__,'api_parse':'passed'}))
'''
    result = json.loads(subprocess.check_output([sys.executable, "-I", "-B", "-c", api, str(src / PACKAGE)],
                                               env=env, text=True, timeout=60))
    if result["version"] != manifest["clang_version"]:
        raise ValueError("libclang/bindings/compiler revision mismatch")
    with tempfile.TemporaryDirectory(prefix="linux157-metagen-probe-", dir=src.parent) as temporary:
        root = Path(temporary)
        (root / "objects.h").write_text(HEADER)
        (root / "driver.cc").write_text('#include "objects.h"\n')
        commands = [{"directory": str(root), "file": "driver.cc",
                     "arguments": ["clang++", "-std=c++20", "-c", "driver.cc"]}]
        (root / "compile_commands.json").write_text(json.dumps(commands))
        subprocess.run([sys.executable, "-B", str(src / "v8/tools/metagen/metagen.py"),
                        "--libclang-dir", str(src / PACKAGE), "--clang-resource-dir", str(src / RESOURCE),
                        "--driver", str(root / "driver.cc"), "--v8-root", str(root), "--out", str(root / "generated"),
                        "--compile-commands", str(root / "compile_commands.json"), "--driver-mode=gcc"],
                       env=env, check=True, timeout=120)
        layouts = json.loads((root / "generated/layouts.json").read_text())
        classes = {entry["cpp_name"]: entry for entry in layouts["classes"]}
        item = classes["v8::internal::ProbeObject"]
        if (layouts["config"]["pointer_size"] != 8 or item["size"] != 8
                or item["base_size"] != 4 or item["fields"][0]["offset"] != 4
                or "PROBE_OBJECT_TYPE" not in (root / "generated/instance-types.h").read_text()):
            raise ValueError("actual metagen hierarchy/layout probe failed")
    result["metagen"] = "instance-types-and-layouts-passed"
    return result


def check_ninja_inputs(src: Path) -> dict:
    out = src / "out/Chromix"
    matches, missing, generated = [], [], []
    for path in sorted(out.rglob("toolchain.ninja")):
        for line in path.read_text().splitlines():
            if (not line.startswith("build ") or ":" not in line
                    or "metagen/instance-types.h" not in line.split(":", 1)[0]):
                continue
            if " | " not in line:
                raise ValueError("unknown metagen Ninja input form")
            inputs = line.split(" | ", 1)[1].split(" || ", 1)[0].split()
            for name in inputs:
                if name.startswith("phony/") or "/phony/" in name:
                    continue
                if "$" in name:
                    raise ValueError("unresolved metagen Ninja input")
                target = (out / name).resolve()
                if not target.is_relative_to(src.resolve()):
                    missing.append(name)
                elif not target.is_file():
                    # v8_dump_build_config is the declared Ninja producer, not a package input.
                    if name.endswith("v8_build_config.json") and ("build " + name + ":") in path.read_text():
                        generated.append(name)
                    else:
                        missing.append(name)
            matches.append(path.relative_to(src).as_posix())
    if not matches or missing:
        raise ValueError("metagen Ninja dependencies incomplete: " + json.dumps(sorted(set(missing))))
    return {"toolchains": matches, "missing": [], "generated_by_ninja": sorted(set(generated))}


def prepare(src: Path, repo: Path = source.ROOT) -> dict:
    marker = src / IN_PROGRESS
    if marker.exists() or marker.is_symlink():
        raise ValueError("interrupted libclang preparation; preserve failed tree")
    marker.write_text(MANIFEST_SHA256 + "\n")
    manifest = install(src, repo)
    result = {"schema_version": 1, "package_sha256": manifest["download"]["sha256"],
              "files": manifest["files"], "probe": probe(src, manifest),
              "ninja_inputs": check_ninja_inputs(src), "host": "linux-x64"}
    (src / ".chromix-linux157-libclang.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    marker.unlink()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=source.ROOT)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(prepare(args.src.resolve(), args.repo), sort_keys=True))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError) as error:
        print("Linux157 libclang: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
