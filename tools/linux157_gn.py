#!/usr/bin/env python3
"""Bootstrap host GN using exact official Linux157 clang and matching in-tree libc++."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

try:
    from . import linux157_toolchain as tools
except ImportError:
    import linux157_toolchain as tools

ENV_FLAGS = ("CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "CPATH", "C_INCLUDE_PATH",
             "CPLUS_INCLUDE_PATH", "LIBRARY_PATH", "GCC_EXEC_PREFIX", "COMPILER_PATH",
             "CC", "CXX", "AR", "LD", "NM", "CXX_host", "CC_host")
INPUTS = {
    "tools/gn/bootstrap/bootstrap.py": "983ab6d697037f05b58d1550b37fa133a37a7c9fb3e2e597fe774d5de9a9f3ca",
    "tools/gn/bootstrap/libc++.ninja": "2708e17cee9526856591c67a540db052b2bd3eb8e0da7d11ff9a3257f6fd4efe",
    "buildtools/third_party/libc++/__config_site": "fcac9dc8ea709997ab8b43af6c4e4e2a3674531c6f6303ca42eaf1e3fe189064",
    "third_party/llvm-libc/src/shared/fp_bits.h": "4f2e29e66446ae730c2b33d852352bece3122954888587dcb4f13a738f6867e5",
    "third_party/libc++/src/include/__config": "8a22f6575188bd4a8af218746301b442900356d0ad5b92e5747ac1d5e3a7a8dc",
    "third_party/libc++abi/src/include/__cxxabi_config.h": "a4981a422d321ad1eef57a4786bad355e374f97c685834908030f26740819927",
}
BINARIES = {
    "clang": "8091a95554f24a2affa32c80e884c3d36960c5d971560d3410391ffc36af19a7",
    "clang++": "8091a95554f24a2affa32c80e884c3d36960c5d971560d3410391ffc36af19a7",
    "llvm-ar": "7d3156f6ebb7dd4b24886e07d5feb66f80b44f210685fb69548df0dd1f814c5c",
}
VERSION = "2595 (0035961837fb)"


def native_elf(path: Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(20)
    if (header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\x3e\x00"
            or not os.access(path, os.X_OK)):
        raise ValueError("GN host tool is not executable Linux x86_64 ELF: " + str(path))


def host_environment(src: Path, env: dict) -> tuple[dict, dict]:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("GN bootstrap requires a Linux x64 host, independently of target_cpu")
    for name, digest in INPUTS.items():
        if tools.file_hash(tools.source.regular(src, name)) != digest:
            raise ValueError("changed official GN/libc++ input: " + name)
    selected = {}
    for key, name in (("CC", "clang"), ("CXX", "clang++"), ("AR", "llvm-ar")):
        binary = (src / "third_party/llvm-build/Release+Asserts/bin" / name).resolve(strict=True)
        if not binary.is_relative_to(src.resolve()):
            raise ValueError("external GN host compiler")
        native_elf(binary)
        if tools.file_hash(binary) != BINARIES[name]:
            raise ValueError("GN compiler does not match official Linux157 toolchain")
        selected[key] = str(binary)
    clean = {key: value for key, value in env.items() if key not in ENV_FLAGS}
    clean.update(selected)
    # The upstream bootstrap's libc++.ninja still names third_party/llvm/libc.
    clean["CXXFLAGS"] = "-isystem" + str(src / "third_party/llvm-libc/src")
    version = subprocess.check_output([selected["CXX"], "--version"], env=clean, text=True).splitlines()[0]
    if not version.startswith("clang version 24.0.0git"):
        raise ValueError("unexpected official GN compiler version")
    return clean, {"compiler": selected, "version": version, "binaries": BINARIES,
                   "inputs": INPUTS, "stdlib": "Chromium157 in-tree libc++/libc++abi"}


def gn_version(path: Path, env: dict) -> str:
    native_elf(path)
    version = subprocess.check_output([str(path), "--version"], env=env, text=True, timeout=30).strip()
    if version != VERSION:
        raise ValueError("unexpected checkpoint GN version")
    return version


def prepare(src: Path, out: Path, *, env: dict | None = None, jobs: int = 4) -> dict:
    src, out = src.resolve(), out.absolute()
    if out != src / "out/Chromix" or out.is_symlink() or not out.is_dir():
        raise ValueError("Linux157 GN requires the existing out/Chromix directory")
    clean, identity = host_environment(src, dict(os.environ) if env is None else env)
    gn = out / "gn"
    if gn.is_symlink():
        raise ValueError("linked GN output")
    if gn.exists():
        digest = tools.file_hash(gn)
        donor_gn = "25684acf0d11a3de5365c881b051f0c111e894e5d5a74898f81c2b04db25801b"
        if digest != donor_gn:
            result = json.loads(tools.source.regular(src, "out/linux157-host-gn-libcxx/result.json").read_bytes())
            library = tools.source.regular(out, "libc++.gn.so")
            if (result.get("sha256") != digest or result.get("host") != identity
                    or result.get("libcxx_sha256") != tools.file_hash(library)):
                raise ValueError("changed cached host GN or libc++")
        version = gn_version(gn, clean)
        return {"host": identity, "version": version, "sha256": digest, "reused": True}
    if type(jobs) is not int or jobs <= 0:
        raise ValueError("invalid GN bootstrap parallelism")
    # Upstream bootstrap uses fixed relative libc++ paths requiring this directory depth.
    build = src / "out/linux157-host-gn-libcxx"
    if build.is_symlink():
        raise ValueError("linked GN host build directory")
    build.mkdir(exist_ok=True)
    marker = build / "compiler.json"
    serialized = json.dumps(identity, sort_keys=True, indent=2) + "\n"
    if marker.is_symlink() or (marker.exists() and marker.read_text() != serialized):
        raise ValueError("GN bootstrap cache belongs to another host compiler")
    marker.write_text(serialized)
    candidate = build / "gn"
    subprocess.run([sys.executable, "tools/gn/bootstrap/bootstrap.py", "--build-path",
                    str(build.relative_to(src)), "-o", str(candidate), "--use-custom-libcxx",
                    "--skip-generate-buildfiles", "--jobs", str(jobs)], cwd=src, env=clean, check=True)
    version = gn_version(candidate, clean)
    library = build / "libc++.gn.so"
    native_elf(library)
    if (out / library.name).exists() or (out / library.name).is_symlink():
        raise ValueError("unreviewed existing GN runtime library")
    shutil.copy2(library, out / library.name)
    shutil.copy2(candidate, gn)
    gn_version(gn, clean)
    result = {"host": identity, "version": version, "sha256": tools.file_hash(gn),
              "libcxx_sha256": tools.file_hash(library), "reused": False}
    (build / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(prepare(args.src, args.src / "out/Chromix", jobs=args.jobs), sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("Linux157 host GN: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
