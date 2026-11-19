#!/usr/bin/env python3
"""Provision Linux157 official host tools without upstream object caches."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

try:
    from . import linux157_source as source
except ImportError:
    import linux157_source as source


def download(pin: dict, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError("linked download destination")
    if destination.exists():
        if destination.stat().st_size != pin["size"] or file_hash(destination) != pin["sha256"]:
            raise ValueError("existing download does not match Linux157 pin")
        return destination
    partial = destination.with_name(destination.name + ".partial")
    with partial.open("xb") as output:
        digest = hashlib.sha256()
        count = 0
        with urllib.request.urlopen(pin["url"], timeout=120) as response:
            while block := response.read(1024 * 1024):
                count += len(block)
                if count > pin["size"]:
                    raise ValueError("Linux157 download exceeds pinned size")
                output.write(block)
                digest.update(block)
        if count != pin["size"] or digest.hexdigest() != pin["sha256"]:
            raise ValueError("Linux157 download checksum/size mismatch")
    partial.replace(destination)
    return destination


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def unpack(archive: Path, destination: Path, strip: str = "") -> None:
    if destination.is_symlink():
        raise ValueError("linked tool destination")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as stream:
        count = 0
        total = 0
        for member in stream:
            name = member.name.removeprefix("./")
            if member.isdir() and name in ("", "."):
                continue
            if strip:
                if name == strip.rstrip("/"):
                    continue
                if not name.startswith(strip):
                    raise ValueError("unexpected tool archive prefix")
                name = name[len(strip):]
            parts = PurePosixPath(name).parts
            if not parts or name.startswith("/") or any(p == ".." for p in parts) or "\\" in name:
                raise ValueError("unsafe tool archive member")
            target = destination.joinpath(*parts)
            current = destination
            for part in parts:
                current = current / part
                if current.is_symlink():
                    raise ValueError("linked tool extraction path")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError("non-regular tool archive member")
            count += 1
            total += member.size
            if count > 200000 or total > 2 * 1024**3:
                raise ValueError("tool archive exceeds extraction limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(member) as incoming, target.open("xb") as output:
                shutil.copyfileobj(incoming, output)
            target.chmod(member.mode & 0o755)


def tree_identity(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("linked Linux157 tool inventory")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = file_hash(path)
    return result


def install_bundle(src: Path, cache: Path, pin: dict, name: str, relative: str,
                   *, strip: str = "", patch: Path | None = None) -> dict:
    archive = download(pin, cache / (name + ".tar.gz"))
    destination = src / relative
    if destination.is_symlink():
        raise ValueError("linked Linux157 tool bundle")
    with tempfile.TemporaryDirectory(prefix="linux157-tools-", dir=cache) as temporary:
        stage = Path(temporary) / "bundle"
        unpack(archive, stage, strip)
        if patch:
            program = shutil.which("gpatch") or shutil.which("patch")
            subprocess.run([program, *source.PATCH_OPTIONS, "-i", str(patch)], cwd=stage, check=True)
        expected = tree_identity(stage)
        if destination.exists():
            # The authenticated source archive may contain a filtered package.
            # Replace it only with the fully verified official dependency bundle.
            tree_identity(destination)
            previous = destination.with_name(destination.name + ".linux157-previous")
            if previous.exists() or previous.is_symlink():
                raise ValueError("interrupted Linux157 bundle installation")
            destination.rename(previous)
            try:
                shutil.move(str(stage), str(destination))
            except BaseException:
                previous.rename(destination)
                raise
            shutil.rmtree(previous)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stage), str(destination))
    return {"path": relative, "archive_sha256": pin["sha256"], "files": expected}


def prepare(src: Path, arch: str, repo: Path = source.ROOT) -> dict:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("Linux157 official host tools require Linux x64; ARM64 targets need a native runtime gate")
    if arch not in ("x64", "arm64"):
        raise ValueError("unsupported Linux157 target")
    if os.environ.get("CDS_CLANG_BUCKET_OVERRIDE"):
        raise ValueError("Linux157 official toolchain forbids a bucket override")
    receipt = source.verify_receipt(src, repo)
    if receipt.get("stage") != "chromix-applied" or receipt.get("target_arch") != arch:
        raise ValueError("Linux157 source preparation/target is incomplete")
    manifest = source.load_manifest(repo)
    if (src / ".chromix-domain-substituted").exists():
        receipt = json.loads(source.regular(src, ".chromix-linux157-toolchain.json").read_bytes())
        if receipt.get("recipe") != source.recipe(repo) or receipt.get("target_arch") != arch:
            raise ValueError("changed Linux157 toolchain receipt")
        for relative, expected in receipt["binaries"].items():
            binary = (src / relative).resolve(strict=True)
            if not binary.is_relative_to(src.resolve()) or file_hash(binary) != expected:
                raise ValueError("changed Linux157 tool binary: " + relative)
        return receipt
    source.hashes(src, manifest["toolchain_inputs"])
    cache = src.parent / "download_cache" / "linux157-tools"
    cache.mkdir(parents=True, exist_ok=True)
    downloads = manifest["downloads"]
    bundles = []
    bundles.append(install_bundle(src, cache, downloads["node"], "node", "third_party/node/linux"))
    bundles.append(install_bundle(src, cache, downloads["node_modules"], "node-modules", "third_party/node/node_modules"))
    bundles.append(install_bundle(src, cache, downloads["tsgo"], "tsgo", "third_party/typescript/linux-amd64/src",
                                  strip="package/", patch=src / "third_party/typescript/linux-amd64/3pp/patches/typescript_native_preview.patch"))
    with tempfile.TemporaryDirectory(prefix="linux157-esbuild-", dir=cache) as temporary:
        stage = Path(temporary) / "esbuild"
        unpack(download(downloads["esbuild"], cache / "esbuild.tar.gz"), stage, "package/")
        binary = source.regular(stage, "bin/esbuild")
        target = src / "third_party/devtools-frontend/src/third_party/esbuild/esbuild"
        if target.is_symlink():
            raise ValueError("linked esbuild target")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and file_hash(target) != file_hash(binary):
            raise ValueError("existing esbuild differs from pinned native binary")
        if not target.exists():
            shutil.copy2(binary, target)
    commands = [
        [sys.executable, "tools/rust/update_rust.py"],
        [sys.executable, "tools/clang/scripts/update.py"],
        [sys.executable, "build/linux/sysroot_scripts/install-sysroot.py", "--arch=amd64"],
    ]
    if arch == "arm64":
        commands.append([sys.executable, "build/linux/sysroot_scripts/install-sysroot.py", "--arch=arm64"])
    for command in commands:
        subprocess.run(command, cwd=src, check=True)
    node = src / "third_party/node/linux/node-linux-x64/bin/node"
    tsgo = src / "third_party/typescript/linux-amd64/src/lib/tsc"
    for binary, expected in ((node, "v24.12.0"), (tsgo, "Version 7.0.2"), (target, "0.28.2")):
        output = subprocess.check_output([str(binary), "--version"], text=True, timeout=30).strip()
        if output != expected:
            raise ValueError("unexpected Linux157 host tool version: " + str(binary))
    required = [node, tsgo, target, src / "third_party/llvm-build/Release+Asserts/bin/clang",
                src / "third_party/llvm-build/Release+Asserts/bin/clang++",
                src / "third_party/llvm-build/Release+Asserts/bin/llvm-ar",
                src / "third_party/rust-toolchain/bin/rustc",
                src / "third_party/rust-toolchain/bin/bindgen"]
    binaries = {}
    for binary in required:
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("missing official Linux157 tool: " + str(binary))
        binaries[binary.relative_to(src).as_posix()] = file_hash(binary)
    result = {"schema_version": 1, "recipe": source.recipe(repo), "target_arch": arch,
              "host_arch": "x64", "bundles": bundles, "commands": commands,
              "binaries": binaries, "runtime_verified": False, "upstream_cache": False}
    (src / ".chromix-linux157-toolchain.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=source.ROOT)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(prepare(args.src.resolve(), args.arch, args.repo), sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        print("Linux157 toolchain: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
