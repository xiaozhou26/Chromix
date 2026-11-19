#!/usr/bin/env python3
"""Exact downstream Linux157 source identity and platform override preparation."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VERSION = "157.0.8103.78"
CORE = "8c886dffc6ade39d8f7df5d64ace5a8824b455e1"
PLATFORM = "6f0aa178c2394fc0a177edc0fd64e41f402a0418"
BASE_CORE = "28a211ab4ab6b83d919966a4f58a6df23cf4fb29"
BASE_REF = "156.0.8078.79-1"
MANIFEST = "build/linux/157.0.8103.78/source.json"
MANIFEST_SHA256 = "5ad5898d9442cc31b6ed571702f9e88510e9eea144ed544f60187e66e5c948d8"
RECEIPT = ".chromix-linux157-source.json"
IN_PROGRESS = ".chromix-linux157-in-progress"
LITE_PATH = "build/windows/lite-tarball-files/v8/test/torque/test-torque.tq"
LITE_SHA256 = "502c92560d745fc94e93ec7b95a1c562a55167f58a2ce79e3d0e4881dfb928c5"
PATCH_OPTIONS = ("-p1", "--fuzz=0", "--batch", "--forward", "--get=0",
                 "--no-backup-if-mismatch", "--reject-file=-")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def regular(root: Path, relative: str) -> Path:
    parts = relative.split("/")
    if not relative or any(p in ("", ".", "..") for p in parts) or "\\" in relative:
        raise ValueError("unsafe Linux157 path")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("linked Linux157 input: " + str(path))
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("missing or hard-linked Linux157 input: " + str(path))
    return path


def load_manifest(repo: Path = ROOT) -> dict:
    raw = regular(repo, MANIFEST).read_bytes()
    if digest(raw) != MANIFEST_SHA256:
        raise ValueError("unverified Linux157 source manifest")
    data = json.loads(raw)
    if (data["chromium_version"], data["core_commit"], data["platform_commit"],
            data["platform_ref"], data["platform_core_gitlink"], data["official_platform_tag"]) != (
            VERSION, CORE, PLATFORM, BASE_REF, BASE_CORE, None):
        raise ValueError("invalid Linux157 source override identity")
    paths = {entry["path"] for entry in data["patches"]}
    directory = repo / "build/linux/157.0.8103.78/patches"
    if directory.is_symlink() or {p.relative_to(repo).as_posix() for p in directory.iterdir()} != paths:
        raise ValueError("unexpected Linux157 override inventory")
    for entry in data["patches"]:
        if digest(regular(repo, entry["path"]).read_bytes()) != entry["sha256"]:
            raise ValueError("changed Linux157 override: " + entry["path"])
    return data


def validate_pins(pins: dict, repo: Path = ROOT) -> dict:
    expected = {"ChromiumVersion": VERSION, "UngoogledVersion": VERSION + "-1",
                "UngoogledCommit": CORE, "UngoogledLinuxVersion": BASE_REF,
                "UngoogledLinuxCommit": PLATFORM}
    if any(pins.get(k) != v for k, v in expected.items()):
        raise ValueError("unverified Linux157 downstream pins")
    return load_manifest(repo)


def recipe(repo: Path = ROOT) -> dict:
    manifest = load_manifest(repo)
    files = [MANIFEST, "tools/linux157_source.py", "tools/linux157_toolchain.py",
             "tools/linux157_build.py", "tools/linux157_gn.py", "tools/linux157_checkpoint.py",
             "build/linux/157.0.8103.78/pre-gn-checkpoints.json", "tools/linux157_libclang.py",
             "tools/linux157_postgn.py", "build/linux/157.0.8103.78/libclang.json",
             "build/linux/157.0.8103.78/post-gn-checkpoints.json", "tools/linux157_locale.py",
             "build/linux/157.0.8103.78/locale-checkpoint.json", LITE_PATH]
    if digest(regular(repo, LITE_PATH).read_bytes()) != LITE_SHA256:
        raise ValueError("changed Linux157 lite payload")
    files += [entry["path"] for entry in manifest["patches"]]
    inputs = {name: digest(regular(repo, name).read_bytes()) for name in files}
    return {"kind": "linux157-downstream-fresh", "chromium_version": VERSION,
            "core_commit": CORE, "platform_commit": PLATFORM, "platform_ref": BASE_REF,
            "platform_core_gitlink": BASE_CORE, "core_override": CORE,
            "official_platform_tag": None,
            "review": {"path": MANIFEST, "sha256": MANIFEST_SHA256}, "inputs": inputs,
            "sha256": digest(json.dumps(inputs, sort_keys=True).encode())}


def git(path: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    return subprocess.check_output(["git", "-C", str(path), *args], env=env, text=True).strip()


def get_platform(core: Path, tooling: Path, repo: Path = ROOT) -> dict:
    manifest = load_manifest(repo)
    if git(core, "rev-parse", "HEAD") != CORE or git(tooling, "rev-parse", "HEAD") != PLATFORM:
        raise ValueError("Linux157 core/platform checkout mismatch")
    if git(core, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("changed Linux157 core checkout")
    hashes(core, manifest["core_inputs"])
    unknown = git(core, "ls-files", "--others", "--exclude-standard", "utils").splitlines()
    if any(not name.endswith(".pyc") for name in unknown):
        raise ValueError("untracked Linux157 core utility")
    if git(tooling, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all"):
        raise ValueError("changed Linux157 platform baseline")
    link = git(tooling, "ls-tree", "HEAD", "ungoogled-chromium").split()
    if link != ["160000", "commit", BASE_CORE, "ungoogled-chromium"]:
        raise ValueError("Linux157 baseline core gitlink mismatch")
    if (regular(core, "chromium_version.txt").read_text().strip() != VERSION or
            regular(core, "revision.txt").read_text().strip() != "1"):
        raise ValueError("Linux157 core version mismatch")
    verify_upstream(tooling, manifest)
    hashes(tooling, manifest["platform_inputs"])
    return recipe(repo)


def verify_upstream(tooling: Path, manifest: dict) -> None:
    series = regular(tooling, "patches/series").read_bytes()
    if digest(series) != manifest["upstream_series_sha256"]:
        raise ValueError("Linux157 baseline series mismatch")
    names = [line.strip() for line in series.decode().splitlines() if line.strip()]
    if names != [entry["upstream_path"] for entry in manifest["patches"]]:
        raise ValueError("Linux157 platform coverage/order mismatch")
    for entry in manifest["patches"]:
        path = regular(tooling, "patches/" + entry["upstream_path"])
        if digest(path.read_bytes()) != entry["upstream_sha256"]:
            raise ValueError("changed upstream platform patch: " + entry["upstream_path"])


def init_subrepos(work: Path, repo: Path = ROOT) -> tuple[Path, Path]:
    load_manifest(repo)
    paths = []
    for name, commit in (("ungoogled-chromium", CORE),
                         ("ungoogled-chromium-portablelinux", PLATFORM)):
        path = work / "tooling" / name
        if path.is_symlink():
            raise ValueError("linked tooling checkout")
        if not path.exists():
            path.mkdir(parents=True)
            git(path, "init")
            git(path, "remote", "add", "origin", "https://github.com/ungoogled-software/" + name + ".git")
        if git(path, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all"):
            raise ValueError("refusing to overwrite modified tooling")
        if git(path, "remote", "get-url", "origin") != "https://github.com/ungoogled-software/" + name + ".git":
            raise ValueError("unexpected tooling remote")
        git(path, "fetch", "--depth=1", "origin", commit)
        git(path, "checkout", "--detach", commit)
        paths.append(path)
    get_platform(*paths, repo)
    # The independent core path is intentional; never rewrite the baseline gitlink.
    return paths[0], paths[1]


def hashes(src: Path, expected: dict) -> None:
    for name, value in expected.items():
        if digest(regular(src, name).read_bytes()) != value:
            raise ValueError("Linux157 source identity mismatch: " + name)


def apply_platform(src: Path, tooling: Path, repo: Path = ROOT,
                   patch_bin: str | None = None) -> dict:
    manifest = load_manifest(repo)
    verify_upstream(tooling, manifest)
    if (src / IN_PROGRESS).exists() or (src / RECEIPT).exists():
        raise ValueError("Linux157 source already prepared or interrupted; preserve snapshot and use a clean source")
    program = patch_bin or shutil.which("gpatch") or shutil.which("patch")
    if not program:
        raise ValueError("GNU patch is required")
    if "GNU patch" not in subprocess.check_output([program, "--version"], text=True):
        raise ValueError("GNU patch is required")
    (src / IN_PROGRESS).write_text(recipe(repo)["sha256"] + "\n")
    results = []
    try:
        for entry in manifest["patches"]:
            hashes(src, entry["inputs"])
            args = [program, *PATCH_OPTIONS, "-i", str(repo / entry["path"])]
            subprocess.run([*args, "--dry-run"], cwd=src, check=True)
            subprocess.run(args, cwd=src, check=True)
            hashes(src, entry["outputs"])
            results.append({"patch": entry["path"], "sha256": entry["sha256"]})
        hashes(src, manifest["final_outputs"])
        for name in manifest["final_outputs"]:
            if name.endswith(".py"):
                ast.parse(regular(src, name).read_text())
    except (OSError, ValueError, subprocess.SubprocessError, SyntaxError) as error:
        (src / ".chromix-linux157-failure.json").write_text(json.dumps(
            {"recipe": recipe(repo), "applied": results, "error": str(error)}, indent=2) + "\n")
        raise
    result = {"schema_version": 1, "recipe": recipe(repo), "platform_patches": results,
              "stage": "platform-applied", "upstream_cache": False}
    (src / RECEIPT).write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    (src / IN_PROGRESS).unlink()
    return result


def verify_receipt(src: Path, repo: Path = ROOT) -> dict:
    data = json.loads(regular(src, RECEIPT).read_bytes())
    manifest = load_manifest(repo)
    expected_patches = [{"patch": entry["path"], "sha256": entry["sha256"]}
                        for entry in manifest["patches"]]
    if (data.get("schema_version") != 1 or data.get("recipe") != recipe(repo) or
            data.get("platform_patches") != expected_patches or
            data.get("stage") not in ("platform-applied", "chromix-applied") or
            data.get("upstream_cache") is not False or (src / IN_PROGRESS).exists() or
            (src / ".chromix-upstream-restored.json").exists()):
        raise ValueError("invalid Linux157 fresh-source receipt")
    expected = data.get("prepared_outputs")
    if data["stage"] == "chromix-applied":
        if (not isinstance(expected, dict) or not set(manifest["final_outputs"]).issubset(expected)
                or data.get("target_arch") not in ("x64", "arm64")
                or not isinstance(data.get("chromix_selection"), dict)):
            raise ValueError("incomplete Linux157 prepared source identity")
    if expected is not None:
        if (src / ".chromix-domain-substituted").exists():
            expected = data.get("substituted_outputs")
            if not isinstance(expected, dict):
                raise ValueError("missing Linux157 substituted source identity")
        for name, value in expected.items():
            path = src / name
            if value is None:
                if path.exists() or path.is_symlink():
                    raise ValueError("unexpected restored source file: " + name)
            elif digest(regular(src, name).read_bytes()) != value:
                raise ValueError("changed Linux157 prepared source: " + name)
    return data


def prepare(work: Path, arch: str, repo: Path = ROOT) -> None:
    try:
        from .platform_pins import load_pins
    except ImportError:
        from platform_pins import load_pins
    validate_pins(load_pins(repo, "linux"), repo)
    if arch not in ("x64", "arm64"):
        raise ValueError("unsupported Linux157 target")
    try:
        from . import patch_selection
    except ImportError:
        import patch_selection
    # Refuse an unmerged selector before downloading any source.
    selection, _ = patch_selection.select(repo, "linux")
    work = work.resolve()
    src = work / "src"
    if src.exists():
        try:
            from .linux157_locale import verify as verify_migrated
        except ImportError:
            from linux157_locale import verify as verify_migrated
        verify_migrated(work, arch, repo)
        receipt = verify_receipt(src, repo)
        expected_ready = "linux157-fresh|" + arch + "|" + recipe(repo)["sha256"]
        if (regular(src, ".chromix-source-ready").read_text().strip() != expected_ready or
                receipt.get("chromix_selection") != selection):
            raise ValueError("incomplete or changed Linux157 preparation; preserve failure snapshot")
        get_platform(work / "tooling/ungoogled-chromium",
                     work / "tooling/ungoogled-chromium-portablelinux", repo)
        return
    raise ValueError("Linux157 recovery requires the exact latest compiled locale snapshot; fresh preparation is disabled")


def validate_route(repo: Path = ROOT, arch: str = "x64") -> dict:
    try:
        from .platform_pins import load_pins
    except ImportError:
        from platform_pins import load_pins
    validate_pins(load_pins(repo, "linux"), repo)
    if arch not in ("x64", "arm64"):
        raise ValueError("unsupported Linux157 target")
    if (os.environ.get("CHROMIX_USE_UPSTREAM_CACHE", "0") != "0"
            or os.environ.get("CHROMIX_UPSTREAM_CACHE_DIR")):
        raise ValueError("Linux157 requires explicit fresh mode without a donor cache")
    return recipe(repo)


def checkpoint(work: Path, arch: str, repo: Path = ROOT) -> dict:
    try:
        from . import patch_selection
    except ImportError:
        import patch_selection
    validate_route(repo, arch)
    work = work.resolve()
    src = work / "src"
    try:
        from .linux157_locale import verify as verify_migrated
    except ImportError:
        from linux157_locale import verify as verify_migrated
    verify_migrated(work, arch, repo)
    receipt = verify_receipt(src, repo)
    selection, _ = patch_selection.select(repo, "linux", src=src)
    if (receipt.get("stage") != "chromix-applied" or receipt.get("target_arch") != arch
            or receipt.get("chromix_selection") != selection
            or regular(src, ".chromix-source-ready").read_text().strip() !=
            "linux157-fresh|" + arch + "|" + recipe(repo)["sha256"]
            or (src / ".chromix-domain-substitution-in-progress").exists()):
        raise ValueError("incomplete or changed Linux157 checkpoint")
    get_platform(work / "tooling/ungoogled-chromium",
                 work / "tooling/ungoogled-chromium-portablelinux", repo)
    try:
        from .linux157_toolchain import preflight_inputs
    except ImportError:
        from linux157_toolchain import preflight_inputs
    if not (src / ".chromix-domain-substituted").exists():
        # A completed source may be checkpointed before tool installation succeeds.
        preflight = preflight_inputs(src, load_manifest(repo), restore=False)
        if preflight["recoverable_missing"]:
            raise ValueError("incomplete Linux157 checkpoint toolchain inputs")
    return {"schema_version": 1, "status": "verified", "platform": "linux", "arch": arch,
            "recipe": recipe(repo), "source_verified": True, "runtime_verified": False,
            "output": "src/out/Chromix", "qualification": "same-recipe fresh checkpoint, not build success"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--phase", choices=("identity", "route", "checkpoint", "prepare", "verify", "init-subrepos", "get-platform", "apply-platform"), required=True)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--src", type=Path)
    parser.add_argument("--core", type=Path)
    parser.add_argument("--platform-tooling", type=Path)
    parser.add_argument("--arch", choices=("x64", "arm64"), default="x64")
    args = parser.parse_args(argv)
    try:
        if args.phase == "identity":
            result = recipe(args.repo)
        elif args.phase == "route":
            result = validate_route(args.repo, args.arch)
        elif args.phase == "checkpoint" and args.workdir:
            result = checkpoint(args.workdir, args.arch, args.repo)
        elif args.phase == "prepare" and args.workdir:
            prepare(args.workdir, args.arch, args.repo)
            result = verify_receipt(args.workdir / "src", args.repo)
        elif args.phase == "verify" and args.src:
            result = verify_receipt(args.src, args.repo)
        elif args.phase == "init-subrepos" and args.workdir:
            result = {"paths": [str(p) for p in init_subrepos(args.workdir, args.repo)]}
        elif args.phase == "get-platform" and args.core and args.platform_tooling:
            result = get_platform(args.core, args.platform_tooling, args.repo)
        elif args.phase == "apply-platform" and args.src and args.platform_tooling:
            result = apply_platform(args.src, args.platform_tooling, args.repo)
        else:
            parser.error("missing phase-specific path arguments")
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("Linux157 source: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
