#!/usr/bin/env python3
"""Fresh Linux157 build entry point; CI retains failure snapshots and runtime gates."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

try:
    from . import linux157_source as source
    from . import linux157_toolchain as toolchain
except ImportError:
    import linux157_source as source
    import linux157_toolchain as toolchain


def build(work: Path, arch: str, repo: Path = source.ROOT) -> None:
    if toolchain.platform.system() != "Linux" or toolchain.platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("Linux157 fresh build requires a Linux x64 build host")
    if os.environ.get("CHROMIX_USE_UPSTREAM_CACHE", "0") != "0" or os.environ.get("CHROMIX_UPSTREAM_CACHE_DIR"):
        raise ValueError("Linux157 fresh builds cannot use a donor cache")
    source.prepare(work, arch, repo)
    src = work / "src"
    toolchain.prepare(src, arch, repo)
    core = work / "tooling/ungoogled-chromium"
    tooling = work / "tooling/ungoogled-chromium-portablelinux"
    source.get_platform(core, tooling, repo)
    receipt = source.verify_receipt(src, repo)
    out = src / "out/Chromix"
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    node = src / "third_party/node/linux/node-linux-x64/bin"
    env["PATH"] = str(node) + os.pathsep + env.get("PATH", "")
    llvm = src / "third_party/llvm-build/Release+Asserts/bin"
    for key, executable in (("CC", "clang"), ("CXX", "clang++"), ("AR", "llvm-ar"), ("NM", "llvm-nm")):
        env[key] = str(llvm / executable)
    # These small build utilities remain explicit host prerequisites.
    for executable, relative in (("gperf", "third_party/gperf/cipd/bin/gperf"),
                                 ("go", "third_party/dawn/tools/golang/linux-amd64/bin/go"),
                                 ("clang-format", "buildtools/linux64-format/clang-format")):
        binary = shutil.which(executable)
        if not binary:
            raise ValueError("missing Linux157 host prerequisite: " + executable)
        target = src / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(binary, target)
    if (src / ".chromix-domain-substitution-in-progress").exists():
        raise ValueError("interrupted domain substitution; preserve the failed snapshot")
    marker = src / ".chromix-domain-substituted"
    if not marker.exists():
        (src / ".chromix-domain-substitution-in-progress").write_text(source.CORE + "\n")
        subprocess.run([sys.executable, str(core / "utils/domain_substitution.py"), "apply", "-r",
                        str(core / "domain_regex.list"), "-f", str(core / "domain_substitution.list"), str(src)],
                       check=True, env=env)
        outputs = {}
        for name, expected in receipt["prepared_outputs"].items():
            outputs[name] = (source.digest(source.regular(src, name).read_bytes())
                             if expected is not None else None)
        receipt["substituted_outputs"] = outputs
        (src / source.RECEIPT).write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
        (src / ".chromix-domain-substitution-in-progress").replace(marker)
    source.verify_receipt(src, repo)
    diagnostic = work / "fingerprint-diagnostics"
    diagnostic.mkdir(exist_ok=True)
    report = diagnostic / ("source-" + str(time.time_ns()) + ".json")
    subprocess.run([sys.executable, str(repo / "tools/verify_patch_stack.py"), "--src", str(src),
                    "--repo", str(repo), "--core", str(core), "--platform-tooling", str(tooling),
                    "--platform", "linux", "--output", str(report)], check=True, env=env)
    shutil.copy2(report, diagnostic / "source-final.json")
    target_gn = work / "linux157-target.gn"
    target_gn.write_text(f'target_cpu = "{arch}"\nv8_target_cpu = "{arch}"\n'
                         'use_typescript_go = true\nuse_chromium_rust_toolchain = true\n'
                         'rust_prebuilt_stdlib = false\n')
    profile = os.environ.get("CHROMIX_BUILD_PROFILE", "release")
    if profile not in ("release", "fast"):
        raise ValueError("invalid Linux157 build profile")
    subprocess.run([sys.executable, str(repo / "tools/merge_gn_args.py"), "--build-profile", profile,
                    str(out / "args.gn"), str(core / "flags.gn"), str(tooling / "flags.linux.gn"),
                    str(repo / "build/args.gn"), str(target_gn)], check=True, env=env)
    subprocess.run([sys.executable, str(repo / "tools/bootstrap_gn.py"), "--src", str(src), "--out", str(out)],
                   check=True, env=env)
    subprocess.run([str(out / "gn"), "gen", str(out), "--fail-on-unused-args"], cwd=src, check=True, env=env)
    ninja = shutil.which("ninja")
    if not ninja:
        raise ValueError("ninja is required")
    jobs = os.environ.get("CHROMIX_JOBS", str(os.cpu_count() or 2))
    if not jobs.isdecimal() or int(jobs) <= 0:
        raise ValueError("invalid Linux157 job count")
    subprocess.run([ninja, "-C", str(out), "-j", jobs, "chrome", "chrome_crashpad_handler", "chrome_sandbox"],
                   check=True, env=env)
    if arch == "x64":
        version = subprocess.check_output([str(out / "chrome"), "--version"], env=env, text=True, timeout=30)
        if source.VERSION not in version:
            raise ValueError("built Chromium version mismatch")
    # Cross targets never claim runtime success; the native CI gate owns that evidence.
    (work / "linux157-compiled.json").write_text(json.dumps({
        "recipe": receipt["recipe"], "target_arch": arch, "compiled": True,
        "runtime_verified": False, "required_runtime_host": arch}, indent=2) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=source.ROOT)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    args = parser.parse_args(argv)
    work = args.workdir.resolve()
    try:
        build(work, args.arch, args.repo)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        work.mkdir(parents=True, exist_ok=True)
        retry_valid = False
        try:
            receipt = source.verify_receipt(work / "src", args.repo)
            retry_valid = (receipt.get("target_arch") == args.arch
                           and (work / "src/out/Chromix/build.ninja").is_file()
                           and not (work / "src/.chromix-domain-substitution-in-progress").exists())
        except (OSError, ValueError):
            pass
        (work / "linux157-failed.json").write_text(json.dumps({
            "schema_version": 1, "target_arch": args.arch, "error": str(error),
            "snapshot_valid_for_retry": retry_valid, "preserve_worktree": True,
            "runtime_verified": False}, indent=2) + "\n")
        print("Linux157 build failed; preserve worktree snapshot: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
