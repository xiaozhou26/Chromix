#!/usr/bin/env python3
"""Select authenticated version/platform overrides without changing legacy stacks."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

try:
    from .platform_pins import PLATFORMS, load_pins, load_shared_pins
except ImportError:
    from platform_pins import PLATFORMS, load_pins, load_shared_pins

VERSION = "154.0.8037.97"
CORE = "37085e47cf580c815a30402917d350ce97399ded"
MACOS154_CORE = "3e46b13825f808f0886e484d44532372655e5fe4"
MACOS154_ADDITIONS_ROOT = "patches/chromium154-macos"
MACOS154_ADDITIONS_MANIFEST = "build/patch-additions-macos154.json"
MACOS154_ADDITIONS_SHA256 = "be73565822a8b4177e63ea16d6e5936d263fed0fbf011366b3fcda4ec7b9a8bd"
PLATFORM_COMMITS = {
    "windows": "f03c33d7974af5b40f25b01984ded8418d60fbe4",
    "linux": "f1441a6efb4d79427d1a3180b1877bbbdb1af7aa",
    "macos": "f7ba75f94442abda7ac3ea81790c217f8636d3ba",
}
LEGACY_VERSIONS = {"152.0.7977.82", "153.0.8010.36", "153.0.8010.47"}
OVERRIDE_ROOT = "patches/chromium154"
# Original and replacement hashes bind each override to its reviewed predecessor.
OVERRIDES = {
    "0049-third_party-blink-renderer-modules-plugins-dom_plugin_array-cc.patch": (
        "4a454a46e7541c1526607206f492ee4b6f7f9db55ea5ba7e9f5899d213b915f3",
        "3799f3cf3e8c143da3675789b3431e6ba6151d58be9c2d348b95b3ffcb4969f0"),
    "0060-third_party-blink-renderer-modules-media_capabilities-media_capabilities-cc.patch": (
        "e050effa3a2cc8e84809bf5184b6c18417718cb3f9f09a1aa63dadaaf14514bb",
        "f03940003568f0568c5b81b9fe94f4fce02209b55e6b7492bd7d8589927c5f28"),
    "0061-third_party-blink-renderer-modules-media_capabilities-media_capabilities-h.patch": (
        "59fe7e3903d37f1ac92bf5deeb7e22af87938494d02f8e6b89f6671340e5bf9c",
        "a939e7968448ee4179589a1653d921b38a6968026d8c150519a5575cf117ada9"),
    "0111-global-privacy-control.patch": (
        "98af6f9657ebe1c92f2bbd170fa038d97389b6126a2962db50bf16129ab13b79",
        "0a7ef68eef8a51ebfbfb84ffd7775e56a43b8694feb7052b562ce3397e936040"),
    "0167-pdf-backend-capability.patch": (
        "7d2ca45084a5a9b206a6d5e1a7a1e13d53999f1e9786ded9ff5ff657fa09379a",
        "fa05702f631f2bcdfe5783b0d0c8163a755ade728cc81d8e438f0e52575d476d"),
    "0177-audio-graph-isolation.patch": (
        "8afa9f8ee0b8b9f5895183279e99eaf661735fd78052c93143e8da1557918bfc",
        "0a79069deaeda83a20945f393a156c66de1b73ee54ae261f6c8661132bffd068"),
    "0222-canvas-readback-shared-noise.patch": (
        "b86ce9d384d17b0ba0d0d0ca4b9dec8b5b2341afccf821c269735cf301a8fd09",
        "3260831685393468941ba4fb8869ac7ec9441c8ef4033c6c80ee360ddc8a0045"),
}


VERSION155 = "155.0.8059.39"
CORE155 = "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a"
PLATFORM155_COMMITS = {
    "macos": "b1238bb72b02cd36219991d9029c1c65183ea295",
    "windows": "989b4382364baaddb8b3180e1eb1030ca96a7273",
    "linux": "025d99db1673a21992534551598b73ca79360762",
}
OVERRIDE155_ROOT = "patches/chromium155"
LINUX155_ADDITIONS_ROOT = "patches/chromium155-linux"
LINUX155_ADDITIONS_MANIFEST = "build/patch-additions-linux155.json"
LINUX155_LEGACY_ADDITIONS_SHA256 = "28fb67c9207bfe6e9b8314a0e56856c5136cc6a98461ad826f9ad7e8c4fbe38f"
LINUX155_ADDITIONS_SHA256 = "5480c06026cf67f27f23fc4bdff4122c294cb0a8a16c4b5dfd7865a72e6c6f1f"
MACOS155_ADDITIONS_ROOT = "patches/chromium155-macos"
MACOS155_ADDITIONS_MANIFEST = "build/patch-additions-macos155.json"
MACOS155_ADDITIONS_SHA256 = "94bb12f24f1c840f5a434bdc24971ab948be908944fd7c2fb51e6a39852af64f"
WINDOWS155_ADDITIONS_ROOT = "patches/chromium155-windows"
WINDOWS155_ADDITIONS_MANIFEST = "build/patch-additions-windows155.json"
WINDOWS155_ADDITIONS_SHA256 = "4e4b2c3655b2ba1c1f5b2601d5d467ee58f98e8f535214719f1f92c5016ebddd"
OVERRIDES155 = {
    "0031-third_party-blink-renderer-platform-graphics-image_data_buffer-cc.patch": (
        "8b86c52d9df1ba45db00a09b293070be90d9dbc7463244c8ddf525017d265b7f",
        "4f6d2c1d7286d226361af69443810c2cf539a42b622dc4a8bec3910dbf065fd5"),
    "0049-third_party-blink-renderer-modules-plugins-dom_plugin_array-cc.patch": (
        "4a454a46e7541c1526607206f492ee4b6f7f9db55ea5ba7e9f5899d213b915f3",
        "3799f3cf3e8c143da3675789b3431e6ba6151d58be9c2d348b95b3ffcb4969f0"),
    "0060-third_party-blink-renderer-modules-media_capabilities-media_capabilities-cc.patch": (
        "e050effa3a2cc8e84809bf5184b6c18417718cb3f9f09a1aa63dadaaf14514bb",
        "f03940003568f0568c5b81b9fe94f4fce02209b55e6b7492bd7d8589927c5f28"),
    "0061-third_party-blink-renderer-modules-media_capabilities-media_capabilities-h.patch": (
        "59fe7e3903d37f1ac92bf5deeb7e22af87938494d02f8e6b89f6671340e5bf9c",
        "a939e7968448ee4179589a1653d921b38a6968026d8c150519a5575cf117ada9"),
    "0077-third_party-blink-renderer-modules-canvas-canvas2d-base_rendering_context_2d-h.patch": (
        "4479b87f37067cecbbe29135af9c80e500bf1e5d068763fec99c5bab033878d5",
        "148644312c9f40aa74ce630aefdea9167fd39b7a1f018f2e834995be173b29aa"),
    "0082-third_party-blink-renderer-modules-webgl-webgl_rendering_context_base-cc.patch": (
        "78da0f157e275f5c219e71b160db13a09c60aad0731b7f857d39c89bc9b0e2a4",
        "785ca10a8d8eda8847a4183af0ed97d059b3212cbd22804329a4f96b91088212"),
    "0111-global-privacy-control.patch": (
        "98af6f9657ebe1c92f2bbd170fa038d97389b6126a2962db50bf16129ab13b79",
        "0a7ef68eef8a51ebfbfb84ffd7775e56a43b8694feb7052b562ce3397e936040"),
    "0167-pdf-backend-capability.patch": (
        "7d2ca45084a5a9b206a6d5e1a7a1e13d53999f1e9786ded9ff5ff657fa09379a",
        "fa05702f631f2bcdfe5783b0d0c8163a755ade728cc81d8e438f0e52575d476d"),
    "0177-audio-graph-isolation.patch": (
        "8afa9f8ee0b8b9f5895183279e99eaf661735fd78052c93143e8da1557918bfc",
        "0a79069deaeda83a20945f393a156c66de1b73ee54ae261f6c8661132bffd068"),
    "0222-canvas-readback-shared-noise.patch": (
        "b86ce9d384d17b0ba0d0d0ca4b9dec8b5b2341afccf821c269735cf301a8fd09",
        "3260831685393468941ba4fb8869ac7ec9441c8ef4033c6c80ee360ddc8a0045"),
}


MACOS152_VERSION = "152.0.7977.82"
MACOS152_CORE = "e71b91c6e336d0f25cfc6b9ef09298a9d2506e24"
MACOS152_PLATFORM = "038db2b41f7aeb00bbceb2f5a56912b26eb5b284"
MACOS152_OVERRIDE_ROOT = "patches/chromium152"
MACOS152_OVERRIDES = {
    "0209-display-native-screen-regressions.patch": (
        "fb94be385764e9bc6836033744596be85805d44ac9132a113e770da27c723aa1",
        "d35e08b61cdfbddf12960b670c33bd662e977d84d9aa79ce66e17b30286cead5"),
}


class SelectionError(ValueError):
    """Patch selection cannot establish a complete, matching stack."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(repo: Path, name: str) -> bytes:
    if (not name or any(c in name for c in "\\:\x00")
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise SelectionError("unsafe patch input path: " + name)
    path = repo
    for part in name.split("/"):
        path = path / part
        if path.is_symlink():
            raise SelectionError("symlink patch input: " + name)
    if not path.is_file():
        raise SelectionError("missing patch input: " + name)
    return path.read_bytes()


def source_version(src: Path) -> str:
    fields = {}
    for line in read(src, "chrome/VERSION").decode("ascii").splitlines():
        if not re.fullmatch(r"(?:MAJOR|MINOR|BUILD|PATCH)=[0-9]+", line):
            raise SelectionError("malformed source chrome/VERSION")
        key, value = line.split("=")
        if key in fields:
            raise SelectionError("duplicate source version field")
        fields[key] = value
    if set(fields) != {"MAJOR", "MINOR", "BUILD", "PATCH"}:
        raise SelectionError("incomplete source version")
    return ".".join(fields[key] for key in ("MAJOR", "MINOR", "BUILD", "PATCH"))


def validate_overrides(repo: Path, root: str = OVERRIDE_ROOT,
                       overrides: dict = OVERRIDES, label: str = "154") -> None:
    directory = repo / root
    if directory.is_symlink() or not directory.is_dir():
        raise SelectionError(f"missing or linked Chromium {label} override directory")
    if {path.name for path in directory.iterdir()} != set(overrides):
        raise SelectionError(f"partial or unexpected Chromium {label} override inventory")
    for name, (original, replacement) in overrides.items():
        if digest(read(repo, "patches/" + name)) != original:
            raise SelectionError("mismatched override predecessor: " + name)
        if digest(read(repo, root + "/" + name)) != replacement:
            raise SelectionError(f"changed Chromium {label} override: " + name)


def linux155_additions(repo: Path) -> tuple[list[str], dict | None]:
    manifest = repo / LINUX155_ADDITIONS_MANIFEST
    directory = repo / LINUX155_ADDITIONS_ROOT
    # Pre-addition donors have neither input; their exact commit is authenticated by migration.
    if not manifest.exists() and not manifest.is_symlink():
        if directory.exists() or directory.is_symlink():
            raise SelectionError("Linux155 additions directory requires its pinned manifest")
        return [], None
    raw = read(repo, LINUX155_ADDITIONS_MANIFEST)
    if digest(raw) not in (LINUX155_LEGACY_ADDITIONS_SHA256, LINUX155_ADDITIONS_SHA256):
        raise SelectionError("changed Linux155 additions manifest")
    additions = json.loads(raw)
    if directory.is_symlink() or not directory.is_dir():
        raise SelectionError("missing or linked Linux155 additions directory")
    entries = additions["patches"]
    if {path.name for path in directory.iterdir()} != {Path(entry["path"]).name for entry in entries}:
        raise SelectionError("partial or unexpected Linux155 additions inventory")
    for entry in entries:
        if digest(read(repo, entry["path"])) != entry["sha256"]:
            raise SelectionError("changed Linux155 addition: " + entry["path"])
    return [entry["path"] for entry in entries], {
        "schema_version": additions["schema_version"], "kind": additions["kind"],
        "manifest": {"path": LINUX155_ADDITIONS_MANIFEST, "sha256": digest(raw)},
        "patches": entries,
    }


def macos154_additions(repo: Path) -> tuple[list[str], dict | None]:
    manifest = repo / MACOS154_ADDITIONS_MANIFEST
    directory = repo / MACOS154_ADDITIONS_ROOT
    # Pre-addition donors have neither input; their exact commit is authenticated by migration.
    if not manifest.exists() and not manifest.is_symlink():
        if directory.exists() or directory.is_symlink():
            raise SelectionError("macOS154 additions directory requires its pinned manifest")
        return [], None
    raw = read(repo, MACOS154_ADDITIONS_MANIFEST)
    if digest(raw) != MACOS154_ADDITIONS_SHA256:
        raise SelectionError("changed macOS154 additions manifest")
    additions = json.loads(raw)
    if directory.is_symlink() or not directory.is_dir():
        raise SelectionError("missing or linked macOS154 additions directory")
    entries = additions["patches"]
    if {path.name for path in directory.iterdir()} != {Path(entry["path"]).name for entry in entries}:
        raise SelectionError("partial or unexpected macOS154 additions inventory")
    for entry in entries:
        if digest(read(repo, entry["path"])) != entry["sha256"]:
            raise SelectionError("changed macOS154 addition: " + entry["path"])
    return [entry["path"] for entry in entries], {
        "schema_version": additions["schema_version"], "kind": additions["kind"],
        "manifest": {"path": MACOS154_ADDITIONS_MANIFEST, "sha256": digest(raw)},
        "patches": entries,
    }


def macos155_additions(repo: Path) -> tuple[list[str], dict | None]:
    manifest = repo / MACOS155_ADDITIONS_MANIFEST
    directory = repo / MACOS155_ADDITIONS_ROOT
    # Pre-addition donors have neither input; their exact commit is authenticated by migration.
    if not manifest.exists() and not manifest.is_symlink():
        if directory.exists() or directory.is_symlink():
            raise SelectionError("macOS155 additions directory requires its pinned manifest")
        return [], None
    raw = read(repo, MACOS155_ADDITIONS_MANIFEST)
    if digest(raw) != MACOS155_ADDITIONS_SHA256:
        raise SelectionError("changed macOS155 additions manifest")
    additions = json.loads(raw)
    if directory.is_symlink() or not directory.is_dir():
        raise SelectionError("missing or linked macOS155 additions directory")
    entries = additions["patches"]
    if {path.name for path in directory.iterdir()} != {Path(entry["path"]).name for entry in entries}:
        raise SelectionError("partial or unexpected macOS155 additions inventory")
    for entry in entries:
        if digest(read(repo, entry["path"])) != entry["sha256"]:
            raise SelectionError("changed macOS155 addition: " + entry["path"])
    return [entry["path"] for entry in entries], {
        "schema_version": additions["schema_version"], "kind": additions["kind"],
        "manifest": {"path": MACOS155_ADDITIONS_MANIFEST, "sha256": digest(raw)},
        "patches": entries,
    }


def windows155_additions(repo: Path) -> tuple[list[str], dict | None]:
    manifest = repo / WINDOWS155_ADDITIONS_MANIFEST
    directory = repo / WINDOWS155_ADDITIONS_ROOT
    # Pre-addition donors have neither input; their exact commit is authenticated by migration.
    if not manifest.exists() and not manifest.is_symlink():
        if directory.exists() or directory.is_symlink():
            raise SelectionError("Windows155 additions directory requires its pinned manifest")
        return [], None
    raw = read(repo, WINDOWS155_ADDITIONS_MANIFEST)
    if digest(raw) != WINDOWS155_ADDITIONS_SHA256:
        raise SelectionError("changed Windows155 additions manifest")
    additions = json.loads(raw)
    if directory.is_symlink() or not directory.is_dir():
        raise SelectionError("missing or linked Windows155 additions directory")
    entries = additions["patches"]
    if {path.name for path in directory.iterdir()} != {Path(entry["path"]).name for entry in entries}:
        raise SelectionError("partial or unexpected Windows155 additions inventory")
    for entry in entries:
        if digest(read(repo, entry["path"])) != entry["sha256"]:
            raise SelectionError("changed Windows155 addition: " + entry["path"])
    return [entry["path"] for entry in entries], {
        "schema_version": additions["schema_version"], "kind": additions["kind"],
        "manifest": {"path": WINDOWS155_ADDITIONS_MANIFEST, "sha256": digest(raw)},
        "patches": entries,
    }


def select(repo: Path, platform: str | None = None, *, src: Path | None = None,
           core: Path | None = None) -> tuple[dict, list[tuple[str, bytes]]]:
    """Return raw selected-byte identity and ordered (physical path, bytes)."""
    repo = Path(repo)
    if platform is not None and platform not in PLATFORMS:
        raise SelectionError("unsupported patch platform: " + platform)
    series = read(repo, "patches/series")
    names = [line.split("#", 1)[0].strip() for line in series.decode("utf-8").splitlines()]
    names = [name for name in names if name]
    if not names or len(names) != len(set(names)):
        raise SelectionError("empty or duplicate patch series")
    if any(not re.fullmatch(r"patches/[^/]+\.patch", name) for name in names):
        raise SelectionError("unsafe series: must name root patch files only")
    pinfile = repo / "build/ungoogled-revisions.psd1"
    pins = None
    if pinfile.exists() or pinfile.is_symlink():
        read(repo, "build/ungoogled-revisions.psd1")
        read(repo, "CHROMIUM_VERSION")
        if platform:
            filename = "CHROMIUM_" + PLATFORMS[platform].upper() + "_VERSION"
            if (repo / filename).exists() or (repo / filename).is_symlink():
                read(repo, filename)
        try:
            pins = load_pins(repo, platform) if platform else load_shared_pins(repo)
        except (OSError, ValueError) as exc:
            raise SelectionError(str(exc)) from exc
    elif any((repo / name).exists() or (repo / name).is_symlink() for name in
             ("CHROMIUM_VERSION", "CHROMIUM_LINUX_VERSION", "CHROMIUM_WINDOWS_VERSION",
              "CHROMIUM_MACOS_VERSION", OVERRIDE_ROOT, MACOS152_OVERRIDE_ROOT,
              OVERRIDE155_ROOT, LINUX155_ADDITIONS_ROOT, LINUX155_ADDITIONS_MANIFEST,
              MACOS154_ADDITIONS_ROOT, MACOS154_ADDITIONS_MANIFEST,
              MACOS155_ADDITIONS_ROOT, MACOS155_ADDITIONS_MANIFEST,
              WINDOWS155_ADDITIONS_ROOT, WINDOWS155_ADDITIONS_MANIFEST)):
        raise SelectionError("patch version selection requires complete repository pins")
    version = pins["ChromiumVersion"] if pins else None
    macos152 = platform == "macos" and version == MACOS152_VERSION
    if src is not None and version is not None:
        source_version_path = Path(src) / "chrome/VERSION"
        if version in (VERSION, VERSION155) or macos152 or source_version_path.is_symlink():
            observed = source_version(Path(src))
            if observed != version:
                raise SelectionError("source version does not match selected repository pins")
    if core is not None and version is not None:
        core_version_path = Path(core) / "chromium_version.txt"
        if version in (VERSION, VERSION155) or macos152 or core_version_path.is_symlink():
            observed = read(Path(core), "chromium_version.txt").decode("ascii").strip()
            if observed != version:
                raise SelectionError("core version does not match selected repository pins")
    selected = names
    selection = None
    if version == VERSION:
        if platform not in PLATFORM_COMMITS:
            raise SelectionError("Chromium 154 overrides require Windows, Linux, or macOS")
        prefix = "Ungoogled" + PLATFORMS[platform]
        core_commit = MACOS154_CORE if platform == "macos" else CORE
        platform_version = VERSION + ("-1.1" if platform in ("windows", "macos") else "-1")
        if (pins["UngoogledCommit"] != core_commit or pins["UngoogledVersion"] != VERSION + "-1"
                or pins[prefix + "Commit"] != PLATFORM_COMMITS[platform]
                or pins[prefix + "Version"] != platform_version):
            raise SelectionError("Chromium 154 core/platform pins do not match reviewed overrides")
        validate_overrides(repo)
        if len(names) != 224 or not {"patches/" + name for name in OVERRIDES}.issubset(names):
            raise SelectionError("Chromium 154 requires the complete 224-patch base series")
        selected = [OVERRIDE_ROOT + "/" + Path(name).name if Path(name).name in OVERRIDES else name
                    for name in names]
        selection = {"schema_version": 1, "version": VERSION, "platform": platform,
                     "core_commit": core_commit, "platform_commit": PLATFORM_COMMITS[platform],
                     "base_series_sha256": digest(series)}
    elif version == VERSION155:
        if platform not in PLATFORM155_COMMITS:
            raise SelectionError("Chromium 155 overrides require Windows, Linux, or macOS")
        prefix = "Ungoogled" + PLATFORMS[platform]
        platform_version = VERSION155 + ("-1.1" if platform in ("windows", "macos") else "-1")
        if (pins["UngoogledCommit"] != CORE155
                or pins["UngoogledVersion"] != VERSION155 + "-1"
                or pins[prefix + "Commit"] != PLATFORM155_COMMITS[platform]
                or pins[prefix + "Version"] != platform_version):
            raise SelectionError("Chromium 155 core/platform pins do not match reviewed overrides")
        validate_overrides(repo, OVERRIDE155_ROOT, OVERRIDES155, "155")
        if len(names) != 224 or not {"patches/" + name for name in OVERRIDES155}.issubset(names):
            raise SelectionError("Chromium 155 requires the complete 224-patch base series")
        selected = [OVERRIDE155_ROOT + "/" + Path(name).name
                    if Path(name).name in OVERRIDES155 else name for name in names]
        selection = {"schema_version": 1, "version": VERSION155, "platform": platform,
                     "core_commit": CORE155, "platform_commit": PLATFORM155_COMMITS[platform],
                     "base_series_sha256": digest(series)}
    elif macos152:
        if (pins["UngoogledCommit"] != MACOS152_CORE
                or pins["UngoogledVersion"] != MACOS152_VERSION + "-1"
                or pins["UngoogledMacOSCommit"] != MACOS152_PLATFORM
                or pins["UngoogledMacOSVersion"] != MACOS152_VERSION + "-1.1"):
            raise SelectionError("Chromium 152 macOS core/platform pins do not match reviewed overrides")
        validate_overrides(repo, MACOS152_OVERRIDE_ROOT, MACOS152_OVERRIDES, "152 macOS")
        if len(names) != 224 or not {"patches/" + name for name in MACOS152_OVERRIDES}.issubset(names):
            raise SelectionError("Chromium 152 macOS requires the complete 224-patch base series")
        selected = [MACOS152_OVERRIDE_ROOT + "/" + Path(name).name
                    if Path(name).name in MACOS152_OVERRIDES else name for name in names]
        selection = {"schema_version": 1, "version": MACOS152_VERSION, "platform": platform,
                     "core_commit": MACOS152_CORE, "platform_commit": MACOS152_PLATFORM,
                     "base_series_sha256": digest(series)}
    elif version is not None and version not in LEGACY_VERSIONS:
        raise SelectionError("unsupported Chromium patch version: " + version)
    effective_series = series
    if selection:
        mapping = dict(zip(names, selected, strict=True))
        effective_series = "".join(
            line.replace(name, mapping[name], 1) if (name := line.split("#", 1)[0].strip()) else line
            for line in series.decode("utf-8").splitlines(keepends=True)).encode("utf-8")
    if version == VERSION155 and platform == "linux":
        additions, addition_identity = linux155_additions(repo)
        if addition_identity is not None:
            selected = [*selected, *additions]
            if not effective_series.endswith(b"\n"):
                effective_series += b"\n"
            effective_series += "".join(name + "\n" for name in additions).encode("utf-8")
            selection["additions"] = addition_identity
    if version == VERSION and platform == "macos":
        additions, addition_identity = macos154_additions(repo)
        if addition_identity is not None:
            selected = [*selected, *additions]
            if not effective_series.endswith(b"\n"):
                effective_series += b"\n"
            effective_series += "".join(name + "\n" for name in additions).encode("utf-8")
            selection["additions"] = addition_identity
    if version == VERSION155 and platform == "macos":
        additions, addition_identity = macos155_additions(repo)
        if addition_identity is not None:
            selected = [*selected, *additions]
            if not effective_series.endswith(b"\n"):
                effective_series += b"\n"
            effective_series += "".join(name + "\n" for name in additions).encode("utf-8")
            selection["additions"] = addition_identity
    if version == VERSION155 and platform == "windows":
        additions, addition_identity = windows155_additions(repo)
        if addition_identity is not None:
            selected = [*selected, *additions]
            if not effective_series.endswith(b"\n"):
                effective_series += b"\n"
            effective_series += "".join(name + "\n" for name in additions).encode("utf-8")
            selection["additions"] = addition_identity
    patches = [(name, read(repo, name)) for name in selected]
    identity = {"series_sha256": digest(effective_series),
                "patches": [{"path": name, "sha256": digest(raw)} for name, raw in patches]}
    if selection:
        identity["selection"] = selection
    return identity, patches


def preparation_key(repo: Path, platform: str, style: str) -> str:
    """Retain historical key encodings; hash selected bytes for reviewed overrides."""
    identity, patches = select(repo, platform)
    hasher = hashlib.sha256()
    if style == "posix":
        for name in ("build/prepare-ungoogled.sh", "build/apply-patches.sh", "patches/series"):
            hasher.update(name.encode())
            hasher.update(read(repo, name))
    elif style != "windows":
        raise SelectionError("unsupported preparation key style")
    if "selection" in identity:
        hasher.update(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())
    for name, raw in patches:
        if style == "posix":
            hasher.update(name.encode())
        hasher.update(raw)
    payload = "build/windows/lite-tarball-files"
    root = repo / payload
    if root.is_symlink():
        raise SelectionError("symlink lite payload")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SelectionError("symlink lite payload")
        if path.is_file():
            name = path.relative_to(repo).as_posix()
            hasher.update((name if style == "posix" else path.relative_to(root).as_posix()).encode())
            hasher.update(read(repo, name))
    return hasher.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--platform", choices=tuple(PLATFORMS), required=True)
    parser.add_argument("--src", type=Path)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--paths", action="store_true")
    output.add_argument("--json", action="store_true")
    output.add_argument("--key", choices=("windows", "posix"))
    args = parser.parse_args(argv)
    try:
        identity, patches = select(args.repo, args.platform, src=args.src)
        if args.key:
            print(preparation_key(args.repo, args.platform, args.key))
        elif args.paths:
            print("\n".join(name for name, _ in patches))
        else:
            print(json.dumps(identity, sort_keys=True))
    except (OSError, ValueError) as exc:
        print("patch selection: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
