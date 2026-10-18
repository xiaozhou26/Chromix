#!/usr/bin/env python3
"""Restore a pinned full upstream source tree without building or preparing it.

restore(workdir, platform, arch, cache_dir, repo=REPO) returns a hit/miss report.
verify_restored(...) returns the receipt or raises Miss; is_restored(...) returns
None only when the receipt is absent. Neither verification API writes markers.
A restored snapshot is not proof of compiler, SDK, or environment compatibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path, PureWindowsPath

try:
    from . import fetch_upstream_cache as fetcher
    from . import import_upstream_cache as importer
    from . import upstream_object_cache as objects
except ImportError:
    import fetch_upstream_cache as fetcher
    import import_upstream_cache as importer
    import upstream_object_cache as objects

REPO = Path(__file__).resolve().parents[1]
MARKER = ".chromix-upstream-restored.json"
REPORT = "upstream-cache-restore.json"
OWNER = "chromix-upstream-restore-v1"
Miss = importer.Miss
LocalError = fetcher.LocalError
TARGETS = {("linux", "x64"), ("linux", "arm64"), ("macos", "x64"),
           ("macos", "arm64"), ("windows", "x64"), ("windows", "arm64")}
NANOSECOND = 1_000_000_000
# Keep synchronized with importer.preserve_external_tool_lookups.
HOST_LINKS = {"third_party/node/linux/node-linux-x64/bin/node",
              "third_party/node/linux/node-linux-arm64/bin/node",
              "third_party/gperf/cipd/bin/gperf",
              "third_party/dawn/tools/golang/linux-amd64/bin/go",
              "third_party/dawn/tools/golang/linux-arm64/bin/go",
              "buildtools/linux64-format/clang-format"}
LINUX154_ESBUILD_LINK = "third_party/devtools-frontend/src/node_modules/esbuild"
LINUX154_ESBUILD_ALIAS = "third_party/devtools-frontend/src/node_modules/.bin/esbuild"
LINUX154_ESBUILD_IDENTITY = {
    "chromium_version": "154.0.8037.97",
    "ungoogled_commit": "37085e47cf580c815a30402917d350ce97399ded",
    "head_sha": "f1441a6efb4d79427d1a3180b1877bbbdb1af7aa",
    "platform": "linux", "repository": "ungoogled-software/ungoogled-chromium-portablelinux",
    "repository_id": 177191557, "head_branch": "154.0.8037.97-1", "event": "push",
    "workflow_path": ".github/workflows/build.yml", "run_id": 37075483703,
}
LINUX154_ESBUILD_ARTIFACTS = {
    "x64": (11271061055, "build-cache-x86_64", 4737557847,
            "sha256:ad79c83fe3fb790c452685d86f0e169e659e076caf4ab1592282993bf2155645"),
    "arm64": (11283212930, "build-cache-arm64", 5672718015,
              "sha256:4e4cdf8e1a4f4baeb54a6d62906fb1f02da6fe0136f016868f823f7f28bda21c"),
}
LINUX155_ESBUILD_IDENTITY = {
    "chromium_version": "155.0.8059.39",
    "ungoogled_commit": "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a",
    "head_sha": "025d99db1673a21992534551598b73ca79360762",
    "platform": "linux", "repository": "ungoogled-software/ungoogled-chromium-portablelinux",
    "repository_id": 177191557, "head_branch": "155.0.8059.39-1", "event": "push",
    "workflow_path": ".github/workflows/build.yml", "run_id": 37906884923,
}
LINUX155_ESBUILD_ARTIFACTS = {
    "x64": (11652570743, "build-cache-x86_64", 4733942111,
            "sha256:34182b5fdbec949fbe29d320a7d7200eb9cd4b284c18b8daa0c516ab4816fa1a"),
    "arm64": (11656090008, "build-cache-arm64", 5677163828,
              "sha256:a4c0bfd8020cd34f27a652ae449f9e8547a468b8b813a11a64acf8a0891db447"),
}
MAC154_ESBUILD_LINK = "third_party/devtools-frontend/src/node_modules/esbuild"
MAC154_ESBUILD_ALIAS = "third_party/devtools-frontend/src/node_modules/.bin/esbuild"
MAC154_ESBUILD_IDENTITY = {
    "chromium_version": "154.0.8037.97",
    "ungoogled_commit": "3e46b13825f808f0886e484d44532372655e5fe4",
    "head_sha": "f7ba75f94442abda7ac3ea81790c217f8636d3ba",
    "platform": "macos", "repository": "ungoogled-software/ungoogled-chromium-macos",
    "repository_id": 177203026, "head_branch": "154.0.8037.97", "event": "push",
    "workflow_path": ".github/workflows/build.yml", "run_id": 37654894671,
}
MAC154_ESBUILD_ARTIFACTS = {
    "x64": (11527088964, "github_build_artifact_x86_64", 12505793478,
            "sha256:f79f7e5769b25d588ab68aa8c2963caeb40e04504b375a97e7288ee77467f186"),
    "arm64": (11520487436, "github_build_artifact_arm64", 10872477177,
              "sha256:4fc2c995fc01213d3e819ff05db169fb14f0f93487b387f23cbf9a7b2e493e6a"),
}
MAC155_ESBUILD_IDENTITY = {
    "chromium_version": "155.0.8059.39",
    "ungoogled_commit": "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a",
    "head_sha": "b1238bb72b02cd36219991d9029c1c65183ea295",
    "platform": "macos", "repository": "ungoogled-software/ungoogled-chromium-macos",
    "repository_id": 177203026, "head_branch": "155.0.8059.39", "event": "push",
    "workflow_path": ".github/workflows/build.yml", "run_id": 38682673288,
}
MAC155_ESBUILD_ARTIFACTS = {
    "x64": (11992595839, "github_build_artifact_x86_64", 12608343117,
            "sha256:9c3f303d6b3e362a1c5efd956a12d0a76ea3083a9d9e92fb231b60fef6e8c437"),
    "arm64": (11971638847, "github_build_artifact_arm64", 10999897218,
              "sha256:8eaaa618155c759175a9a3bb9bed279ecc8893c2a9dae4a8d0e184a42848f970"),
}
MAC_EXTERNAL_TOOL_LINKS = {"third_party/dawn/tools/golang/mac-arm64/bin/go",
                           "third_party/dawn/tools/golang/mac-amd64/bin/go"}
MAC_XCODE_LINK_ROOT = Path("out/Default/sdk/xcode_links")
MAC_XCODE_BASENAMES = {"MacOSX.platform", "XcodeDefault.xctoolchain"}
MAC_SDK = re.compile(r"MacOSX(?:[0-9]+(?:\.[0-9]+)*)?\.sdk\Z")
REQUIRED = ("chrome/VERSION", "BUILD.gn", "out/Default/args.gn",
            "out/Default/build.ninja", "out/Default/.ninja_log", "out/Default/.ninja_deps")


def linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def safe_path(root: Path, relative: Path) -> Path:
    path = importer.safe_path(root, relative)
    if any(linked(part) for part in (path, *path.parents) if importer.contained(part, root)):
        raise Miss(f"linked directory or input: {relative}")
    return path


def local_path(value) -> Path:
    if not str(value).strip():
        raise LocalError("empty local directory argument")
    path = Path(os.path.abspath(Path(value).expanduser()))
    if any(linked(part) for part in (path, *path.parents)):
        raise LocalError("local directory contains a symlink or junction")
    if path.exists() and not path.is_dir():
        raise LocalError("local directory argument is not a directory")
    return path


def check_target(platform: str, arch: str) -> None:
    if (platform, arch) not in TARGETS:
        raise LocalError(f"unsupported target: {platform}-{arch}")


def identities(repo: Path, platform: str, arch: str) -> tuple[dict, dict, dict]:
    identity, _ = importer.repository_identity(repo, platform, arch)
    try:
        pin, manifest = fetcher.load_manifest(platform, arch, root=repo)
    except fetcher.CacheMiss as error:
        raise Miss(f"invalid pinned manifest: {error}") from error
    return identity, pin, manifest


def manifest_matches(recorded: dict, current: dict) -> bool:
    # The manifest's location can change between CI hosts; its digest cannot.
    return isinstance(recorded, dict) and ({key: value for key, value in recorded.items() if key != "path"}
                                           == {key: value for key, value in current.items() if key != "path"})


def gn_assignments_last_wins(path: Path) -> dict:
    """Parse upstream concatenated args, matching merge_gn_args last-wins order."""
    return parse_gn_assignments(path.read_text(encoding="utf-8"))


def parse_gn_assignments(text: str) -> dict:
    """Accept literal scalar/list assignments, never GN expressions or evaluation."""
    assignments = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)", line)
        if not match:
            raise Miss("unsupported GN assignment")
        key, text = match.groups()
        if "$" in text:
            raise Miss(f"unevaluated GN expression: {key}")
        try:
            value, end = json.JSONDecoder().raw_decode(text)
        except ValueError:
            raise Miss(f"nonliteral GN assignment: {key}") from None
        tail = text[end:].strip()
        if tail and not tail.startswith("#"):
            raise Miss(f"unsupported GN expression: {key}")
        def literal(item):
            return (type(item) in (str, int, bool)
                    or (isinstance(item, list) and all(literal(value) for value in item)))
        if not literal(value):
            raise Miss(f"unsupported GN value: {key}")
        assignments[key] = json.dumps(value, separators=(",", ":"))
    return assignments


def source_args(src: Path, identity: dict) -> dict:
    for relative in REQUIRED:
        path = safe_path(src, Path(relative))
        if not path.is_file():
            raise Miss(f"missing complete source/build state: {relative}")
    version = (src / "chrome/VERSION").read_text(encoding="utf-8")
    parts = dict(re.findall(r"^(MAJOR|MINOR|BUILD|PATCH)=(\d+)\s*$", version, re.M))
    if ".".join(parts.get(key, "?") for key in ("MAJOR", "MINOR", "BUILD", "PATCH")) != identity["chromium_version"]:
        raise Miss("restored chrome/VERSION does not match repository pins")
    path = src / "out/Default/args.gn"
    values = gn_assignments_last_wins(path)
    importer.validate_gn_target(values, identity["platform"], identity["arch"])
    raw = path.read_bytes()
    return {"path": "out/Default/args.gn", "text": raw.decode("utf-8"),
            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "assignments": values}


def is_known_external_link(relative: str, platform: str, identity: dict | None = None) -> bool:
    if relative in HOST_LINKS:
        return True
    if relative in (LINUX154_ESBUILD_LINK, LINUX154_ESBUILD_ALIAS):
        if platform not in ("linux", "macos") or not isinstance(identity, dict):
            return False
        records = ((MAC154_ESBUILD_IDENTITY, MAC154_ESBUILD_ARTIFACTS),
                   (MAC155_ESBUILD_IDENTITY, MAC155_ESBUILD_ARTIFACTS)) if platform == "macos" else (
            (LINUX154_ESBUILD_IDENTITY, LINUX154_ESBUILD_ARTIFACTS),
            (LINUX155_ESBUILD_IDENTITY, LINUX155_ESBUILD_ARTIFACTS))
        arch = identity.get("arch")
        if not isinstance(arch, str):
            return False
        for source_identity, artifacts in records:
            if arch not in artifacts:
                continue
            artifact_id, name, size, digest = artifacts[arch]
            expected = dict(source_identity, arch=arch, artifact_id=artifact_id,
                            artifact_name=name, artifact_size_in_bytes=size, artifact_digest=digest)
            if (identity == expected
                    and all(type(identity[key]) is type(value) for key, value in expected.items())):
                return True
        return False
    if platform != "macos":
        return False
    if relative in MAC_EXTERNAL_TOOL_LINKS:
        return True
    prefix = MAC_XCODE_LINK_ROOT.as_posix() + "/"
    if not relative.startswith(prefix):
        return False
    basename = relative[len(prefix):]
    return "/" not in basename and (basename in MAC_XCODE_BASENAMES or MAC_SDK.fullmatch(basename) is not None)


def missing_host_links(cache: Path, donor: Path, result: dict, platform: str,
                       identity: dict | None = None) -> list[str]:
    omitted = result.get("external_symlink_paths", [])
    count = result.get("skipped_external_symlinks", 0)
    if type(count) is not int or count < 0 or not isinstance(omitted, list) or len(omitted) != count:
        raise Miss("incomplete donor source: omitted links were not recorded")
    paths = []
    for name in omitted:
        if not isinstance(name, str) or fetcher.safe_name(name) != name:
            raise Miss("incomplete donor source: invalid omitted link path")
        # Fetcher records archive-relative names, extracted beneath cache/tree.
        path = safe_path(cache, Path("tree") / name)
        if not importer.contained(path, donor) or not is_known_external_link(path.relative_to(donor).as_posix(), platform, identity):
            raise Miss("incomplete donor source: unknown external symlink")
        if path.exists() or linked(path):
            raise Miss("omitted external link unexpectedly exists")
        paths.append(path.relative_to(donor).as_posix())
    if len(set(paths)) != len(paths):
        raise Miss("duplicate omitted external symlink")
    if LINUX154_ESBUILD_ALIAS in paths and LINUX154_ESBUILD_LINK not in paths:
        raise Miss("incomplete esbuild external symlink chain")
    return sorted(paths)


def donor_counts(src: Path) -> dict:
    counts = {"files_moved": 0, "bytes_moved": 0, "symlinks_moved": 0}

    def walk_error(error):
        raise error

    for directory, dirs, files in os.walk(src, followlinks=False, onerror=walk_error):
        for name in dirs + files:
            path = Path(directory) / name
            relative = path.relative_to(src).as_posix()
            if name.lower().startswith(".chromix") or relative.lower() == "out/chromix":
                raise Miss(f"donor contains Chromix marker: {relative}")
            info = path.lstat()
            if linked(path):
                if not path.is_symlink():
                    raise Miss(f"donor contains a junction: {relative}")
                target = os.readlink(path)
                if (os.path.isabs(target) or PureWindowsPath(target).drive
                        or not importer.contained(path.resolve(), src)):
                    raise Miss(f"donor contains an external symlink: {relative}")
                counts["symlinks_moved"] += 1
            elif stat.S_ISREG(info.st_mode):
                counts["files_moved"] += 1
                counts["bytes_moved"] += info.st_size
            elif not stat.S_ISDIR(info.st_mode):
                raise Miss(f"donor contains a special file: {relative}")
    return counts


def ninja_state_diagnostics(src: Path) -> dict:
    """Keep available bounded evidence without following unsafe metadata paths."""
    state = {}
    for name, limit in ((".ninja_log", 128), (".ninja_deps", 16)):
        relative = Path("out/Default") / name
        evidence = {"path": relative.as_posix()}
        try:
            path = safe_path(src, relative)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise Miss("Ninja metadata is not a regular file")
            with path.open("rb") as stream:
                header = stream.readline(limit + 1) if name == ".ninja_log" else stream.read(limit)
            evidence.update(size_bytes=info.st_size, header_hex=header[:limit].hex(),
                            header_truncated=len(header) > limit)
        except (OSError, ValueError, RuntimeError) as error:
            evidence["error"] = str(error)[:256]
        state[name] = evidence
    return state


def ninja_mtime_plan(src: Path) -> dict:
    """Plan only exact whole-second output repairs; never adjust input mtimes."""
    out = safe_path(src, Path("out/Default"))
    deps = objects.ninja_deps(safe_path(out, Path(".ninja_deps")))
    logs = objects.ninja_log(safe_path(out, Path(".ninja_log")))
    skipped = Counter()
    skipped_samples = []
    repairs = []
    # Cache applies only to this read-only planning pass; revalidate before use.
    input_stats = {}

    def checked_input(value):
        dependency = objects.relative_path(value, out, src)
        safe_path(src, dependency.relative_to(src))
        if not dependency.is_file():
            raise Miss("recorded input is missing")
        return dependency.stat()

    def input_identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)

    for name, (recorded, inputs) in deps.items():
        active_input = None
        try:
            if name in {"args.gn", "build.ninja", ".ninja_log", ".ninja_deps"}:
                raise Miss("build metadata is not a repairable output")
            path = objects.relative_path(name, out, out, output=True)
            safe_path(out, path.relative_to(out))
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Miss("output is not an independent regular file")
            if info.st_mtime_ns == recorded:
                skipped["already exact"] += 1
                continue
            if (recorded <= 0 or recorded % NANOSECOND == 0
                    or info.st_mtime_ns != recorded // NANOSECOND * NANOSECOND):
                raise Miss("output mtime is not the exact whole-second floor")
            if name not in logs:
                raise Miss("output has no command log record")
            logged, _, version = logs[name]
            freshness = objects.object_times(info.st_mtime_ns, recorded, logged, version, True)
            if not inputs:
                raise Miss("output has no recorded inputs")
            for value in inputs:
                active_input = value
                if value not in input_stats:
                    input_stats[value] = checked_input(value)
                if not objects.input_is_fresh(input_stats[value].st_mtime_ns, freshness):
                    raise Miss("recorded input is newer or same-second ambiguous")
            repairs.append({"output": name, "from_ns": info.st_mtime_ns, "to_ns": recorded})
        except (OSError, ValueError, RuntimeError) as error:
            skipped[str(error)] += 1
            if len(skipped_samples) < 32:
                sample = {"output": name[:2048], "reason": str(error)[:512]}
                if active_input is not None:
                    sample["input"] = active_input[:2048]
                skipped_samples.append(sample)
    for value, original in input_stats.items():
        try:
            current = checked_input(value)
        except (ValueError, RuntimeError) as error:
            raise Miss(f"recorded input changed during timestamp planning: {value}: {error}") from error
        if input_identity(current) != input_identity(original):
            raise Miss(f"recorded input changed during timestamp planning: {value}")
    return {"outputs_restored": len(repairs), "repairs": repairs, "skipped": dict(skipped),
            "skipped_samples": skipped_samples}


def apply_mtime_plan(src: Path, plan: dict, journal: list) -> None:
    out = src / "out/Default"
    for entry in plan["repairs"]:
        path = safe_path(out, Path(entry["output"]))
        info = path.stat()
        if info.st_mtime_ns != entry["from_ns"] or info.st_nlink != 1:
            raise Miss("output changed during timestamp restoration")
        journal.append((path, info.st_atime_ns, info.st_mtime_ns))
        os.utime(path, ns=(info.st_atime_ns, entry["to_ns"]), follow_symlinks=False)
        if path.stat().st_mtime_ns != entry["to_ns"]:
            raise Miss("filesystem cannot preserve Ninja nanosecond timestamps")


def raise_rollback_error(error: BaseException, rollback_error: BaseException, source: Path) -> None:
    message = f"rollback failed; preserved owned source at {source}: {rollback_error}"
    if not isinstance(error, Exception):
        failure = error
    elif not isinstance(rollback_error, Exception):
        failure = rollback_error
    else:
        failure = LocalError(message)
    failure.preserved_source = str(source)
    if not isinstance(failure, Exception):
        failure.add_note(message)
    if failure is rollback_error:
        raise failure
    raise failure from rollback_error


def restore_ninja_output_mtimes(src: Path) -> dict:
    """Repair a trusted extracted source tree; callers must validate its provenance."""
    src = Path(src).resolve()
    plan = ninja_mtime_plan(src)
    journal = []
    try:
        apply_mtime_plan(src, plan, journal)
    except BaseException as error:
        try:
            for path, atime, mtime in reversed(journal):
                os.utime(path, ns=(atime, mtime), follow_symlinks=False)
        except BaseException as rollback_error:
            raise_rollback_error(error, rollback_error, src)
        raise
    return plan


def verify_restored(workdir: Path, platform: str, arch: str, repo: Path = REPO) -> dict:
    """Return a current pinned restore receipt, without asserting build cache hits."""
    check_target(platform, arch)
    work = local_path(workdir)
    src = work / "src"
    if linked(src) or not src.is_dir():
        raise Miss("restored source directory is missing or linked")
    receipt = importer.read_json(safe_path(src, Path(MARKER)))
    identity, _, manifest = identities(Path(repo), platform, arch)
    if (receipt.get("schema_version") != 1 or receipt.get("owner") != OWNER
            or receipt.get("status") != "restored"
            or receipt.get("extraction_scope") != fetcher.SOURCE_SCOPE):
        raise Miss("invalid restored ownership or extraction scope")
    if (receipt.get("identity") != identity or not manifest_matches(receipt.get("manifest"), manifest)
            or receipt.get("platform") != platform or receipt.get("arch") != arch):
        raise Miss("restored receipt does not match current manifest/platform/architecture pins")
    original = receipt.get("original_args")
    if not isinstance(original, dict) or not isinstance(original.get("text"), str):
        raise Miss("restored receipt has no original GN args")
    raw = original["text"].encode("utf-8")
    if (original.get("bytes") != len(raw) or original.get("sha256") != hashlib.sha256(raw).hexdigest()
            or original.get("path") != "out/Default/args.gn"
            or original.get("assignments") != parse_gn_assignments(original["text"])):
        raise Miss("restored original GN args record is inconsistent")
    original_values = original["assignments"]
    for name in ("target_cpu", "v8_target_cpu"):
        if (name == "target_cpu" or name in original_values) and original_values.get(name) != json.dumps(arch):
            raise Miss("restored original GN args architecture is inconsistent")
    importer.validate_gn_target(original_values, platform, arch)
    links = receipt.get("external_symlink_paths")
    if (not isinstance(links, list)
            or any(not isinstance(name, str) or not is_known_external_link(name, platform, identity) for name in links)
            or len(set(links)) != len(links)):
        raise Miss("restored receipt has unknown omitted host links")
    if LINUX154_ESBUILD_ALIAS in links and LINUX154_ESBUILD_LINK not in links:
        raise Miss("restored receipt has incomplete esbuild external symlink chain")
    # Preparation may legitimately regenerate args and Ninja state after restoring.
    source_args(src, identity)
    return receipt


def is_restored(workdir: Path, platform: str | None = None, arch: str | None = None,
                repo: Path = REPO) -> dict | None:
    """Return the verified receipt, None if absent, or raise on an invalid receipt."""
    work = local_path(workdir)
    src = work / "src"
    if linked(src):
        raise LocalError("source directory is symlinked or junctioned")
    marker = safe_path(src, Path(MARKER))
    if not marker.exists():
        return None
    receipt = importer.read_json(marker)
    return verify_restored(work, platform or receipt.get("platform"), arch or receipt.get("arch"), repo)


def no_copy(source, destination):
    raise Miss("same-filesystem rename failed; copy fallback is disabled")


def install(donor: Path, work: Path, receipt: dict) -> None:
    if donor.stat().st_dev != work.stat().st_dev or donor.anchor.lower() != work.anchor.lower():
        raise Miss("cross-device restore is unsupported; keep cache and WORK on the same volume")
    # Private staging contains any partial shutil.move fallback, never WORK/src.
    transaction = Path(tempfile.mkdtemp(prefix=".chromix-upstream-restore-", dir=work))
    staged = transaction / "src"
    destination = work / "src"
    original = donor.stat()
    journal = []
    keep = True

    def original_directory(path):
        if linked(path):
            return False
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return False
        return stat.S_ISDIR(info.st_mode) and (
            info.st_dev, info.st_ino) == (original.st_dev, original.st_ino)

    try:
        shutil.move(str(donor), str(staged), copy_function=no_copy)
        apply_mtime_plan(staged, receipt["ninja_mtimes"], journal)
        importer.write_json(staged / MARKER, receipt)
        os.utime(staged, ns=(original.st_atime_ns, original.st_mtime_ns))
        if destination.exists() or linked(destination):
            raise LocalError("WORK/src appeared during restoration; it was not overwritten")
        os.rename(staged, destination)
        keep = False
    except BaseException as error:
        location = staged
        try:
            donor_retained = original_directory(donor)
            if not donor_retained:
                if not staged.exists() and not linked(staged):
                    if not original_directory(destination):
                        raise LocalError("moved source could not be located")
                    location = destination
                for path, atime, mtime in reversed(journal):
                    path = location / path.relative_to(staged)
                    os.utime(path, ns=(atime, mtime), follow_symlinks=False)
                (location / MARKER).unlink(missing_ok=True)
                os.utime(location, ns=(original.st_atime_ns, original.st_mtime_ns))
                if donor.exists() or linked(donor):
                    raise LocalError("original donor path is occupied")
                os.rename(location, donor)
            keep = False
        except BaseException as rollback_error:
            # A signal may arrive after the rollback rename already succeeded.
            try:
                if original_directory(donor):
                    location = donor
            except OSError:
                pass
            raise_rollback_error(error, rollback_error, location)
        raise
    finally:
        if not keep:
            shutil.rmtree(transaction)


def write_report(work: Path, entry: dict) -> None:
    if linked(work / REPORT):
        raise LocalError("diagnostic report is symlinked or junctioned")
    importer.write_json(work / REPORT, {key: value for key, value in entry.items() if key != "receipt"})


def cache_entries(cache: Path, locked: bool = False) -> None:
    allowed = {"result.json", "tree", ".download.zip", ".inner", ".result.tmp"}
    if locked:
        allowed.add(".lock")
    for path in cache.iterdir():
        if (path.name not in allowed or linked(path)
                or (path.name == "tree" and not path.is_dir())
                or (path.name != "tree" and not path.is_file())):
            raise Miss("cache contains unowned or linked entries")


def _cleanup_owned_miss(cache: Path, result: dict, reason: str) -> dict:
    """Remove only tree after pinned ownership validation, while holding .lock."""
    if (result.get("owner") != fetcher.OWNER or result.get("destination") != str(cache)
            or result.get("status") != "hit" or result.get("extraction_scope") != fetcher.SOURCE_SCOPE):
        raise Miss("cleanup skipped: cache ownership not verified")
    if not safe_path(cache, Path(".lock")).is_file():
        raise Miss("cleanup skipped: cache lock is missing")
    cache_entries(cache, locked=True)
    result_path = safe_path(cache, Path("result.json"))
    if importer.read_json(result_path) != result:
        raise Miss("cleanup skipped: downloader receipt changed")
    tree = safe_path(cache, Path("tree"))
    removed = int(tree.exists())
    rejected = dict(result, status="miss", source=None, download_status=result["status"],
                    reason="restore rejected: " + reason)
    # Invalidate the hit before deleting its files, including on interrupted cleanup.
    importer.write_json(result_path, rejected)
    if removed:
        shutil.rmtree(tree)
    return {"status": "removed", "cache_entries_removed": removed, "path": str(tree)}


def restore(workdir: Path, platform: str, arch: str, cache_dir: Path,
            repo: Path = REPO) -> dict:
    """Move an owned cache into absent WORK/src; clean rejected owned source trees."""
    check_target(platform, arch)
    work, cache, repo = local_path(workdir), local_path(cache_dir), Path(repo).resolve()
    src = work / "src"
    if (work == repo or work in repo.parents or work == Path.home() or work == Path(work.anchor)
            or importer.contained(work, cache) or importer.contained(cache, src)
            or importer.contained(repo, cache)):
        raise LocalError("unsafe overlapping work/cache/repository directories")
    if linked(src) or linked(work / REPORT):
        raise LocalError("source or diagnostic report is symlinked or junctioned")
    work.mkdir(parents=True, exist_ok=True)
    started = phase_started = time.monotonic()
    entry = {"schema_version": 1, "platform": platform, "arch": arch, "status": "miss",
             "reasons": [], "counts": {"files_moved": 0, "bytes_moved": 0, "symlinks_moved": 0},
             "phase_durations_seconds": {},
             "progress": {"files_counted": 0, "bytes_counted": 0, "symlinks_counted": 0,
                          "outputs_planned": 0, "outputs_skipped": 0}}

    def report_phase(phase):
        nonlocal phase_started
        now = time.monotonic()
        previous = entry.get("phase")
        if previous is not None:
            durations = entry["phase_durations_seconds"]
            durations[previous] = round(durations.get(previous, 0) + now - phase_started, 3)
        phase_started = now
        entry.update(phase=phase, duration_seconds=round(now - started, 3))
        print(f"upstream cache restore: phase={phase}; duration_seconds={entry['duration_seconds']}; "
              f"progress={json.dumps(entry['progress'], sort_keys=True)}", file=sys.stderr, flush=True)
        write_report(work, entry)

    lock = cache / ".lock"
    locked = False
    cache_owned = False
    cleanup_reason = None
    validation_complete = False
    try:
        report_phase("validate_cache")
        if src.exists():
            raise Miss("WORK/src already exists; it was not inspected, overwritten, or removed")
        if importer.resume_marker(work, src):
            raise Miss("resume marker exists; donor was not consumed")
        if lock.exists() or linked(lock):
            raise Miss("downloader is still writing the cache")
        result_path = safe_path(cache, Path("result.json"))
        result = importer.read_json(result_path)
        identity, pin, manifest = identities(repo, platform, arch)
        donor = importer.donor_source(cache, result, identity, repo)
        safe_path(cache, donor.relative_to(cache))
        if result.get("extraction_scope") != fetcher.SOURCE_SCOPE:
            raise Miss("restore requires extraction_scope=source-and-objects")
        if not manifest_matches(result.get("manifest"), manifest):
            raise Miss("downloader manifest provenance does not match current pins")
        if donor.relative_to(cache / "tree").as_posix() not in pin["source_roots"]:
            raise Miss("donor source layout does not match pinned platform")
        cache_entries(cache)
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        locked = True
        if importer.read_json(result_path) != result:
            raise Miss("downloader receipt changed during restoration")
        cache_owned = True
        entry["ninja_state"] = ninja_state_diagnostics(donor)
        report_phase("validate_source")
        try:
            original_args = source_args(donor, identity)
            omitted = missing_host_links(cache, donor, result, platform, identity)
            report_phase("donor_counts")
            counts = donor_counts(donor)
            entry["progress"].update(files_counted=counts["files_moved"],
                                     bytes_counted=counts["bytes_moved"],
                                     symlinks_counted=counts["symlinks_moved"])
            report_phase("ninja_mtime_plan")
            plan = ninja_mtime_plan(donor)
            entry["progress"].update(outputs_planned=plan["outputs_restored"],
                                     outputs_skipped=sum(plan["skipped"].values()))
        except (ValueError, RuntimeError, fetcher.CacheMiss) as error:
            cleanup_reason = str(error)
            raise
        validation_complete = True
        receipt = {"schema_version": 1, "owner": OWNER, "status": "restored",
                   "identity": identity, "manifest": manifest, "platform": platform, "arch": arch,
                   "extraction_scope": fetcher.SOURCE_SCOPE, "original_args": original_args,
                   "counts": counts, "ninja_mtimes": plan, "ninja_state": entry["ninja_state"],
                   "external_symlink_paths": omitted,
                   "archive_external_symlink_paths": result.get("external_symlink_paths", []),
                   "environment": {"compiler": "unverified", "sdk": "unverified",
                                   "external_inputs": "unverified", "cache_hit_proven": False}}
        report_phase("install")
        install(donor, work, receipt)
        entry.update(status="hit", reasons=["full pinned upstream source restored; preparation and environment checks still required"],
                     counts=counts, receipt=receipt, receipt_path=str(src / MARKER))
        result.update(status="consumed", download_status="hit", source=None, restored_to=str(src))
        try:
            importer.write_json(result_path, result)
        except OSError as error:
            entry["reasons"].append(f"restored successfully; downloader receipt update failed: {error}")
    except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError,
            fetcher.CacheMiss, LocalError) as error:
        entry["reasons"] = [str(error)]
        if getattr(error, "preserved_source", None) is not None:
            entry["preserved_source"] = error.preserved_source
        if isinstance(error, LocalError):
            raise
    except BaseException as error:
        entry["reasons"] = [f"{type(error).__name__}: {error}"]
        if getattr(error, "preserved_source", None) is not None:
            entry["preserved_source"] = error.preserved_source
        raise
    finally:
        try:
            if entry["status"] == "miss":
                entry["failed_phase"] = entry.get("phase")
                try:
                    report_phase("failed")
                finally:
                    if cache_owned and cleanup_reason is not None:
                        try:
                            entry["cleanup"] = _cleanup_owned_miss(cache, result, cleanup_reason)
                            entry["counts"]["cache_entries_removed"] = entry["cleanup"]["cache_entries_removed"]
                        except (OSError, ValueError, RuntimeError) as error:
                            entry["cleanup"] = {"status": "failed", "reason": str(error)}
                            entry["reasons"].append(f"owned cache cleanup failed: {error}")
                    elif "preserved_source" in entry:
                        entry["cleanup"] = {"status": "preserved", "path": entry["preserved_source"],
                                            "reason": "installation rollback failed; source preserved at reported path"}
                    else:
                        entry["cleanup"] = {"status": "preserved", "reason":
                                            "installation failed; donor retained for retry" if validation_complete else
                                            "ownership not fully validated or source validation had an I/O error"}
        finally:
            if locked:
                lock.unlink()
            if entry["status"] == "hit":
                report_phase("complete")
            else:
                entry["duration_seconds"] = round(time.monotonic() - started, 3)
                write_report(work, entry)
    return entry


def run_restore(phase: str, platform: str, arch: str, workdir: Path,
                cache_dir: Path | None = None, repo: Path = REPO) -> dict:
    if phase == "verify":
        receipt = verify_restored(workdir, platform, arch, repo)
        return {"status": "verified", "receipt": receipt, "reasons": ["restored identity matches current pins"]}
    if phase != "restore" or cache_dir is None:
        raise LocalError("restore requires --cache-dir; phase must be restore or verify")
    return restore(workdir, platform, arch, cache_dir, repo)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("restore", "verify"), required=True)
    parser.add_argument("--platform", choices=("linux", "macos", "windows"), required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        entry = run_restore(args.phase, args.platform, args.arch, args.workdir, args.cache_dir)
    except (LocalError, OSError, ValueError, KeyError, TypeError, RuntimeError, fetcher.CacheMiss) as error:
        print(f"upstream cache {args.phase}: error: {error}", file=sys.stderr)
        return 2 if isinstance(error, LocalError) else 1
    print(f"upstream cache {args.phase}: {entry['status']}: {'; '.join(entry['reasons'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
