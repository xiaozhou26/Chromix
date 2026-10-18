#!/usr/bin/env python3
"""Exact versioned Linux restored DevTools identities and local esbuild provisioning."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile
import tempfile
import ctypes
import errno
import secrets

PINS = ("154.0.8037.97", "37085e47cf580c815a30402917d350ce97399ded",
        "f1441a6efb4d79427d1a3180b1877bbbdb1af7aa")
DEVTOOLS = "third_party/devtools-frontend/src"
TYPESCRIPT_PORTABLE = "99034dad55a0805c052f928fca1f03c335a59a4e61bc3ad4c183bb3c0ff4c471"
TYPESCRIPT_REPAIRED = {
    "x64": "309b0cb8a713af2d493bdf99dcabdc16a1760f4f4c450a5b524496086ef6baac",
    "arm64": "492b26da4772c7971149ae05739372b245f683bccf92334560fa4fa70011c52e",
}
# Sorted os.walk files, hashing JSON [relative path, SHA256] as in prepare_linux_typescript.
TYPESCRIPT_TREE = "2dda99b559c5bbc04a1e5ec68440bf9afc14866593f4d9ca0de6c00556d2fb91"
ESBUILD_VERSION = "0.25.1"
ESBUILD_BINARY = DEVTOOLS + "/third_party/esbuild/chromix-linux/esbuild"
ESBUILD_MODULE = DEVTOOLS + "/node_modules/esbuild"
LOCK = DEVTOOLS + "/package-lock.json"
LOCK_SHA256 = "aae038b752d0944d6c19d455f47f3d08134f60463a2bf84d4196d82036663fd2"
MODULE_FILES = {
    "bin/esbuild": "ac392c0990e6e7f1be8db82a182c596e1d6cfaad66bb254c7db0606ebe04f2fb",
    "install.js": "417cbbaf3ea9cb878011832b70eeaa00ce49419a0589cbbf4b65d258c4d7c1dd",
    "lib/main.js": "5e7dcf28613dbf0bc5558743d6253d38fee888b935dfeb4ef0e702de0a71f075",
    "package.json": "c6dc442df570a4325880f3788e64f2e612629c365cb16145f429261386c793eb",
    "LICENSE.md": "b40ec5baec7bb34fa5b1c09521fa3cd52d5fad7adafed74932a2010d3612a681",
    "README.md": "6d481cd60ec3c679e5e395f547ab4221147f35642894f6b95a8df38fd25c87bd",
    "lib/main.d.ts": "b8caba62c0d2ef625f31cbb4fde09d851251af2551086ccf068611b0a69efd81",
}
ARCHIVES = {
    "module": ("https://registry.npmjs.org/esbuild/-/esbuild-0.25.1.tgz",
               "ed9bf82566fe0e3a5e01317d676bed3c8f241656c0d4bd30f96b5c84fda0f9cb"),
    "x64": ("https://registry.npmjs.org/@esbuild/linux-x64/-/linux-x64-0.25.1.tgz",
            "a3972c20d19545792ba6c6abe564b61c91f66cfa242ff9bffd9e5d59c65452e5"),
    "arm64": ("https://registry.npmjs.org/@esbuild/linux-arm64/-/linux-arm64-0.25.1.tgz",
              "70771c9212585cfd1b190465f92dae98d1d3fc4a4fab5cacbef71457ee08e254"),
}
BINARY_HASHES = {
    "x64": "cfbfcac245e272a19c709cea06cda74d8cdebf3c033ba9f4dc2b7fc5985fd516",
    "arm64": "4d8f932cd7de4422d3b54baa5445a5eabd4afe4152f9f5f59d1b111e72b0d596",
}
GN_OLD = b'_esbuild = "/usr/bin/esbuild"'
GN_NEW = b'_esbuild = devtools_location_prepend + "third_party/esbuild/chromix-linux/esbuild"'
TSC_OLD = b'  tsc_binary = "/usr/bin/tsc"'
TSC_NEW = b'  tsc_binary = devtools_location_prepend + "node_modules/typescript/bin/tsc"'
# DevTools 66df492aaa0129d090937e933dd44c5389ab24d2 plus portablelinux patches.
SOURCE_REPAIRS = {
    "scripts/build/esbuild.js": (
        "c34fa6f77da6a2364a94b3a96ea39471578e33451b75ca8e2bfd695ef04e3e5e",
        "2a51c265eaac9f315f9048629ef5768fb06f716cc35be61c5fc6cd9c4887f780",
        b"    '/',\n    'usr',\n    'bin',\n",
        b"    devtoolsRootPath(),\n    'third_party',\n    'esbuild',\n    'chromix-linux',\n"),
    "scripts/build/ninja/bundle.gni": (
        "50ae4c76e82f385712b6c34468fafe59bbbcb586beae7ea37209c880843110a4",
        "24ada61a481c4617c59045219532b2a769e0b578118fbb8300963a2ea64a8983", GN_OLD, GN_NEW),
    "scripts/build/typescript/typescript.gni": (
        "de82419b6538ed10cb49e362c8025bc21b8301d72d15110237cc4ea5f58dd575",
        "e9b2dc92202ce8e9f6deca5d2c166b10644c8f9bda4a252648228a1f785b3dc2", GN_OLD, GN_NEW),
    "scripts/build/typescript/typescript_vars.gni": (
        "1f37a0ec115a482cad5eb3d21d3addf605cded05e933f00b6627e7b607fef0d5",
        "fcb41c43639bc6df1ee44d2ccc55d7ba6f38ff03da33b91072c59d26c0135472",
        TSC_OLD, TSC_NEW),
    "scripts/build/typescript/ts_library.py": (
        "c16be050b46de839827ca8cf3ec03752ca11ca549a7e3ebf06fe94ceb314103c",
        "64cd3e00db5b66e89f9a4aa37e96f1625fc27b39c1002bd3e84bfb9a2bc3df1e",
        b"ESBUILD_LOCATION = devtools_paths.esbuild_path()",
        b"ESBUILD_LOCATION = path.join(ROOT_DIRECTORY_OF_REPOSITORY, 'third_party',\n"
        b"                             'esbuild', 'chromix-linux', 'esbuild')"),
}


PINS155 = ("155.0.8059.39", "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a",
           "025d99db1673a21992534551598b73ca79360762")
PINS15579 = ("155.0.8059.79", "9f145d8815e6e50cc4a2537fcac40966d911e027",
             "6b86f45adc4a7f7ac4c3897a8b651eb34784d761")
# Rebuilt from cd7bfad9bba968ac23ba67e538f9e1d499991d25, donor patches and core domains.
TYPESCRIPT155_PORTABLE = "99034dad55a0805c052f928fca1f03c335a59a4e61bc3ad4c183bb3c0ff4c471"
TYPESCRIPT155_REPAIRED = {
    "x64": "309b0cb8a713af2d493bdf99dcabdc16a1760f4f4c450a5b524496086ef6baac",
    "arm64": "492b26da4772c7971149ae05739372b245f683bccf92334560fa4fa70011c52e",
}
TYPESCRIPT155_TREE = "2dda99b559c5bbc04a1e5ec68440bf9afc14866593f4d9ca0de6c00556d2fb91"
ESBUILD155_VERSION = "0.28.2"
LOCK155_SHA256 = "0000e82b22813820d5ca00d1658f3d52ceac681f91f84f4812c051fb75ecc4bc"
MODULE155_FILES = {
    "bin/esbuild": "183d6ead2dbc3af51aec767d5395f86fe54fbbc2d2aa8e54feb4e31d76a62f7e",
    "install.js": "612294e278914443bdcf81cb17f54afec34dbdd2ebd999a6ee187912320cc315",
    "lib/main.js": "61ec0ba144ff93f4bf0e96ad6fab6f430ede0aa13e27ace8063ed63ba416b3cd",
    "package.json": "9d0bc453f4e791553c4cc2298ba023b409241fd9801e494741666eb0f6051490",
    "LICENSE.md": "b40ec5baec7bb34fa5b1c09521fa3cd52d5fad7adafed74932a2010d3612a681",
    "README.md": "6d481cd60ec3c679e5e395f547ab4221147f35642894f6b95a8df38fd25c87bd",
    "lib/main.d.ts": "e078310f23f0ccfa28a6ac81b5fad81f27150e921f92ab6e648caab42e2d9a14",
}
ARCHIVES155 = {
    "module": ("https://registry.npmjs.org/esbuild/-/esbuild-0.28.2.tgz",
               "e045f94c235c7adc50e77ba2a579c7bec41b496b6b47c3a7845f7c1e19959a88"),
    "x64": ("https://registry.npmjs.org/@esbuild/linux-x64/-/linux-x64-0.28.2.tgz",
               "9573bb2233aab0f9ea7647d5cca9726113cc1768de61d66b17267f4db84488f6"),
    "arm64": ("https://registry.npmjs.org/@esbuild/linux-arm64/-/linux-arm64-0.28.2.tgz",
               "a96dbfa41d3ef5dbd1ef22b1c10d5187be9267e86093a870f06402a7ec931596"),
}
BINARY155_HASHES = {
    "arm64": "90bd269553d258e80b19e3ccab4f98f0831f87442a2f056510ce24fd1b425fc2",
    "x64": "e1698a3d5c6c0798fee4fd3b5cc816651f460c63d390a7a26ea4beb0b1884100",
}
SOURCE155_REPAIRS = {
    "front_end/ui/legacy/ARIAUtils.ts": (
        "b8bdfdb829af51ba09253de4cea27880a9481871b82ca5d673a7bdad96cacd4d",
        "29e9150b81c4c88e60bd29441182e2d1acc5ddbc349bcbb4dfe0527b7bc484ad",
        b"getOrCreateAnnouncerElement(container: HTMLElement = document.body,",
        b"getOrCreateAnnouncerElement(container: HTMLElement|undefined = document.body,",
    ),
    "front_end/ui/legacy/Widget.ts": (
        "3d9bda48ca918b992bfe56d15f3c12847c5de64d1564291e1c680d08a24fd648",
        "c2d260f5a0b5518824c41bed8e3f34e878985559a15380098570c7183821b78f",
        b"readonly widgetParams?: Partial<WidgetT>)",
        b"readonly widgetParams?: Partial<WidgetT>|undefined)",
    ),
    "scripts/build/esbuild.js": (
        "7441e250f11d08744475e1a816d77c69aa265a9581380a475ff8313c80d63947",
        "05f0876b590b92caf11117f3321fd3af842f8516c125bf5341c08c323ec3eb69",
        b"    '/',\n"
        b"    'usr',\n"
        b"    'bin',\n",
        b'    devtoolsRootPath(),\n'
        b"    'third_party',\n"
        b"    'esbuild',\n"
        b"    'chromix-linux',\n",
    ),
    "scripts/build/ninja/bundle.gni": (
        "470cd49b818adb42066bbd32edf4262fee0537c28e68f813c4f9762f84757e8f",
        "dac1c09e5b97c287a76aa7c863a5e39f47eba164057020b3604bf796aa55dd92",
        b'_esbuild = "/usr/bin/esbuild"',
        b'_esbuild = devtools_location_prepend + "third_party/esbuild/chromix-linux/esbuild"',
    ),
    "scripts/build/typescript/typescript_vars.gni": (
        "aa17ae85e0be06e7c593f1e8ff947b0795565b311b8938753366836085f88572",
        "36650c33dd69e4cab6d51fa25b77170fe9a34a0fa2874afdc5a2b80044354a73",
        (
            b'  tsc_binary = "/usr/bin/tsc"',
            b'# Standard TypeScript library definitions',
            b'typescript_base_inputs = [ tsc_binary ]',
        ),
        (
            b'  tsc_binary = devtools_location_prepend + "node_modules/typescript/lib/tsc.js"',
            b'if (host_os == "linux") {\n'
            b'  tsc_node = "//third_party/node/linux/node-linux-${host_cpu}/bin/node"\n'
            b'  tsc_command = [\n'
            b'    rebase_path(tsc_node, root_build_dir),\n'
            b'    rebase_path(tsc_binary, root_build_dir),\n'
            b'  ]\n'
            b'} else {\n'
            b'  tsc_command = [ rebase_path(tsc_binary, root_build_dir) ]\n'
            b'}\n'
            b'\n'
            b'# Standard TypeScript library definitions',
            b'typescript_base_inputs = [ tsc_binary ]\n'
            b'if (host_os == "linux") {\n'
            b'  typescript_base_inputs += [ tsc_node ]\n'
            b'}',
        ),
    ),
    "scripts/build/typescript/ts_type_check.gni": (
        "bd87baff09d8e2e504fd93f8db58081491bf2c3d3eb5de3a1250ad5b7268a5fb",
        "cfcb7d21806175c969cee489315145c96829e25e79ff9e14bdc2e7f13aa8d208",
        b'      rebase_path(tsc_binary, root_build_dir),',
        b'    ] + tsc_command + [',
    ),
    "scripts/build/typescript/typescript.gni": (
        "b9b10d5452d78217851fd404dd48775e38a7a0479f7b937cc626a5de347bcf3f",
        "aa51f9621055f57f3bc98bfd8268ce98061280088f3c31b2231bbce94c2f081c",
        (
            b'rebase_path(_dts_outputs, root_build_dir) + [ "--" ] + [\n'
            b'          rebase_path(tsc_binary, root_build_dir),',
            b'rebase_path(_stamp_file, root_build_dir),\n'
            b'        "--",\n'
            b'        rebase_path(tsc_binary, root_build_dir),',
            b'rebase_path(_js_outputs, root_build_dir) + [ "--" ] + [\n'
            b'            rebase_path(tsc_binary, root_build_dir),',
        ),
        (
            b'rebase_path(_dts_outputs, root_build_dir) + [ "--" ] + tsc_command + [',
            b'rebase_path(_stamp_file, root_build_dir),\n'
            b'        "--",\n'
            b'      ] + tsc_command + [',
            b'rebase_path(_js_outputs, root_build_dir) + [ "--" ] + tsc_command + [',
        ),
    ),
}


def generator_profile(pins: tuple = PINS) -> dict:
    """Select independent versioned pins; never infer a donor from its files."""
    if type(pins) is not tuple or not all(type(value) is str for value in pins):
        raise ValueError("unverified Linux generator pins")
    if pins == PINS:
        return {"version": ESBUILD_VERSION, "lock": LOCK_SHA256,
                "module": MODULE_FILES, "archives": ARCHIVES, "binaries": BINARY_HASHES,
                "sources": SOURCE_REPAIRS, "typescript_portable": TYPESCRIPT_PORTABLE,
                "typescript_repaired": TYPESCRIPT_REPAIRED, "typescript_tree": TYPESCRIPT_TREE}
    if pins in (PINS155, PINS15579):
        return {"version": ESBUILD155_VERSION, "lock": LOCK155_SHA256,
                "module": MODULE155_FILES, "archives": ARCHIVES155, "binaries": BINARY155_HASHES,
                "sources": SOURCE155_REPAIRS, "typescript_portable": TYPESCRIPT155_PORTABLE,
                "typescript_repaired": TYPESCRIPT155_REPAIRED, "typescript_tree": TYPESCRIPT155_TREE}
    raise ValueError("unverified Linux generator pins")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_AT_FDCWD = -100
_AT_EMPTY_PATH = 0x1000
_AT_SYMLINK_NOFOLLOW = 0x100
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_libc = ctypes.CDLL(None, use_errno=True)
_renameat2 = getattr(_libc, "renameat2", None)
if _renameat2 is not None:
    _renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                           ctypes.c_uint]
    _renameat2.restype = ctypes.c_int


def _raise_errno(operation: str) -> None:
    error = ctypes.get_errno()
    raise OSError(error, f"{operation}: {os.strerror(error)}")


def _rename_at(parent_fd: int, old: str, new: str, flags: int = 0,
               *, destination_fd: int | None = None) -> None:
    destination_fd = parent_fd if destination_fd is None else destination_fd
    if flags and _renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    if flags:
        result = _renameat2(parent_fd, old.encode(), destination_fd, new.encode(), flags)
        if result != 0:
            _raise_errno("renameat")
    else:
        os.rename(old, new, src_dir_fd=parent_fd, dst_dir_fd=destination_fd)


def _open_dir_nofollow(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _open_relative_dir(parent_fd: int, name: str) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                   dir_fd=parent_fd)


def _relative_parent_fd(src: Path, relative: str) -> tuple[int, str, list[int]]:
    parts = PurePosixPath(relative).parts
    if len(parts) < 2:
        return _open_dir_nofollow(src), parts[0], []
    fd = _open_dir_nofollow(src)
    opened = [fd]
    try:
        for part in parts[:-1]:
            child = _open_relative_dir(fd, part)
            opened.append(child)
            fd = child
        return fd, parts[-1], opened[:-1]
    except BaseException:
        for item in reversed(opened):
            os.close(item)
        raise


def _close_all(fds: list[int]) -> None:
    for fd in reversed(fds):
        try:
            os.close(fd)
        except OSError:
            pass


def _metadata(fd: int) -> tuple[int, int, int, int, int, int]:
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns)


def _directory_identity(fd: int) -> tuple[int, int, int]:
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _directory_chain(parent_fd: int, ancestors: list[int]) -> list[tuple[int, int, int]]:
    return [_directory_identity(fd) for fd in (*ancestors, parent_fd)]


def _assert_directory_chain(src: Path, relative: str,
                            expected: list[tuple[int, int, int]]) -> None:
    fresh_parent, _, fresh_ancestors = _relative_parent_fd(src, relative)
    try:
        if _directory_chain(fresh_parent, fresh_ancestors) != expected:
            raise ValueError(f"Linux generator parent directory changed: {relative}")
    finally:
        os.close(fresh_parent)
        _close_all(fresh_ancestors)


def _read_fd(fd: int) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    before = _metadata(fd)
    if not stat.S_ISREG(before[2]) or before[3] != 1 or not before[4]:
        raise ValueError("missing, empty or linked Linux generator input")
    chunks = []
    while True:
        data = os.read(fd, 1024 * 1024)
        if not data:
            break
        chunks.append(data)
    after = _metadata(fd)
    if after != before:
        raise ValueError("Linux generator input changed during read")
    first = b"".join(chunks)
    os.lseek(fd, 0, os.SEEK_SET)
    second_chunks = []
    while True:
        data = os.read(fd, 1024 * 1024)
        if not data:
            break
        second_chunks.append(data)
    if b"".join(second_chunks) != first or _metadata(fd) != before:
        raise ValueError("Linux generator input content changed during read")
    return first, before


def _read_regular_at(parent_fd: int, name: str) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as error:
        raise ValueError(f"linked or unreadable Linux generator input: {name}") from error
    try:
        payload, metadata = _read_fd(fd)
        path_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        current = (path_metadata.st_dev, path_metadata.st_ino, path_metadata.st_mode,
                   path_metadata.st_nlink, path_metadata.st_size, path_metadata.st_mtime_ns)
        if current != metadata:
            raise ValueError(f"Linux generator input changed after read: {name}")
        return payload, metadata
    finally:
        os.close(fd)


def _read_verified_with_metadata(src: Path, relative: str) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    parent_fd, name, ancestors = _relative_parent_fd(src, relative)
    expected_chain = _directory_chain(parent_fd, ancestors)
    try:
        _assert_directory_chain(src, relative, expected_chain)
        payload, metadata = _read_regular_at(parent_fd, name)
        _assert_directory_chain(src, relative, expected_chain)
        return payload, metadata
    finally:
        os.close(parent_fd)
        _close_all(ancestors)


def read_verified(src: Path, relative: str) -> bytes:
    """Read one source file through an anchored O_NOFOLLOW directory walk."""
    return _read_verified_with_metadata(src, relative)[0]


def _fsync_parent(parent_fd: int) -> None:
    os.fsync(parent_fd)


def _path_payload(parent_fd: int, name: str) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        payload, metadata = _read_fd(fd)
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        current = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                   info.st_size, info.st_mtime_ns)
        if metadata != current:
            raise ValueError(f"Linux generator path changed during verification: {name}")
        return payload, metadata
    finally:
        os.close(fd)


def _atomic_replace(src: Path, relative: str, expected: bytes, replacement: bytes,
                    *, expected_metadata=None, before_publish=None) -> None:
    """CAS-publish one regular file with Linux dirfd/O_NOFOLLOW and RENAME_EXCHANGE."""
    parent_fd, name, ancestors = _relative_parent_fd(src, relative)
    temp_name = f".chromix-{secrets.token_hex(12)}"
    temp_fd = None
    exchanged = False
    try:
        expected_chain = _directory_chain(parent_fd, ancestors)
        current_bytes, current = _path_payload(parent_fd, name)
        if (current_bytes != expected or current[3] != 1
                or (expected_metadata is not None and current != expected_metadata)):
            raise ValueError(f"Linux generator output changed before publish: {relative}")
        _assert_directory_chain(src, relative, expected_chain)
        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=parent_fd)
        offset = 0
        while offset < len(replacement):
            offset += os.write(temp_fd, replacement[offset:])
        os.fsync(temp_fd)
        os.fchmod(temp_fd, stat.S_IMODE(current[2]))
        os.fsync(temp_fd)
        staged = _metadata(temp_fd)
        if (not stat.S_ISREG(staged[2]) or staged[3] != 1 or staged[4] != len(replacement)
                or stat.S_IMODE(staged[2]) != stat.S_IMODE(current[2])):
            raise ValueError(f"invalid staged Linux generator output: {relative}")
        os.close(temp_fd)
        temp_fd = None
        if before_publish is not None:
            before_publish(src / relative)
        _assert_directory_chain(src, relative, expected_chain)
        current_bytes, current_after = _path_payload(parent_fd, name)
        if (current_bytes != expected or current_after != current
                or (expected_metadata is not None and current_after != expected_metadata)):
            raise ValueError(f"Linux generator output changed before publish: {relative}")
        _rename_at(parent_fd, temp_name, name, _RENAME_EXCHANGE)
        exchanged = True
        _fsync_parent(parent_fd)
        old_bytes, old_metadata = _path_payload(parent_fd, temp_name)
        if old_bytes != expected or old_metadata != current:
            _rename_at(parent_fd, temp_name, name, _RENAME_EXCHANGE)
            exchanged = False
            _fsync_parent(parent_fd)
            raise ValueError(f"Linux generator output changed during publish: {relative}")
        new_bytes, new_metadata = _path_payload(parent_fd, name)
        if (new_bytes != replacement or new_metadata[3] != 1
                or new_metadata != staged):
            raise ValueError(f"Linux generator output changed after publish: {relative}")
        _assert_directory_chain(src, relative, expected_chain)
        os.unlink(temp_name, dir_fd=parent_fd)
        exchanged = False
        _fsync_parent(parent_fd)
    except BaseException:
        if temp_fd is not None:
            os.close(temp_fd)
        if exchanged:
            try:
                _rename_at(parent_fd, temp_name, name, _RENAME_EXCHANGE)
                _fsync_parent(parent_fd)
                exchanged = False
            except OSError:
                pass
        if not exchanged:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(parent_fd)
        _close_all(ancestors)


atomic_replace = _atomic_replace


def _ensure_directory_fd(src: Path, relative: str) -> tuple[int, list[int]]:
    parts = PurePosixPath(relative).parts
    if not parts or parts[0] == ".." or "\\" in relative:
        raise ValueError(f"unsafe Linux generator directory: {relative}")
    fd = _open_dir_nofollow(src)
    opened = [fd]
    try:
        for part in parts:
            try:
                child = _open_relative_dir(fd, part)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                child = _open_relative_dir(fd, part)
            opened.append(child)
            fd = child
        return fd, opened[:-1]
    except BaseException:
        _close_all(opened)
        raise


def _write_fd(fd: int, data: bytes, mode: int) -> None:
    os.fchmod(fd, mode)
    offset = 0
    while offset < len(data):
        offset += os.write(fd, data[offset:])
    os.fsync(fd)


def _atomic_create(src: Path, relative: str, data: bytes, mode: int,
                   *, before_publish=None) -> None:
    """Create one missing file atomically beneath an anchored no-follow directory walk."""
    parent_relative = relative.rsplit("/", 1)[0]
    parent_fd, ancestors = _ensure_directory_fd(src, parent_relative)
    name = relative.rsplit("/", 1)[1]
    temp_name = f".chromix-{secrets.token_hex(12)}"
    temp_fd = None
    try:
        expected_chain = _directory_chain(parent_fd, ancestors)
        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=parent_fd)
        _write_fd(temp_fd, data, mode)
        staged = _metadata(temp_fd)
        if staged[3] != 1 or staged[4] != len(data) or stat.S_IMODE(staged[2]) != mode:
            raise ValueError(f"invalid staged Linux generator file: {relative}")
        os.close(temp_fd)
        temp_fd = None
        if before_publish is not None:
            before_publish(src / relative)
        _assert_directory_chain(src, relative, expected_chain)
        _rename_at(parent_fd, temp_name, name, _RENAME_NOREPLACE)
        _fsync_parent(parent_fd)
    except BaseException:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(parent_fd)
        _close_all(ancestors)


def safe_path(src: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if (not relative or parts.is_absolute() or ".." in parts.parts
            or "\\" in relative or "\0" in relative or parts.as_posix() != relative):
        raise ValueError(f"unsafe Linux generator path: {relative}")
    path = src / relative
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError(f"linked Linux generator path: {relative}")
    return path


def regular(src: Path, relative: str) -> Path:
    path = safe_path(src, relative)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_size:
        raise ValueError(f"missing, empty or linked Linux generator input: {relative}")
    return path


def module_identity(src: Path, *, pins: tuple = PINS) -> dict | None:
    profile = generator_profile(pins)
    root = safe_path(src, ESBUILD_MODULE)
    if not root.exists():
        return None
    if not root.is_dir():
        raise ValueError("invalid esbuild module directory")
    files = {}
    def walk_error(error):
        raise error
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        for name in dirs:
            safe_path(src, (Path(directory) / name).relative_to(src).as_posix())
        for name in names:
            relative = (Path(directory) / name).relative_to(src).as_posix()
            path = regular(src, relative)
            files[path.relative_to(root).as_posix()] = sha256(read_verified(src, relative))
    if files != profile["module"]:
        raise ValueError("unknown esbuild module bytes/inventory")
    return {"path": ESBUILD_MODULE, "files": files}


def binary_identity(src: Path, *, pins: tuple = PINS) -> dict | None:
    profile = generator_profile(pins)
    path = safe_path(src, ESBUILD_BINARY)
    if not path.exists():
        return None
    payload = read_verified(src, ESBUILD_BINARY)
    digest = sha256(payload)
    host = next((arch for arch, value in profile["binaries"].items() if value == digest), None)
    if host is None:
        raise ValueError("unknown esbuild native binary bytes")
    if (payload[:6] != b"\x7fELF\x02\x01" or len(payload) < 20
            or int.from_bytes(payload[18:20], "little") != {"x64": 62, "arm64": 183}[host]
            or not os.access(path, os.X_OK)):
        raise ValueError("esbuild native binary header/mode mismatch")
    return {"path": ESBUILD_BINARY, "sha256": digest, "host_arch": host}


def source_plan(src: Path, *, pins: tuple = PINS) -> tuple[dict, list]:
    profile = generator_profile(pins)
    if sha256(read_verified(src, LOCK)) != profile["lock"]:
        raise ValueError("unknown Linux DevTools package lock")
    identities, changes = {}, []
    for relative, (original_hash, repaired_hash, old, new) in profile["sources"].items():
        name = DEVTOOLS + "/" + relative
        regular(src, name)
        payload, metadata = _read_verified_with_metadata(src, name)
        digest = sha256(payload)
        if digest not in (original_hash, repaired_hash):
            raise ValueError(f"unknown Linux esbuild source: {name}")
        identities[name] = {"sha256": digest, "original_sha256": original_hash,
                            "repaired_sha256": repaired_hash}
        if digest != repaired_hash:
            restored = payload
            replacements = zip(old, new) if isinstance(old, tuple) else ((old, new),)
            for before, after in replacements:
                if restored.count(before) != 1:
                    raise ValueError(f"unexpected Linux esbuild repair: {name}")
                restored = restored.replace(before, after, 1)
            if sha256(restored) != repaired_hash:
                raise ValueError(f"unexpected Linux esbuild repair: {name}")
            changes.append((name, payload, restored, metadata))
    return identities, changes


def prepare_esbuild(src: Path, *, host_arch: str, repair=False, before_publish=None,
                    pins: tuple = PINS) -> dict:
    """Inspect without executing; repair only after exact package and native-host checks."""
    profile = generator_profile(pins)
    if host_arch not in profile["binaries"]:
        raise ValueError("unsupported Linux esbuild host")
    sources, changes = source_plan(src, pins=pins)
    module, binary = module_identity(src, pins=pins), binary_identity(src, pins=pins)
    install_needed = module is None or binary is None or binary["host_arch"] != host_arch
    result = {"version": profile["version"], "host_arch": host_arch, "sources": sources,
              "lock_sha256": profile["lock"], "module": module, "binary": binary,
              "install_needed": install_needed, "repair_needed": install_needed or bool(changes)}
    if not repair:
        return result
    if install_needed:
        raise ValueError("pinned native esbuild/module must be installed before finish")
    command = [str(src / ESBUILD_BINARY), "--version"]
    completed = subprocess.run(command, cwd=src, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, check=False, timeout=30)
    if completed.returncode or completed.stdout.strip() != profile["version"]:
        raise ValueError(f"pinned esbuild probe failed: {completed.stdout[:2000]}")
    # No restored JS is executed by the module-resolution probe.
    node = src / f"third_party/node/linux/node-linux-{host_arch}/bin/node"
    script = ("const p=require('node:path');const r=require.resolve('esbuild');"
              "if(r!==p.resolve('node_modules/esbuild/lib/main.js'))process.exit(1);"
              "process.env.ESBUILD_BINARY_PATH=process.argv[1];"
              f"const e=require('esbuild');if(e.version!=={json.dumps(profile['version'])})process.exit(2);"
              "e.transformSync('const n: number = 1',{loader:'ts'});")
    completed = subprocess.run([str(node), "-e", script, str(src / ESBUILD_BINARY)],
                               cwd=src / DEVTOOLS, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, check=False, timeout=30)
    if completed.returncode:
        raise ValueError(f"pinned esbuild module probe failed: {completed.stdout[:2000]}")
    if module_identity(src, pins=pins) != module or binary_identity(src, pins=pins) != binary:
        raise ValueError("esbuild tools changed during preparation")
    for name, before, after, metadata in changes:
        if _read_verified_with_metadata(src, name) != (before, metadata):
            raise ValueError("esbuild source changed during preparation")
    for name, before, after, metadata in changes:
        _atomic_replace(src, name, before, after, expected_metadata=metadata,
                        before_publish=before_publish)
        sources[name]["sha256"] = sha256(after)
    result["repair_needed"] = False
    return result


def archive_files(archive: Path, key: str, *, pins: tuple = PINS) -> dict[str, bytes]:
    """Verify the compressed archive before parsing; never extract or run install scripts."""
    profile = generator_profile(pins)
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > 32 * 1024**2:
        raise ValueError("invalid esbuild archive")
    data = archive.read_bytes()
    if sha256(data) != profile["archives"][key][1]:
        raise ValueError(f"esbuild archive SHA256 mismatch: {key}")
    files, size = {}, 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as stream:
        for entry in stream:
            name = PurePosixPath(entry.name)
            if (not entry.isfile() or len(name.parts) < 2 or name.parts[0] != "package"
                    or ".." in name.parts or name.as_posix() != entry.name or "\\" in entry.name):
                raise ValueError("unsafe esbuild archive member")
            relative = name.relative_to("package").as_posix()
            size += entry.size
            if relative in files or size > 32 * 1024**2:
                raise ValueError("duplicate/oversized esbuild archive member")
            files[relative] = stream.extractfile(entry).read()
    return files


def install_esbuild(src: Path, *, host_arch: str, module_archive: Path, native_archive: Path,
                    before_publish=None, pins: tuple = PINS) -> dict:
    """Install verified local artifacts only; caller must first verify the restore receipt."""
    profile = generator_profile(pins)
    before = prepare_esbuild(src, host_arch=host_arch, pins=pins)
    module = archive_files(module_archive, "module", pins=pins)
    native = archive_files(native_archive, host_arch, pins=pins)
    if {name: sha256(data) for name, data in module.items()} != profile["module"]:
        raise ValueError("unexpected esbuild module archive contents")
    if (set(native) != {"bin/esbuild", "package.json", "README.md"}
            or sha256(native["bin/esbuild"]) != profile["binaries"][host_arch]):
        raise ValueError("unexpected esbuild native archive contents")
    destination = safe_path(src, ESBUILD_MODULE)
    if before["module"] is None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".chromix-esbuild-", dir=destination.parent) as temp:
            staged = Path(temp) / "package"
            staged.mkdir()
            for name, data in module.items():
                path = staged / name
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o755 if name == "bin/esbuild" else 0o644)
                try:
                    offset = 0
                    while offset < len(data):
                        offset += os.write(fd, data[offset:])
                    os.fsync(fd)
                finally:
                    os.close(fd)
            if safe_path(src, ESBUILD_MODULE).exists():
                raise ValueError("esbuild module appeared during installation")
            staging_parent_fd = _open_dir_nofollow(Path(temp))
            module_parent_fd = _open_dir_nofollow(destination.parent)
            try:
                _rename_at(staging_parent_fd, staged.name, destination.name, _RENAME_NOREPLACE,
                           destination_fd=module_parent_fd)
                _fsync_parent(staging_parent_fd)
                _fsync_parent(module_parent_fd)
            finally:
                os.close(staging_parent_fd)
                os.close(module_parent_fd)
    binary = safe_path(src, ESBUILD_BINARY)
    if before["binary"] is None or before["binary"]["host_arch"] != host_arch:
        if binary_identity(src, pins=pins) != before["binary"]:
            raise ValueError("esbuild binary changed during installation")
        parent = ESBUILD_BINARY.rsplit("/", 1)[0]
        parent_fd, ancestors = _ensure_directory_fd(src, parent)
        temp_name = f".chromix-{secrets.token_hex(12)}"
        temp_fd = None
        try:
            temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o700, dir_fd=parent_fd)
            _write_fd(temp_fd, native["bin/esbuild"], 0o755)
            os.close(temp_fd)
            temp_fd = None
            _rename_at(parent_fd, temp_name, "esbuild", _RENAME_NOREPLACE if before["binary"] is None else 0)
            _fsync_parent(parent_fd)
        except BaseException:
            if temp_fd is not None:
                os.close(temp_fd)
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(parent_fd)
            _close_all(ancestors)
    return prepare_esbuild(src, host_arch=host_arch, pins=pins)


def atomic_wrapper_replace(src: Path, expected: bytes, replacement: bytes, *, before_publish=None) -> None:
    """Atomically publish the pinned TypeScript wrapper through the Linux safe writer."""
    _atomic_replace(src, DEVTOOLS + "/third_party/typescript/typescript.py", expected, replacement,
                    before_publish=before_publish)
