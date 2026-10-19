#!/usr/bin/env python3
"""Pinned macOS DevTools generators, without Homebrew or npm lifecycle scripts."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile

try:
    from . import linux_restored_generators as safe
except ImportError:
    import linux_restored_generators as safe

PINS = ("154.0.8037.97", "3e46b13825f808f0886e484d44532372655e5fe4",
        "f7ba75f94442abda7ac3ea81790c217f8636d3ba")
PINS155 = ("155.0.8059.39", "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a",
           "b1238bb72b02cd36219991d9029c1c65183ea295")
DEVTOOLS = "third_party/devtools-frontend/src"
PACKAGES = DEVTOOLS + "/third_party/chromix-macos/node_modules"
# npm integrity plus SHA256 of the archive and sorted JSON [path, mode, SHA256] file inventory.
ARCHIVES = {'esbuild': {'files': 7,
             'integrity': 'sha512-HKVLS8dvII+xoKW9kmqxbRKrnWEXfJJr/FZhhJmiqIB0e053QNYFqOBouTMO/k5sID4MvCiUCvv8b9M4h32wIA==',
             'package': 'esbuild',
             'sha256': 'e045f94c235c7adc50e77ba2a579c7bec41b496b6b47c3a7845f7c1e19959a88',
             'tree_sha256': '4765ec3c0ce5b2758ac05e5b1327da59c68b0cfb48933c463e00e9108a0acfb2',
             'url': 'https://registry.npmjs.org/esbuild/-/esbuild-0.28.2.tgz',
             'version': '0.28.2'},
 'esbuild-arm64': {'files': 3,
                   'integrity': 'sha512-n4KqkOQrraxHJcgjM1RvwbigfQKIKJVpM7xp+KsxiyUSrRdIXnt73VhrPAx0fV44hgfmIVKjxMN9J1t5jySVkw==',
                   'package': '@esbuild/darwin-arm64',
                   'sha256': '1980cde09749094452b20d36ff267585ccb3f72749c7bc97291cd9996ccf5a2a',
                   'tree_sha256': '0bddd4c1a937774d0becd25f0dd43aac8db74f9057744404fcab8ad04fc384f4',
                   'url': 'https://registry.npmjs.org/@esbuild/darwin-arm64/-/darwin-arm64-0.28.2.tgz',
                   'version': '0.28.2'},
 'esbuild-x64': {'files': 3,
                 'integrity': 'sha512-uq6suIWYP37qzGddBKPw5QEQPi6HiLGsO7UmkpfyaYNQ3D+rN6w6WfwH+nuqcGXWvawGwxOEroO4YGnFh95azw==',
                 'package': '@esbuild/darwin-x64',
                 'sha256': 'abb6a7a895aaf2cc0df36fecc3bf0042479ffec51a643a056d54e9109f27db55',
                 'tree_sha256': 'b0fbdc6c8830008e37b33a00f9aeb58c938ad16279953d573b6e7b6f12746bc4',
                 'url': 'https://registry.npmjs.org/@esbuild/darwin-x64/-/darwin-x64-0.28.2.tgz',
                 'version': '0.28.2'},
 'typescript': {'files': 416,
                'integrity': 'sha512-8FYau96o3NKOhbjKi/qNvG/W5jhzxkbdm5sj9AbZ/5T5sWqn3hJgLfGx27sRKZWTvyzCP8dLRBTf5tBTSRVUNA==',
                'package': 'typescript',
                'sha256': 'da2513f4b95176d6dde8b51aab7afe8a927656c9d277369793f77f7e59371c08',
                'tree_sha256': 'c86d9c51109df1bbd14409548f735869223f0f48f5f93e6e12f4cf131432f86f',
                'url': 'https://registry.npmjs.org/typescript/-/typescript-7.0.2.tgz',
                'version': '7.0.2'},
 'typescript-arm64': {'files': 113,
                      'integrity': 'sha512-gowzar9MwS/aRWp6f3a4KUqzRjAZjOsmGNCM6LcTgXum+dBfgsBVMN+AgvOCCbguXyick6LJhpBszxMebJ8syA==',
                      'package': '@typescript/typescript-darwin-arm64',
                      'sha256': '902e2fe1cf0799198ef902c6b8c310a450fef629a6baba41d45641ef75c04ebd',
                      'tree_sha256': 'ce34bcb892cc048b4f58f0b40c4cdca4bea371ff17594a093f8a7e5b96247c89',
                      'url': 'https://registry.npmjs.org/@typescript/typescript-darwin-arm64/-/typescript-darwin-arm64-7.0.2.tgz',
                      'version': '7.0.2'},
 'typescript-x64': {'files': 113,
                    'integrity': 'sha512-SZ9xZInqApNlNGc9s0W1VSsktYSOe9cFqNOIqmN1Gs8SmkjKZYFt017G4VwPxASInODuAdbTW7sXiFUf893RgA==',
                    'package': '@typescript/typescript-darwin-x64',
                    'sha256': 'eba158cb54050f723d5ff781438f33de5640054440bb4f2bd170cfe9bc2eb551',
                    'tree_sha256': '1b6e518e2f065b3b8703587f2f8cc03486e2a664f8434a2998819708ae1beeb5',
                    'url': 'https://registry.npmjs.org/@typescript/typescript-darwin-x64/-/typescript-darwin-x64-7.0.2.tgz',
                    'version': '7.0.2'}}

# DevTools 66df492aaa0129d090937e933dd44c5389ab24d2 + core/mac series + domain substitution.
SOURCE_REPAIRS = {'scripts/build/esbuild.js': ('f7d01510b0e62357f04d74e5e80bbe4273519ac42afc982b592ed6a57e01aa66',
                              '91db163b698163aa07219558736886dad716ed3fc8d9765ab799b9b9c4f14323',
                              ((b"    '/',\n    'opt',\n    'homebrew',\n    'bin',",
                                b"    devtoolsRootPath(),\n    'third_party',\n    'chromix-macos',\n    'nod"
                                b"e_modules',\n    '@esbuild',\n    `darwin-${process.arch}`,\n    'bin',"),
                               (b"await import('esbuild')",
                                b"await import('../../third_party/chromix-macos/node_modules/esbuild/lib/m"
                                b"ain.js')"))),
 'scripts/build/ninja/bundle.gni': ('291181451737537d4d3906522b18d0cff9534f3f8d1dff0477386ce4eaa3a185',
                                    'd29cb030a8463cf2cd2d67a393ff996dff1f2474fb3bfdfda229af5c0c75c4d2',
                                    ((b'"/opt/homebrew/bin/esbuild"',
                                      b'devtools_location_prepend + "third_party/chromix-macos/node_modules/'
                                      b'@esbuild/darwin-${host_cpu}/bin/esbuild"'),)),
 'scripts/build/typescript/typescript.gni': ('adf7c81d55b97d5a4c7075e13c033a88fc2f490fce58c805eafddfa8c8b47816',
                                             'cad43cc0a41c4e5071e6baf77e60a311578542d9bb31b9743922a9b59e12f591',
                                             ((b'"/opt/homebrew/bin/esbuild"',
                                               b'devtools_location_prepend + "third_party/chromix-macos/node_'
                                               b'modules/@esbuild/darwin-${host_cpu}/bin/esbuild"'),)),
 'scripts/build/typescript/typescript_vars.gni': ('b1d68f6a5250df31cb003bf7190313b1def7405c2cd2dfef59df7e4f20a95123',
                                                  '13e10861231da59b7cd0dedda7095968aee05c632f1ca82684efa06b9d841874',
                                                  ((b'"/opt/homebrew/bin/tsc"',
                                                    b'devtools_location_prepend + "third_party/chromix-mac'
                                                    b'os/node_modules/@typescript/typescript-darwin-${host_cpu}/lib/tsc"'),)),
 'scripts/devtools_paths.py': ('3caacb6dec4dce6b4d5d90f3596ef8fdfdd2d310c10bb0763f7b24f324bf2cfb',
                               'e6baab561fc2e6162a58620e75f40607f822a21285de0c1fb0df24a10e249a27',
                               ((b"return '/opt/homebrew/bin/esbuild'",
                                 b"return path.join(devtools_root_path(), 'third_party', 'chromix-macos',\n "
                                 b"                    'node_modules', '@esbuild', 'darwin-' +\n            "
                                 b"         ('arm64' if platform.machine() == 'arm64' else 'x"
                                 b"64'),\n                     'bin', 'esbuild')"),)),
 'third_party/typescript/typescript.py': ('10f70e5ec0770a757e1aab9679ad0e0cb19516c832a4271922bcca38db5f93d5',
                                          '816346e33d16e17e5a9313bfd2043707c19db6b55704742a48c40d17403d5b75',
                                          ((b'def GetBinaryPath():\n    return "/opt/homebrew/bin/tsc"\n    '
                                            b"if platform.machine() == 'arm64':\n        darwin_path = 'mac"
                                            b"-arm64'\n    else:\n        darwin_path = 'mac-amd64'\n\n   "
                                            b" relative = {\n        'Darwin': (darwin_path, 'src', 'lib', "
                                            b"'tsc'),\n        'Linux': ('linux-amd64', 'src', 'lib', 'tsc'"
                                            b"),\n        'Windows': ('windows-amd64', 'src', 'lib', 'tsc.e"
                                            b"xe'),\n    }[platform.system()]\n\n    devtools_checkout_path ="
                                            b' os_path.join(os_path.dirname(__file__), *relative)\n    if o'
                                            b's_path.exists(devtools_checkout_path):\n        return os_pat'
                                            b'h.normpath(devtools_checkout_path)\n\n    # Assume we are in a'
                                            b' chromium checkout.\n    return os_path.normpath(\n        os_'
                                            b"path.join(os_path.dirname(__file__), '..', '..', '..', '..',"
                                            b"\n                     'typescript', *relative))\n\n\n",
                                            b'def GetBinaryPath():\n    return os_path.normpath(os_path.joi'
                                            b"n(os_path.dirname(__file__), '..',\n                         "
                                            b"               'chromix-macos', 'node_modules', 'typescript'"
                                            b", 'bin', 'tsc'))\n\n\ndef GetNodePath():\n    node = ('mac_a"
                                            b"rm64', 'node-darwin-arm64') if platform.machine() == 'arm64'"
                                            b" else ('mac', 'node-darwin-x64')\n    return os_path.normpath"
                                            b"(os_path.join(os_path.dirname(__file__), '..', '..', '..', '"
                                            b"..',\n                                        'node', *node, "
                                            b"'bin', 'node'))\n\n\n"),
                                           (b'cmd = [GetBinaryPath()] + cmd_parts',
                                            b'cmd = [GetNodePath(), GetBinaryPath()] + cmd_parts')))}


# DevTools cd7bfad9bba968ac23ba67e538f9e1d499991d25 + core/mac series + domain substitution.
SOURCE155_HASHES = {'scripts/build/esbuild.js': ('c10b89b1e642911227984b1cf60a731a1ca0bfac5813b5a33bfcc55e67c8b40d',
                              'af9ad24f819a4f92db714eaf768a1584806b9ede35e596fab48aec1c5617a5ed'),
 'scripts/build/ninja/bundle.gni': ('b5aabea6c7deccd38a2c05fcc317be8903c0d791915b3ca2993763c5524aa96c',
                                    'e5f1a574effb1fd3ceae6eba3a097b830c41ae6198ef006dcea8f242680e75ef'),
 'scripts/build/typescript/typescript.gni': ('b9b10d5452d78217851fd404dd48775e38a7a0479f7b937cc626a5de347bcf3f',
                                             'b9b10d5452d78217851fd404dd48775e38a7a0479f7b937cc626a5de347bcf3f'),
 'scripts/build/typescript/typescript_vars.gni': ('94f495ee819bca553a093a0a03ba3d4c1a558156e008598e01d665ef76d914e8',
                                                  '155352f9a9014616bdefefbc2ee1a44f0e9a82c45357113e6db6c2db60c67ba2'),
 'scripts/devtools_paths.py': ('45ff53ad956151b62fa3b8695c95cf66c471fdd1f2d01b2e98b96b7914d6ba45',
                               '37081ce1f0222710902807cc8c3e4eacfd4df147c649dd6049e5aa24f2e343e1'),
 'third_party/typescript/typescript.py': ('10f70e5ec0770a757e1aab9679ad0e0cb19516c832a4271922bcca38db5f93d5',
                                          '816346e33d16e17e5a9313bfd2043707c19db6b55704742a48c40d17403d5b75')}
SOURCE155_REPAIRS = {
    name: (*hashes, () if name == "scripts/build/typescript/typescript.gni"
           else SOURCE_REPAIRS[name][2])
    for name, hashes in SOURCE155_HASHES.items()
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_digest(pins=PINS) -> str:
    repairs = source_repairs(pins)
    value = {"pins": pins, "archives": ARCHIVES,
             "sources": {name: entry[:2] for name, entry in repairs.items()}}
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def source_repairs(pins):
    if pins == PINS:
        return SOURCE_REPAIRS
    if pins == PINS155:
        return SOURCE155_REPAIRS
    raise ValueError("macOS generators require exact reviewed pins")


def check_pins(repo: Path) -> tuple:
    try:
        from .platform_pins import load_pins
    except ImportError:
        from platform_pins import load_pins
    pins = load_pins(repo, "macos")
    identity = tuple(pins[key] for key in ("ChromiumVersion", "UngoogledCommit", "UngoogledMacOSCommit"))
    source_repairs(identity)
    return identity


def package_keys(host_arch: str) -> tuple[str, ...]:
    if host_arch not in ("x64", "arm64"):
        raise ValueError("unsupported macOS generator host")
    return "esbuild", "typescript", "esbuild-" + host_arch, "typescript-" + host_arch


def package_path(key: str) -> str:
    return PACKAGES + "/" + ARCHIVES[key]["package"]


def tree_digest(records: list) -> str:
    return sha256(json.dumps(sorted(records), separators=(",", ":")).encode())


def archive_files(data: bytes, key: str) -> dict[str, tuple[bytes, int]]:
    entry = ARCHIVES[key]
    if (len(data) > 32 * 1024**2 or sha256(data) != entry["sha256"]
            or "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode() != entry["integrity"]):
        raise ValueError("macOS generator archive integrity mismatch")
    files = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            name = member.name.removeprefix("package/")
            path = PurePosixPath(name)
            if (not member.name.startswith("package/") or not name or path.is_absolute()
                    or path.as_posix() != name or any(p in (".", "..") for p in path.parts)
                    or "\\" in name or "\0" in name or not member.isfile()
                    or member.mode not in (0o644, 0o755) or name in files
                    or member.size <= 0 or len(files) >= 1000):
                raise ValueError("unsafe macOS generator archive member")
            total += member.size
            if total > 64 * 1024**2:
                raise ValueError("oversized macOS generator package")
            payload = archive.extractfile(member).read()
            if len(payload) != member.size:
                raise ValueError("truncated macOS generator package")
            files[name] = payload, member.mode
    records = [[name, mode, sha256(data)] for name, (data, mode) in files.items()]
    if len(records) != entry["files"] or tree_digest(records) != entry["tree_sha256"]:
        raise ValueError("unknown macOS generator archive inventory")
    metadata = json.loads(files["package.json"][0])
    if (metadata.get("name"), metadata.get("version")) != (entry["package"], entry["version"]):
        raise ValueError("macOS generator package metadata mismatch")
    return files


def package_identity(src: Path, key: str, *, relative: str | None = None) -> dict | None:
    relative = package_path(key) if relative is None else relative
    root = safe.safe_path(src, relative)
    if not root.exists():
        return None
    if not root.is_dir():
        raise ValueError("invalid macOS generator package directory")
    records, directories = [], set()
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        for name in dirs:
            path = safe.safe_path(src, (Path(directory) / name).relative_to(src).as_posix())
            directories.add(path.relative_to(root).as_posix())
        for name in files:
            path = Path(directory) / name
            rel = path.relative_to(src).as_posix()
            safe.regular(src, rel)
            data, metadata = safe._read_verified_with_metadata(src, rel)
            records.append([path.relative_to(root).as_posix(), stat.S_IMODE(metadata[2]), sha256(data)])
    expected_dirs = {p.as_posix() for name, _, _ in records for p in PurePosixPath(name).parents
                     if p.as_posix() != "."}
    entry = ARCHIVES[key]
    if (directories != expected_dirs or len(records) != entry["files"]
            or tree_digest(records) != entry["tree_sha256"]):
        raise ValueError(f"unknown macOS generator package bytes/inventory: {key}")
    return {"path": relative, "tree_sha256": entry["tree_sha256"],
            "archive_sha256": entry["sha256"], "version": entry["version"]}


def _rename(parent: int, old: str, new: str, *, exchange=False) -> None:
    if sys.platform != "darwin":
        safe._rename_at(parent, old, new, safe._RENAME_EXCHANGE if exchange else safe._RENAME_NOREPLACE)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameatx_np
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # Darwin RENAME_SWAP and RENAME_EXCL are not the Linux renameat2 flag values.
    if rename(parent, old.encode(), parent, new.encode(), 2 if exchange else 4):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def atomic_replace(src: Path, relative: str, expected: bytes, replacement: bytes,
                   *, expected_metadata=None, before_publish=None) -> None:
    safe.regular(src, relative)
    parent, name, ancestors = safe._relative_parent_fd(src, relative)
    temporary = ".chromix-mac154-" + secrets.token_hex(12)
    swapped = False
    try:
        chain = safe._directory_chain(parent, ancestors)
        original, metadata = safe._path_payload(parent, name)
        if original != expected or expected_metadata is not None and metadata != expected_metadata:
            raise ValueError("macOS generator source changed before repair")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            safe._write_fd(fd, replacement, stat.S_IMODE(metadata[2]))
            staged = safe._metadata(fd)
        finally:
            os.close(fd)
        if before_publish:
            before_publish(src / relative)
        safe._assert_directory_chain(src, relative, chain)
        if safe._path_payload(parent, name) != (expected, metadata):
            raise ValueError("macOS generator source changed before repair")
        _rename(parent, temporary, name, exchange=True)
        swapped = True
        if (safe._path_payload(parent, temporary) != (expected, metadata)
                or safe._path_payload(parent, name) != (replacement, staged)):
            raise ValueError("macOS generator source changed during repair")
        safe._assert_directory_chain(src, relative, chain)
        os.fsync(parent)
        swapped = False
    finally:
        try:
            if swapped:
                _rename(parent, temporary, name, exchange=True)
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent)
            safe._close_all(ancestors)


def install_package(src: Path, key: str, files: dict, *, before_publish=None) -> None:
    if package_identity(src, key) is not None:
        return
    relative = package_path(key)
    parent_relative, name = relative.rsplit("/", 1)
    parent, ancestors = safe._ensure_directory_fd(src, parent_relative)
    temporary = ".chromix-mac154-" + secrets.token_hex(12)
    try:
        chain = safe._directory_chain(parent, ancestors)
        os.mkdir(temporary, 0o700, dir_fd=parent)
        staged_relative = parent_relative + "/" + temporary
        for filename, (data, mode) in files.items():
            container = staged_relative + "/" + filename.rsplit("/", 1)[0] if "/" in filename else staged_relative
            directory, opened = safe._ensure_directory_fd(src, container)
            try:
                fd = os.open(filename.rsplit("/", 1)[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                try:
                    safe._write_fd(fd, data, mode)
                finally:
                    os.close(fd)
            finally:
                os.close(directory)
                safe._close_all(opened)
        if before_publish:
            before_publish(src / relative)
        safe._assert_directory_chain(src, relative, chain)
        package_identity(src, key, relative=staged_relative)
        _rename(parent, temporary, name)
        os.fsync(parent)
    finally:
        # The private staging directory may remain after failure; never traverse a replaced parent to clean it.
        os.close(parent)
        safe._close_all(ancestors)
    package_identity(src, key)


def source_plan(src: Path, pins=PINS) -> tuple[dict, list]:
    sources, changes = {}, []
    for relative, (before, after, replacements) in source_repairs(pins).items():
        name = DEVTOOLS + "/" + relative
        safe.regular(src, name)
        data, metadata = safe._read_verified_with_metadata(src, name)
        digest = sha256(data)
        if digest not in (before, after):
            raise ValueError(f"unknown macOS generator source: {name}")
        sources[name] = {"sha256": digest, "original_sha256": before, "repaired_sha256": after}
        if digest == before and before != after:
            repaired = data
            for old, new in replacements:
                if old not in repaired:
                    raise ValueError("missing exact macOS generator repair")
                repaired = repaired.replace(old, new)
            if sha256(repaired) != after:
                raise ValueError("macOS repaired source digest mismatch")
            changes.append((name, data, repaired, metadata))
    return sources, changes


def native_paths(host_arch: str) -> dict[str, str]:
    package_keys(host_arch)
    node = "mac_arm64/node-darwin-arm64" if host_arch == "arm64" else "mac/node-darwin-x64"
    return {"node": "third_party/node/" + node + "/bin/node",
            "esbuild": package_path("esbuild-" + host_arch) + "/bin/esbuild",
            "tsc": package_path("typescript-" + host_arch) + "/lib/tsc"}


def smoke(src: Path, host_arch: str) -> dict:
    try:
        from .prepare_restored_build import binary_architectures, host_identity
    except ImportError:
        from prepare_restored_build import binary_architectures, host_identity
    if host_identity() != ("macos", host_arch):
        raise ValueError("macOS generator probes require the matching native host")
    paths = native_paths(host_arch)
    identities = {}
    for name, relative in paths.items():
        path = safe.regular(src, relative)
        if host_arch not in binary_architectures(path, "macos") or not os.access(path, os.X_OK):
            raise ValueError(f"macOS generator native header/mode mismatch: {name}")
        identities[name] = {"path": relative, "sha256": sha256(safe.read_verified(src, relative))}
    env = {key: value for key, value in os.environ.items()
           if key not in ("NODE_OPTIONS", "NODE_PATH", "ESBUILD_BINARY_PATH") and not key.startswith("DYLD_")}
    env.update(ESBUILD_BINARY_PATH=str(src / paths["esbuild"]), PATH="")
    node = str(src / paths["node"])
    native_tsc = str(src / paths["tsc"])
    tsc = str(src / package_path("typescript") / "bin/tsc")
    def run(command, cwd):
        result = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, check=False, timeout=60)
        if result.returncode:
            raise ValueError(f"macOS generator probe failed: {result.stdout[:2000]}")
        return result.stdout.strip()
    versions = {"esbuild": run([str(src / paths["esbuild"]), "--version"], src),
                "typescript": run([node, tsc, "--version"], src),
                "typescript_native": run([native_tsc, "--version"], src)}
    if versions != {"esbuild": "0.28.2", "typescript": "Version 7.0.2", "typescript_native": "Version 7.0.2"}:
        raise ValueError("unexpected macOS generator version")
    identities["node"]["version"] = run([node, "--version"], src)
    identities["esbuild"]["version"] = versions["esbuild"]
    identities["tsc"]["version"] = versions["typescript_native"]
    with tempfile.TemporaryDirectory(prefix="chromix-mac154-smoke-") as temporary:
        root = Path(temporary).resolve()
        (root / "sample.ts").write_text('const answer: number = 42; console.log(answer);\n')
        run([node, tsc, "sample.ts", "--outDir", "compiled", "--target", "es2023"], root)
        if run([node, str(root / "compiled/sample.js")], root) != "42":
            raise ValueError("TypeScript Node launcher compilation probe mismatch")
        run([native_tsc, "sample.ts", "--outDir", "native-compiled", "--target", "es2023"], root)
        if run([node, str(root / "native-compiled/sample.js")], root) != "42":
            raise ValueError("TypeScript direct native compilation probe mismatch")
        module = str(src / package_path("esbuild") / "lib/main.js")
        script = "require(process.argv[1]).buildSync({entryPoints:['sample.ts'],bundle:true,outfile:'bundle.js'});"
        run([node, "-e", script, module], root)
        if run([node, str(root / "bundle.js")], root) != "42":
            raise ValueError("esbuild native bundle probe mismatch")
    for name, entry in identities.items():
        if sha256(safe.read_verified(src, entry["path"])) != entry["sha256"]:
            raise ValueError(f"macOS generator tool changed during probes: {name}")
    return dict(versions, typescript_compile=True, typescript_native_compile=True,
                path_search_disabled=True, esbuild_bundle=True, tools=identities)


def prepare(src: Path, *, host_arch: str, repo: Path, repair=False, before_publish=None) -> dict:
    pins = check_pins(repo)
    keys = package_keys(host_arch)
    sources, changes = source_plan(src, pins)
    packages = {key: package_identity(src, key) for key in keys}
    missing = any(value is None for value in packages.values())
    result = {"manifest_sha256": manifest_digest(pins), "pins": list(pins), "host_arch": host_arch,
              "sources": sources, "packages": packages, "install_needed": missing,
              "repair_needed": missing or bool(changes)}
    if not repair:
        return result
    if missing:
        raise ValueError("pinned macOS generator packages must be installed before finish")
    probe = smoke(src, host_arch)
    for name, original, repaired, metadata in changes:
        atomic_replace(src, name, original, repaired, expected_metadata=metadata, before_publish=before_publish)
    sources, changes = source_plan(src, pins)
    if changes or {key: package_identity(src, key) for key in keys} != packages:
        raise ValueError("macOS generator inputs changed during preparation")
    result = dict(result, sources=sources, repair_needed=False)
    probe = dict(probe, schema_version=1, host_arch=host_arch,
                 manifest_sha256=result["manifest_sha256"],
                 generator_fingerprint_sha256=sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()))
    return dict(result, probe=probe)
