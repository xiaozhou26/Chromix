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
import tempfile

try:
    from . import linux157_source as source
    from . import linux157_toolchain as toolchain
    from . import linux157_gn as host_gn
    from . import linux157_checkpoint as checkpoint
except ImportError:
    import linux157_source as source
    import linux157_toolchain as toolchain
    import linux157_gn as host_gn
    import linux157_checkpoint as checkpoint


GO_VERSION = "go1.27.1"
GO_TARGET = "third_party/dawn/tools/golang/linux-amd64/bin/go"


def prepare_host_go(src: Path, env: dict) -> dict:
    """Bind the copied generator to this stage's verified host Go installation."""
    if toolchain.platform.system() != "Linux" or toolchain.platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("Linux157 Go requires a Linux x64 host")
    selected = shutil.which("go", path=env.get("PATH", os.defpath))
    if not selected:
        raise ValueError("missing Linux157 host prerequisite: go")
    binary = Path(selected).resolve(strict=True)

    def native_elf(path):
        with path.open("rb") as stream:
            header = stream.read(20)
        if (header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\x3e\x00"
                or not os.access(path, os.X_OK)):
            raise ValueError("Go tool is not executable Linux amd64 ELF: " + str(path))

    native_elf(binary)
    discovery = dict(env)
    discovery.pop("GOROOT", None)
    discovery.update(GOENV="off", GOTOOLCHAIN="local", GOOS="linux", GOARCH="amd64",
                     GOFLAGS="", GOWORK="off")
    fields = ("GOROOT", "GOVERSION", "GOHOSTOS", "GOHOSTARCH", "GOTOOLDIR")
    info = json.loads(subprocess.check_output([str(binary), "env", "-json", *fields],
                                             env=discovery, text=True, timeout=30))
    if (info.get("GOVERSION"), info.get("GOHOSTOS"), info.get("GOHOSTARCH")) != (GO_VERSION, "linux", "amd64"):
        raise ValueError("Linux157 requires Go1.27.1 linux/amd64")
    root = Path(info["GOROOT"])
    if not root.is_absolute():
        raise ValueError("Go GOROOT must be absolute")
    root = root.resolve(strict=True)
    tool_dir = root / "pkg/tool/linux_amd64"
    if Path(info["GOTOOLDIR"]).resolve(strict=True) != tool_dir.resolve(strict=True):
        raise ValueError("Go tool directory does not belong to GOROOT")
    if (source.regular(root, "VERSION").read_text().splitlines()[0] != GO_VERSION
            or not (root / "src/runtime").is_dir()
            or toolchain.file_hash(source.regular(root, "bin/go")) != toolchain.file_hash(binary)):
        raise ValueError("Go binary/GOROOT version mismatch")
    compiler = source.regular(root, "pkg/tool/linux_amd64/compile")
    native_elf(compiler)
    bound = dict(discovery, GOROOT=str(root))
    compiler_version = subprocess.check_output([str(compiler), "-V=full"], env=bound,
                                               text=True, timeout=30).strip()
    if compiler_version != "compile version " + GO_VERSION:
        raise ValueError("Go compiler/GOROOT version mismatch")
    target = src
    for part in GO_TARGET.split("/"):
        target = target / part
        if target.is_symlink():
            raise ValueError("linked checkpoint Go path")
    target.parent.mkdir(parents=True, exist_ok=True)
    expected_hash = toolchain.file_hash(binary)
    if target.exists():
        if toolchain.file_hash(source.regular(src, GO_TARGET)) != expected_hash:
            raise ValueError("checkpoint Go binary differs from selected host Go")
    else:
        shutil.copy2(binary, target)
    native_elf(target)
    copied = json.loads(subprocess.check_output([str(target), "env", "-json", *fields],
                                               env=bound, text=True, timeout=30))
    if copied != {**info, "GOROOT": str(root)}:
        raise ValueError("copied Go does not resolve the selected GOROOT")
    with tempfile.TemporaryDirectory(prefix="linux157-go-probe-", dir=src.parent) as temporary:
        probe = Path(temporary)
        (probe / "main.go").write_text(
            'package main\nimport ("fmt"; "runtime"; "crypto/sha256")\n'
            'func main() { fmt.Printf("%s %s/%s %x\\n", runtime.Version(), runtime.GOOS, '
            'runtime.GOARCH, sha256.Sum256([]byte("chromix-go"))) }\n')
        probe_env = dict(bound, GO111MODULE="off", CGO_ENABLED="0", GOCACHE=str(probe / "cache"))
        subprocess.run([str(target), "build", "-o", str(probe / "probe"), "main.go"],
                       cwd=probe, env=probe_env, check=True, timeout=180)
        output = subprocess.check_output([str(probe / "probe")], env=probe_env,
                                         text=True, timeout=30).strip()
        expected = GO_VERSION + " linux/amd64 " + source.digest(b"chromix-go")
        if output != expected:
            raise ValueError("copied Go standard-library probe identity mismatch")
    env.update({key: bound[key] for key in ("GOROOT", "GOENV", "GOTOOLCHAIN", "GOOS", "GOARCH", "GOFLAGS", "GOWORK")})
    return {"version": GO_VERSION, "host": "linux/amd64", "goroot": str(root),
            "selected": str(binary), "copied": GO_TARGET, "sha256": expected_hash,
            "compiler_sha256": toolchain.file_hash(compiler), "probe": output}


def build(work: Path, arch: str, repo: Path = source.ROOT) -> None:
    if toolchain.platform.system() != "Linux" or toolchain.platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("Linux157 fresh build requires a Linux x64 build host")
    if os.environ.get("CHROMIX_USE_UPSTREAM_CACHE", "0") != "0" or os.environ.get("CHROMIX_UPSTREAM_CACHE_DIR"):
        raise ValueError("Linux157 fresh builds cannot use a donor cache")
    if not (work / "src").is_dir():
        raise ValueError("Linux157 GN recovery requires the exact latest stage1 prepared checkpoint; fresh fallback forbidden")
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
    go_identity = prepare_host_go(src, env)
    diagnostic = work / "fingerprint-diagnostics"
    diagnostic.mkdir(exist_ok=True)
    (diagnostic / "linux157-host-go.json").write_text(json.dumps(go_identity, indent=2) + "\n")
    # These small build utilities remain explicit host prerequisites.
    for executable, relative in (("gperf", "third_party/gperf/cipd/bin/gperf"),
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
    checkpoint.prepare_args(work, arch, repo)
    gn_identity = host_gn.prepare(src, out, env=env)
    (diagnostic / "linux157-host-gn.json").write_text(json.dumps(gn_identity, indent=2) + "\n")
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
        checkpoint_error = None
        try:
            source.checkpoint(work, args.arch, args.repo)
            retry_valid = True
        except (OSError, ValueError, subprocess.SubprocessError) as failure:
            checkpoint_error = str(failure)
        (work / "linux157-failed.json").write_text(json.dumps({
            "schema_version": 1, "target_arch": args.arch, "error": str(error),
            "snapshot_valid_for_retry": retry_valid, "preserve_worktree": True,
            "checkpoint_error": checkpoint_error,
            "runtime_verified": False}, indent=2) + "\n")
        print("Linux157 build failed; preserve worktree snapshot: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
