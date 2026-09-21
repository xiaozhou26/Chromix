#!/usr/bin/env python3
"""Import verified toolchains and stage compiler-checked object candidates.

Canonical source and Ninja build graphs remain authoritative. Optional cache
misses return success and leave compilation to the normal build commands.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import tempfile
from pathlib import Path

try:
    from .fetch_upstream_cache import CacheMiss, load_manifest
    from .platform_pins import PinError, load_pins, load_shared_pins
    from .upstream_script_identity import ScriptIdentity
except ImportError:
    from fetch_upstream_cache import CacheMiss, load_manifest
    from platform_pins import PinError, load_pins, load_shared_pins
    from upstream_script_identity import ScriptIdentity

REPO = Path(__file__).resolve().parents[1]
CLANG = Path("third_party/llvm-build/Release+Asserts")
RUST = Path("third_party/rust-toolchain")
REPORT = "upstream-cache-import.json"
MARKER = ".chromix-upstream-toolchain.json"
PLATFORM_KEYS = {"linux": "Linux", "macos": "MacOS", "windows": "Windows"}
TRIPLES = {
    ("linux", "x64"): "x86_64-unknown-linux-gnu",
    ("linux", "arm64"): "aarch64-unknown-linux-gnu",
    ("macos", "x64"): "x86_64-apple-darwin",
    ("macos", "arm64"): "aarch64-apple-darwin",
    ("windows", "x64"): "x86_64-pc-windows-msvc",
    ("windows", "arm64"): "aarch64-pc-windows-msvc",
}


class Miss(ValueError):
    """The optional donor cannot safely accelerate this build."""


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Miss(f"expected JSON object: {path.name}")
    return value


def write_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def contained(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def safe_path(root: Path, relative: Path) -> Path:
    path = root / relative
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise Miss(f"symlinked directory or input: {relative}")
    if not contained(path.resolve(), root.resolve()):
        raise Miss(f"path escapes root: {relative}")
    return path


def inventory(root: Path, ignore_pycache: bool = False) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise Miss(f"missing or symlinked directory: {root}")
    entries = {}
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        if ignore_pycache:
            dirs[:] = [name for name in dirs if name != "__pycache__"]
            files = [name for name in files if not name.endswith((".pyc", ".pyo"))]
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                resolved = path.resolve(strict=True)
                if os.path.isabs(target) or not contained(resolved, root.resolve()):
                    raise Miss(f"external toolchain symlink: {relative}")
                if not (resolved.is_file() or resolved.is_dir()):
                    raise Miss(f"unsupported toolchain symlink target: {relative}")
                entries[relative] = ["symlink", mode, target]
            elif stat.S_ISREG(info.st_mode):
                entries[relative] = ["file", mode, info.st_size, digest_file(path)]
            elif stat.S_ISDIR(info.st_mode):
                entries[relative] = ["directory", mode]
            else:
                raise Miss(f"unsupported special file: {relative}")
    return entries


def tree_digest(entries: dict) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def literal_constants(path: Path, names: tuple[str, ...]) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in names:
                if target.id in values:
                    raise Miss(f"duplicate update constant: {target.id}")
                try:
                    values[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    raise Miss(f"nonliteral update constant: {target.id}") from None
    if set(values) != set(names):
        raise Miss(f"missing static update constants in {path.name}")
    return values


def expected_versions(src: Path) -> dict:
    clang = literal_constants(src / "tools/clang/scripts/update.py", (
        "CLANG_REVISION", "CLANG_SUB_REVISION", "RELEASE_VERSION"))
    rust = literal_constants(src / "tools/rust/update_rust.py", (
        "RUST_REVISION", "RUST_SUB_REVISION"))
    for key, value in {**clang, **rust}.items():
        if key.endswith("SUB_REVISION"):
            if type(value) is not int or value < 0:
                raise Miss(f"invalid update constant: {key}")
        elif not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise Miss(f"invalid update constant: {key}")
    return {
        "clang": f"{clang['CLANG_REVISION']}-{clang['CLANG_SUB_REVISION']}",
        "clang_release": clang["RELEASE_VERSION"],
        "rust": f"{rust['RUST_REVISION']}-{rust['RUST_SUB_REVISION']}-{clang['CLANG_REVISION']}",
    }


def repository_identity(repo: Path, platform: str, arch: str) -> tuple[dict, dict]:
    try:
        pins = load_pins(repo, platform)
        global_pins = load_shared_pins(repo)
    except PinError as exc:
        raise Miss(str(exc)) from exc
    manifest = read_json(repo / "build/upstream-cache.json")
    if manifest.get("schema_version") != 1:
        raise Miss("unsupported pinned cache manifest schema")
    source = manifest["sources"][platform]
    expected = {
        "chromium_version": pins["ChromiumVersion"],
        "ungoogled_commit": pins["UngoogledCommit"],
        "head_sha": pins[f"Ungoogled{PLATFORM_KEYS[platform]}Commit"],
    }
    if any(manifest.get(key) != global_pins[pin] for key, pin in (
            ("chromium_version", "ChromiumVersion"), ("ungoogled_commit", "UngoogledCommit"))):
        raise Miss("cache manifest global identity does not match repository pins")
    if any(source.get(key, manifest.get(key)) != expected[key]
           for key in ("chromium_version", "ungoogled_commit")):
        raise Miss("cache manifest does not match repository pins")
    if source.get("head_sha") != expected["head_sha"]:
        raise Miss("cache manifest platform commit does not match repository pins")
    try:
        pin, _ = load_manifest(platform, arch, root=repo)
    except CacheMiss as exc:
        raise Miss(f"invalid pinned cache manifest: {exc}") from exc
    artifact = pin["artifact"]
    identity = dict(expected, platform=platform, arch=arch)
    for key in ("repository", "repository_id", "head_branch", "event", "workflow_path", "run_id"):
        identity[key] = pin[key]
    for key in ("id", "name", "digest", "size_in_bytes"):
        identity[f"artifact_{key}"] = artifact[key]
    return identity, source


def validate_source(src: Path, identity: dict) -> None:
    version_file = safe_path(src, Path("chrome/VERSION"))
    parts = dict(re.findall(r"^(MAJOR|MINOR|BUILD|PATCH)=(\d+)$", version_file.read_text(), re.M))
    version = ".".join(parts.get(key, "?") for key in ("MAJOR", "MINOR", "BUILD", "PATCH"))
    if version != identity["chromium_version"]:
        raise Miss("canonical final chrome/VERSION does not match repository")
    ready = safe_path(src, Path(".chromix-source-ready")).read_text().strip().split("|")
    expected = [identity[key] for key in ("chromium_version", "ungoogled_commit", "head_sha")]
    if identity["platform"] != "windows":
        expected = [identity["platform"], identity["arch"]] + expected
    if ready[:-1] != expected or not re.fullmatch(r"[0-9a-f]{64}", ready[-1]):
        raise Miss("canonical source preparation marker does not match pinned identity")


def donor_source(cache: Path, result: dict, identity: dict, repo: Path) -> Path:
    if result.get("status") != "hit" or result.get("owner") != "chromix-upstream-cache-v1":
        raise Miss("downloader did not provide an owned verified hit")
    provenance = result.get("manifest")
    if not isinstance(provenance, dict):
        raise Miss("missing downloader manifest identity")
    expected = {key: identity[key] for key in (
        "chromium_version", "repository", "head_sha", "run_id", "artifact_id", "artifact_digest")}
    expected.update(schema_version=1, target=f"{identity['platform']}-{identity['arch']}",
                    sha256=digest_file(repo / "build/upstream-cache.json"))
    for key, value in expected.items():
        if provenance.get(key) != value:
            raise Miss(f"downloader pinned identity mismatch: {key}")
    if any(result.get(key) != identity[key] for key in ("platform", "arch")):
        raise Miss("downloader platform/architecture mismatch")
    if result.get("destination") != str(cache):
        raise Miss("downloader destination mismatch")
    value = result.get("source")
    if not isinstance(value, str) or not value:
        raise Miss("downloader source path is missing")
    path = Path(value)
    if not path.is_absolute():
        path = cache / path
    if not contained(path.absolute(), cache) or path.absolute() == cache:
        raise Miss("donor source is not inside cache directory")
    relative = path.absolute().relative_to(cache)
    if relative.as_posix() not in {"tree/src", "tree/build/src"}:
        raise Miss("unsupported extracted donor source layout")
    source = safe_path(cache, relative).resolve(strict=True)
    if source == cache or not source.is_dir():
        raise Miss("invalid donor source directory")
    parts = dict(re.findall(r"^(MAJOR|MINOR|BUILD|PATCH)=(\d+)$",
                           safe_path(source, Path("chrome/VERSION")).read_text(), re.M))
    if ".".join(parts.get(key, "?") for key in ("MAJOR", "MINOR", "BUILD", "PATCH")) != identity["chromium_version"]:
        raise Miss("donor chrome/VERSION does not match repository")
    return source


def require_file(path: Path, executable: bool = False, platform: str = "linux") -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise Miss(f"missing or empty required toolchain file: {path}")
    if executable and platform != "windows" and not path.stat().st_mode & 0o111:
        raise Miss(f"required binary is not executable: {path}")


def require_binary(path: Path, platform: str, arch: str) -> None:
    require_file(path, executable=True, platform=platform)
    with path.open("rb") as binary:
        header = binary.read(64)
        valid = False
        if platform == "linux" and header[:6] == b"\x7fELF\x02\x01" and len(header) >= 20:
            valid = struct.unpack_from("<H", header, 18)[0] == {"x64": 62, "arm64": 183}[arch]
        elif platform == "macos" and header[:4] == b"\xcf\xfa\xed\xfe" and len(header) >= 8:
            valid = struct.unpack_from("<I", header, 4)[0] == {"x64": 0x1000007, "arm64": 0x100000C}[arch]
        elif platform == "windows" and header[:2] == b"MZ" and len(header) == 64:
            binary.seek(struct.unpack_from("<I", header, 60)[0])
            pe = binary.read(6)
            valid = pe[:4] == b"PE\0\0" and len(pe) == 6 and struct.unpack_from("<H", pe, 4)[0] == {"x64": 0x8664, "arm64": 0xAA64}[arch]
        if not valid:
            raise Miss(f"unsupported binary format or host architecture: {path.name}")


def require_glob(root: Path, pattern: str) -> None:
    paths = [path for path in root.glob(pattern) if path.is_file()]
    if not paths:
        raise Miss(f"missing required toolchain libraries: {pattern}")
    for path in paths:
        require_file(path)


def validate_rust_libraries(src: Path, platform: str, arch: str) -> dict:
    arches = sorted({arch, "x64"}) if platform == "windows" else [arch]
    libraries = {}
    for cpu in arches:
        relative = RUST / "lib/rustlib" / TRIPLES[platform, cpu] / "lib"
        root = safe_path(src, relative)
        for pattern in ("libstd-*.rlib", "libcore-*.rlib", "liballoc-*.rlib", "libcompiler_builtins-*.rlib"):
            require_glob(root, pattern)
            for path in sorted(root.glob(pattern)):
                if path.is_file():
                    safe_path(src, path.relative_to(src))
                    libraries[path.relative_to(src).as_posix()] = digest_file(path)
    return libraries


def validate_toolchains(src: Path, versions: dict, platform: str, arch: str,
                        bindgen: bool = True) -> dict:
    host_arch = "x64" if platform == "windows" else arch
    clang = safe_path(src, CLANG)
    rust = safe_path(src, RUST)
    trees = {"clang": inventory(clang), "rust": inventory(rust)}
    stamp = (clang / "cr_build_revision").read_text().strip()
    if stamp.split(",")[0] != versions["clang"]:
        raise Miss("clang cr_build_revision does not match static source constants")
    rust_stamp = (rust / "VERSION").read_text().splitlines()[0]
    match = re.fullmatch(r"rustc [0-9.]+ [0-9a-f]+ \((.+?) chromium\)", rust_stamp)
    if not match or match.group(1) != versions["rust"]:
        raise Miss("rust VERSION does not match static source constants")
    suffix = ".exe" if platform == "windows" else ""
    binaries = {
        "linux": ["clang", "clang++", "llvm-ar", "llvm-nm", "llvm-readobj", "llvm-objcopy", "ld.lld"],
        "macos": ["clang", "clang++", "llvm-ar", "llvm-readobj", "llvm-objcopy", "ld64.lld"],
        "windows": ["clang-cl", "lld-link", "llvm-ml"],
    }[platform]
    for name in binaries:
        require_binary(clang / "bin" / (name + suffix), platform, host_arch)
    resource = clang / "lib/clang" / versions["clang_release"]
    require_file(resource / "include/stddef.h")
    require_file(resource / "include/stdarg.h")
    runtime_arch = {"x64": "x86_64", "arm64": "aarch64"}[arch]
    if platform == "linux":
        runtimes = [resource / f"lib/{TRIPLES[platform, arch]}/libclang_rt.builtins.a",
                    resource / f"lib/linux/libclang_rt.builtins-{runtime_arch}.a"]
        if not any(path.is_file() and path.stat().st_size for path in runtimes):
            raise Miss("missing required toolchain libraries: target builtins runtime")
    else:
        require_glob(resource, {"macos": "lib/darwin/libclang_rt.osx.a",
                                "windows": f"lib/windows/clang_rt.builtins-{runtime_arch}.lib"}[platform])
    for name in ["rustc", "cargo", "rustfmt"] + (["bindgen"] if bindgen else []):
        require_binary(rust / "bin" / (name + suffix), platform, host_arch)
    if (platform, arch) == ("windows", "arm64"):
        validate_rust_libraries(src, platform, arch)
    else:
        rustlib = rust / "lib/rustlib" / TRIPLES[platform, arch] / "lib"
        for pattern in ("libstd-*.rlib", "libcore-*.rlib", "liballoc-*.rlib", "libcompiler_builtins-*.rlib"):
            require_glob(rustlib, pattern)
    require_glob(rust, "bin/rustc_driver*.dll" if platform == "windows" else "lib/librustc_driver*.*")
    if bindgen:
        require_glob(rust, {"linux": "lib/libclang.so*", "macos": "lib/libclang.dylib",
                            "windows": "bin/libclang.dll"}[platform])
    return trees


def script_identity(src: Path) -> dict:
    for relative in ("tools/clang/scripts/build.py", "tools/rust/build_rust.py", "tools/rust/build_bindgen.py"):
        require_file(safe_path(src, Path(relative)))
    return {str(relative): tree_digest(inventory(safe_path(src, relative), ignore_pycache=True))
            for relative in (Path("tools/clang"), Path("tools/rust"))}


def compare_scripts(src: Path, donor: Path, platform: str = "linux") -> dict:
    canonical, cached = script_identity(src), script_identity(donor)
    normalizer = None
    for relative in canonical:
        if canonical[relative] == cached[relative]:
            continue
        current = inventory(src / relative, ignore_pycache=True)
        previous = inventory(donor / relative, ignore_pycache=True)
        if current.keys() != previous.keys():
            raise Miss(f"donor and canonical {relative} scripts differ")
        for name, entry in current.items():
            other = previous[name]
            if entry == other:
                continue
            if entry[:2] != other[:2] or entry[0] != "file":
                raise Miss(f"donor script type or mode differs: {relative}/{name}")
            path = f"{relative}/{name}"
            if normalizer is None:
                normalizer = ScriptIdentity(src.parent / "tooling/ungoogled-chromium", platform)
            if not normalizer.matches(path, (src / path).read_bytes(), (donor / path).read_bytes()):
                raise Miss(f"donor and canonical {relative} scripts differ: {name}")
    return canonical


def bindgen_addition(relative: str, platform: str) -> bool:
    return relative in ("bin/bindgen", "bin/bindgen.exe") or bool(re.fullmatch(
        {"linux": r"lib/libclang\.so(?:\.[0-9]+)*", "macos": r"lib/libclang\.dylib",
         "windows": r"bin/libclang\.dll"}[platform], relative))


def compatible_existing(canonical: dict, donor: dict, platform: str) -> None:
    if canonical["clang"] != donor["clang"]:
        raise Miss("canonical compiler content differs; bindgen cannot be reused")
    for relative in set(canonical["rust"]) | set(donor["rust"]):
        current = canonical["rust"].get(relative)
        cached = donor["rust"].get(relative)
        if current != cached and not (current is None and cached is not None and bindgen_addition(relative, platform)):
            raise Miss(f"canonical Rust content differs: {relative}")


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def install_transaction(src: Path, donor: Path, trees: dict, platform: str,
                        marker: dict) -> dict:
    if list(src.glob(".chromix-upstream-transaction-*")):
        raise Miss("interrupted import transaction exists; use a clean work directory")
    transaction = Path(tempfile.mkdtemp(prefix=".chromix-upstream-transaction-", dir=src))
    installed = []
    backups = []
    complete = False
    counts = {"files_copied": 0, "bytes_copied": 0, "directories_reused": 0, "objects_copied": 0}
    selections = [("clang", CLANG), ("rust", RUST)] if platform == "linux" else [("rust", RUST)]
    try:
        for name, relative in selections:
            staged = transaction / name
            if platform == "linux":
                shutil.copytree(donor / relative, staged, symlinks=True)
            else:
                shutil.copytree(src / relative, staged, symlinks=True)
                for filename in trees[name]:
                    if bindgen_addition(filename, platform) and not (staged / filename).exists():
                        shutil.copy2(donor / relative / filename, staged / filename, follow_symlinks=False)
            if inventory(staged) != trees[name]:
                raise Miss(f"{name} changed while staging the import")
        for name, relative in selections:
            destination = safe_path(src, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                backup = transaction / (name + "-previous")
                os.replace(destination, backup)
                backups.append((backup, destination))
            os.replace(transaction / name, destination)
            installed.append(destination)
            entries = trees[name].values()
            counts["files_copied"] += sum(entry[0] in ("file", "symlink") for entry in entries)
            counts["bytes_copied"] += sum(entry[2] for entry in trees[name].values() if entry[0] == "file")
            counts["directories_reused"] += 1
        if platform == "linux":
            write_json(src / MARKER, marker)
        complete = True
    finally:
        if not complete:
            for path in reversed(installed):
                remove_path(path)
            for backup, destination in reversed(backups):
                os.replace(backup, destination)
        shutil.rmtree(transaction)
    return counts


def gn_assignments(path: Path) -> dict:
    assignments = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_]\w*)\s*=\s*(.+)", line)
        if not match or match.group(1) in assignments:
            raise Miss("unsupported or duplicate GN assignment")
        key, text = match.groups()
        if "$" in text:
            raise Miss(f"unevaluated GN expression: {key}")
        try:
            value, end = json.JSONDecoder().raw_decode(text)
        except ValueError:
            raise Miss(f"nonliteral GN assignment: {key}") from None
        if text[end:].strip() and not text[end:].lstrip().startswith("#"):
            raise Miss(f"unsupported GN expression: {key}")
        def literal(item):
            return type(item) in (str, int, bool) or (isinstance(item, list) and all(literal(x) for x in item))
        if not literal(value):
            raise Miss(f"unsupported GN value: {key}")
        # Preserve value types (Python otherwise equates True with 1).
        assignments[key] = json.dumps(value, separators=(",", ":"))
    return assignments


def preserve_external_tool_lookups(cache: Path, donor: Path, result: dict) -> None:
    omitted = result.get("external_symlink_paths", [])
    if not result.get("skipped_external_symlinks", 0):
        return
    if not isinstance(omitted, list) or len(omitted) != result["skipped_external_symlinks"]:
        raise Miss("incomplete donor source: omitted links were not recorded")
    tools = {"third_party/node/linux/node-linux-x64/bin/node",
             "third_party/node/linux/node-linux-arm64/bin/node",
             "third_party/gperf/cipd/bin/gperf",
             "third_party/dawn/tools/golang/linux-amd64/bin/go",
             "third_party/dawn/tools/golang/linux-arm64/bin/go",
             "buildtools/linux64-format/clang-format"}
    paths = []
    for name in omitted:
        path = safe_path(cache, Path(name))
        if not contained(path, donor) or path.relative_to(donor).as_posix() not in tools:
            raise Miss("incomplete donor source: unknown external symlink")
        if path.exists() or path.is_symlink():
            raise Miss("omitted external link unexpectedly exists")
        paths.append(path)
    # Preserve __has_include existence; including an external executable must miss.
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#error unverified external build tool\n", encoding="utf-8")


def validate_gn_target(values: dict, platform: str, arch: str) -> None:
    for name in ("target_cpu", "v8_target_cpu"):
        if (name == "target_cpu" or name in values) and values.get(name) != json.dumps(arch):
            raise Miss(f"GN {name} does not match requested architecture")
    if platform == "windows":
        for name, expected in (("host_cpu", "x64"), ("target_os", "win")):
            if name in values and values[name] != json.dumps(expected):
                raise Miss(f"GN {name} does not match Windows x64-host build")


def check_objects(src: Path, donor: Path, arch: str, platform: str) -> None:
    if not (src / ".chromix-domain-substituted").is_file():
        raise Miss("final canonical domain substitution is not recorded")
    canonical = gn_assignments(safe_path(src, Path("out/Chromix/args.gn")))
    cached = gn_assignments(safe_path(donor, Path("out/Default/args.gn")))
    for values in (canonical, cached):
        if values.get("target_cpu") != json.dumps(arch):
            raise Miss("mandatory GN target_cpu does not match requested architecture")
        if "v8_target_cpu" in values and values["v8_target_cpu"] != json.dumps(arch):
            raise Miss("GN v8_target_cpu does not match requested architecture")
        validate_gn_target(values, platform, arch)
    differences = sorted(key for key in canonical.keys() | cached.keys() if canonical.get(key) != cached.get(key))
    if differences:
        raise Miss("GN assignments differ: " + ", ".join(differences[:12]))
    for relative in (CLANG, RUST):
        if inventory(safe_path(src, relative)) != inventory(safe_path(donor, relative)):
            raise Miss(f"object toolchain content differs: {relative}")
    if platform != "linux":
        raise Miss("external SDK identity and environment are unavailable; objects are not relocatable")
    sysroots = sorted((src / "build/linux").glob("*sysroot"))
    if not sysroots:
        raise Miss("canonical SDK/sysroot content identity is unavailable")
    for root in sysroots:
        relative = root.relative_to(src)
        if inventory(safe_path(src, relative)) != inventory(safe_path(donor, relative)):
            raise Miss("SDK/sysroot content identity differs")
    raise Miss("out/Default to out/Chromix relocation and external dependency/environment identity are unproven; objects not imported")


def resume_marker(work: Path, src: Path) -> bool:
    return any(path.exists() for path in (
        work / ".chromix-resumed", work / ".chromix-resume", work / ".chromix-upstream-cache-resume",
        src / ".chromix-resumed", src / ".chromix-resume",
        src / "out/Chromix/.ninja_log", src / "out/Chromix/.ninja_deps"))


def cleanup_cache(cache: Path) -> int:
    result = read_json(cache / "result.json")
    if result.get("owner") != "chromix-upstream-cache-v1" or result.get("destination") != str(cache):
        raise Miss("cleanup skipped: cache ownership not verified")
    allowed = {"result.json", "tree", ".download.zip", ".inner", ".result.tmp"}
    if any(path.name not in allowed for path in cache.iterdir()):
        raise Miss("cleanup skipped: cache is locked or contains unexpected files")
    lock = cache / ".lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        count = 0
        for name in ("tree", ".download.zip", ".inner", ".result.tmp"):
            path = cache / name
            if path.exists() or path.is_symlink():
                remove_path(path)
                count += 1
        result.update(status="consumed", download_status=result.get("status"), source=None)
        write_json(cache / "result.json", result)
        return count
    finally:
        lock.unlink()


def run_import(phase: str, platform: str, arch: str, workdir: Path,
               cache_dir: Path, repo: Path = REPO) -> dict:
    work = workdir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    src = work / "src"
    cache = cache_dir.resolve()
    report_path = work / REPORT
    try:
        report = read_json(report_path) if report_path.is_file() else {}
    except (OSError, ValueError):
        report = {}
    if report.get("platform", platform) != platform or report.get("arch", arch) != arch:
        report = {}
    report.update(schema_version=1, platform=platform, arch=arch)
    if not isinstance(report.get("phases"), dict):
        report["phases"] = {}
    phases = report["phases"]
    entry = {"status": "miss", "reasons": [], "counts": {
        "files_copied": 0, "bytes_copied": 0, "directories_reused": 0, "objects_copied": 0}}
    can_cleanup = False
    try:
        if resume_marker(work, src):
            raise Miss("resume marker exists; donor artifacts were not inspected or consumed")
        if src.is_symlink():
            raise Miss("canonical source root is symlinked")
        if cache_dir.is_symlink() or contained(work, cache) or contained(cache, src.resolve()) or contained(repo.resolve(), cache):
            raise Miss("unsafe overlapping or symlinked cache directory")
        if (cache / ".lock").exists():
            raise Miss("downloader is still writing the cache")
        can_cleanup = cache.is_dir()
        identity, _ = repository_identity(repo, platform, arch)
        validate_source(src, identity)
        if (src / ".chromix-domain-substitution-in-progress").exists():
            raise Miss("canonical domain substitution is incomplete")
        if phase == "toolchain" and (src / ".chromix-toolchain-ready").exists():
            raise Miss("canonical toolchain is already ready; donor not consumed")
        if phase == "toolchain" and (src / MARKER).exists():
            previous = read_json(src / MARKER)
            if previous.get("identity") != identity:
                raise Miss("existing upstream toolchain marker has a different identity")
            if previous.get("scripts") != script_identity(src):
                raise Miss("canonical toolchain scripts changed after import")
            trees = validate_toolchains(src, expected_versions(src), platform, arch)
            if previous.get("content") != {name: tree_digest(tree) for name, tree in trees.items()}:
                raise Miss("installed upstream toolchain content changed")
            entry.update(status="hit", reasons=["previous validated toolchain import is intact"], reused=previous["reused"])
        else:
            result = read_json(cache / "result.json")
            donor = donor_source(cache, result, identity, repo)
            if phase == "objects":
                if platform == "linux":
                    if not (src / ".chromix-domain-substituted").is_file():
                        raise Miss("final canonical domain substitution is not recorded")
                    preserve_external_tool_lookups(cache, donor, result)
                    try:
                        from .upstream_object_cache import prepare
                    except ImportError:
                        from upstream_object_cache import prepare
                    entry = prepare(src, donor, platform, arch, work,
                                    allow_truncated_mtimes=result.get("extraction_scope") == "source-and-objects")
                    entry["counts"].setdefault("objects_copied", 0)
                else:
                    check_objects(src, donor, arch, platform)
            else:
                if (src / ".chromix-domain-substituted").exists():
                    raise Miss("toolchains must be imported before canonical domain substitution")
                scripts = compare_scripts(src, donor, platform)
                versions = expected_versions(src)
                trees = validate_toolchains(donor, versions, platform, arch)
                if platform != "linux":
                    canonical = validate_toolchains(src, versions, platform, arch, bindgen=False)
                    compatible_existing(canonical, trees, platform)
                marker = {"schema_version": 1, "identity": identity, "versions": versions, "scripts": scripts,
                          "content": {name: tree_digest(tree) for name, tree in trees.items()},
                          "reused": {"clang": platform == "linux", "rust": platform == "linux", "bindgen": True},
                          "sysroot_reused": False}
                counts = install_transaction(src, donor, trees, platform, marker)
                entry.update(status="hit", reasons=["validated downloaded toolchains" if platform == "linux" else
                                                    "bindgen reused with byte-identical canonical compiler and Rust"],
                             counts=counts, reused=marker["reused"], sysroot_reused=False)
    except (OSError, ValueError, KeyError, TypeError, IndexError, SyntaxError, RuntimeError) as error:
        entry["reasons"] = [str(error)]
    finally:
        if phase == "toolchain" and entry["status"] == "miss" and not src.is_symlink():
            # Rejected receipts cannot authorize later compiler reuse.
            (src / MARKER).unlink(missing_ok=True)
        if phase == "objects" and can_cleanup and entry["status"] != "ready":
            try:
                entry["counts"]["cache_entries_removed"] = cleanup_cache(cache)
            except (OSError, ValueError) as error:
                entry["reasons"].append(f"donor cleanup failed: {error}")
        phases[phase] = entry
        write_json(report_path, report)
    return entry


def finalize_objects(workdir: Path, cache_dir: Path) -> dict:
    """Record actual compiler receipts and release first-stage donor storage."""
    work, cache = workdir.resolve(), cache_dir.resolve()
    objects = work / ".upstream-objects"
    report = {"hits": 0, "misses": 0, "bytes_reused": 0, "reasons": {}, "cleanup_errors": []}
    try:
        if objects.is_symlink():
            raise Miss("symlinked object cache")
        for path in (objects / "receipts").glob("*.json"):
            receipt = read_json(path)
            if receipt.get("status") == "hit" and receipt.get("returncode") == 0:
                report["hits"] += 1
                report["bytes_reused"] += receipt["bytes"]
            else:
                report["misses"] += 1
                reason = receipt.get("reason", "unknown")
                report["reasons"][reason] = report["reasons"].get(reason, 0) + 1
        if objects.is_dir():
            if any(path.name not in ("manifest.json", "receipts") and
                   not path.name.startswith(("generation-", "preprocess-", ".write-"))
                   for path in objects.iterdir()):
                raise Miss("unexpected object-cache contents; cleanup skipped")
            shutil.rmtree(objects)
    except (OSError, ValueError, KeyError, TypeError) as error:
        report["cleanup_errors"].append(str(error))
    if cache.is_dir() and not cache_dir.is_symlink() and not contained(work, cache) and not contained(cache, work / "src"):
        try:
            cleanup_cache(cache)
        except (OSError, ValueError) as error:
            report["cleanup_errors"].append(str(error))
    write_json(work / "upstream-object-cache.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("toolchain", "objects", "finalize"), required=True)
    parser.add_argument("--platform", choices=("linux", "macos", "windows"), required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "finalize":
        report = finalize_objects(args.workdir, args.cache_dir)
        print(f"upstream object cache: {report['hits']} hits, {report['misses']} misses, {report['bytes_reused']} bytes reused")
        return 0
    entry = run_import(args.phase, args.platform, args.arch, args.workdir, args.cache_dir)
    print(f"upstream cache {args.phase}: {entry['status']}: {'; '.join(entry['reasons'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
