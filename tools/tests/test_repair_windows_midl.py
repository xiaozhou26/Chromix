"""Pinned MIDL repair, real upstream control flow, and restored-stage integration."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import builtins
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid
import warnings

from tools import prepare_restored_build as prepare
from tools import repair_windows_midl as repair
from tools import restore_upstream_cache as restore
from tools.tests import test_prepare_restored_build as prepare_tests


FIXTURE = Path(__file__).parent / "fixtures/windows153_midl.py"
ORIGINAL = FIXTURE.read_bytes()
GUID = "158428A4-6014-4978-83BA-9FAD0DABE791"
REPLACEMENT = "D0E1CACC-C63C-4192-94AB-BF8EAD0E3B83"
RULE_NAME = "__chrome_elevation_service_elevation_service_idl_idl_action___build_toolchain_win_win_clang_x64__rule"
RULE_COMMAND = ("C$:/hostedtoolcache/windows/Python/3.12.10/x64/python3.exe "
                "../../build/toolchain/win/midl.py environment.x64 "
                "../../third_party/win_build_output/midl/chrome/elevation_service "
                "gen/chrome/elevation_service none ${source_name_part}.tlb ${source_name_part}.h "
                "${source_name_part}.dlldata.c ${source_name_part}_i.c ${source_name_part}_p.c "
                "../../third_party/llvm-build/Release+Asserts/bin/clang-cl.exe ${in} /char signed /env x64 /Oicf")
RULE = (f"rule {RULE_NAME}\n  command = {RULE_COMMAND}\n"
        "  description = ACTION //chrome/elevation_service:elevation_service_idl_idl_action"
        "(//build/toolchain/win:win_clang_x64)\n  restat = 1\n  pool = build_toolchain_action_pool\n\n")


def graph_text(names):
    return RULE + "build " + " ".join(names) + ": " + RULE_NAME + " | ../../" + repair.SCRIPT + "\n"


def msft_tlb(guid=GUID):
    # Minimal MSFT sections consumed by the real GUID parser and ZapTimestamp.
    header = bytearray(0x54 + 15 * 16)
    header[:8] = b"MSFT\x02\x00\x01\x00"
    hash_offset = len(header)
    guid_offset = hash_offset + 0x80
    custom_offset = guid_offset + 24
    struct.pack_into("<II", header, 0x54 + 4 * 16, hash_offset, 0x80)
    struct.pack_into("<II", header, 0x54 + 5 * 16, guid_offset, 24)
    struct.pack_into("<II", header, 0x54 + 11 * 16, custom_offset, 0x54)
    custom = (b"\x08\x00\x3e\x00\x00\x00"
              b"Created by MIDL version 8.01.0622 at Tue Jan 19 03:14:07 2038\n"
              b"\x13\x00\xff\xff\xff\x7fWW\x13\x00\x6e\x02\x01\x08WW")
    return bytes(header) + b"\xff" * 0x80 + uuid.UUID(guid).bytes_le + b"\xff" * 8 + custom


def load_midl(payload=None):
    namespace = {"__name__": "midl_fixture"}
    exec(compile(repair.transform(ORIGINAL) if payload is None else payload, str(FIXTURE), "exec"), namespace)
    namespace["sys"] = SimpleNamespace(platform="win32")
    return namespace


class WindowsMidlReadTest(unittest.TestCase):
    FIELDS = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="windows midl stat ")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "midl.py"
        self.path.write_bytes(b"stable\n")
        self.before = SimpleNamespace(
            st_dev=0x1234567890ABCDEF, st_ino=(1 << 100) + 17,
            st_mode=stat.S_IFREG | 0o666, st_nlink=1, st_size=7,
            st_mtime_ns=1700000000000000200, st_ctime_ns=1700000000000000000,
            st_birthtime_ns=1700000000000000000, st_file_attributes=0x20)
        self.opened = self.changed(self.before, st_ctime_ns=1700000000001234500)

    def changed(self, info, **changes):
        return SimpleNamespace(**(vars(info) | changes))

    def simulated(self, *, before=None, opened=None, after=None, final=None, windows=True, **kwargs):
        before = self.before if before is None else before
        opened = self.opened if opened is None else opened
        after = before if after is None else after
        final = opened if final is None else final
        with mock.patch.object(repair, "WINDOWS", windows), \
                mock.patch.object(repair, "_safe_stat", side_effect=[before, after]), \
                mock.patch.object(repair.os, "fstat", side_effect=[opened, final]):
            return repair._read(self.path, **kwargs)

    def test_windows_stable_ctime_difference_and_full_width_ids_return_path_stat(self):
        self.assertNotEqual(repair._identity(self.before), repair._identity(self.opened))
        payload, info = self.simulated()
        self.assertEqual(payload, b"stable\n")
        self.assertIs(info, self.before)
        self.assertGreater(info.st_dev, (1 << 32) - 1)
        self.assertGreater(info.st_ino, (1 << 64) - 1)
        high_only = self.changed(self.before, st_dev=1 << 48, st_ino=1 << 100)
        self.assertEqual(self.simulated(before=high_only, opened=high_only)[0], payload)

    def test_windows_cross_api_differences_retain_full_identity_and_diagnostics(self):
        changes = {
            "st_dev": self.opened.st_dev + (1 << 32),
            "st_ino": self.opened.st_ino + (1 << 64),
            "st_mode": stat.S_IFREG | 0o444,
            "st_nlink": 2,
            "st_size": 8,
            "st_mtime_ns": self.opened.st_mtime_ns + 1,
            "st_birthtime_ns": self.opened.st_birthtime_ns + 1,
        }
        for field, value in changes.items():
            with self.subTest(field=field), self.assertRaises(ValueError) as caught:
                self.simulated(opened=self.changed(self.opened, **{field: value}))
            message = str(caught.exception)
            self.assertIn(str(self.path), message)
            self.assertIn(field, message)
            self.assertIn(str(value), message)
            if field != "st_nlink":
                self.assertIn("P0/H0", message)
                self.assertIn(str(getattr(self.before, field)), message)
            self.assertNotIn("st_ctime_ns", message)
        self.assertEqual(self.path.read_bytes(), b"stable\n")

    def test_windows_each_api_retains_all_before_after_checks(self):
        for where, original, phase in (("after", self.before, "P0/P1"),
                                        ("final", self.opened, "H0/H1")):
            for field in self.FIELDS + ("st_birthtime_ns",):
                value = getattr(original, field) + 1
                with self.subTest(where=where, field=field), self.assertRaises(ValueError) as caught:
                    self.simulated(**{where: self.changed(original, **{field: value})})
                message = str(caught.exception)
                self.assertIn(str(self.path), message)
                self.assertIn(field, message)
                self.assertIn(str(value), message)
                if field != "st_nlink" or where == "after":
                    self.assertIn(phase, message)

    def test_windows_both_apis_changing_together_is_not_a_stable_read(self):
        for field in self.FIELDS + ("st_birthtime_ns",):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                self.simulated(after=self.changed(self.before, **{field: getattr(self.before, field) + 1}),
                               final=self.changed(self.opened, **{field: getattr(self.opened, field) + 1}))

    def test_windows_missing_or_invalid_identity_is_rejected_on_every_snapshot(self):
        for where in ("before", "opened", "after", "final"):
            original = self.before if where in ("before", "after") else self.opened
            for field in self.FIELDS + ("st_birthtime_ns",):
                for value in ("missing", None, True, 1.5, "invalid"):
                    changed = self.changed(original, **{field: value})
                    if value == "missing":
                        delattr(changed, field)
                    with self.subTest(where=where, field=field, value=value), \
                            self.assertRaises(ValueError) as caught:
                        self.simulated(**{where: changed})
                    self.assertIn(str(self.path), str(caught.exception))
                    self.assertIn(field, str(caught.exception))

    def test_windows_zero_negative_or_truncated_ids_are_never_wildcards(self):
        for field, width in (("st_dev", 32), ("st_ino", 64)):
            for value in (0, -1, getattr(self.opened, field) & ((1 << width) - 1)):
                for where in ("before", "opened", "after", "final"):
                    original = self.before if where in ("before", "after") else self.opened
                    with self.subTest(field=field, value=value, where=where), \
                            self.assertRaisesRegex(ValueError, field):
                        self.simulated(**{where: self.changed(original, **{field: value})})
            for value in (0, -1):
                with self.subTest(field=field, both=value), self.assertRaisesRegex(ValueError, field):
                    self.simulated(before=self.changed(self.before, **{field: value}),
                                   opened=self.changed(self.opened, **{field: value}))
        for field, value in (("st_mode", 0), ("st_nlink", 0), ("st_size", -1)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                self.simulated(before=self.changed(self.before, **{field: value}),
                               opened=self.changed(self.opened, **{field: value}))

    def test_windows_handles_reject_links_reparse_and_non_regular_before_and_after(self):
        changes = ({"st_file_attributes": stat.FILE_ATTRIBUTE_REPARSE_POINT},
                   {"st_nlink": 2}, {"st_mode": stat.S_IFLNK | 0o666},
                   {"st_mode": stat.S_IFDIR | 0o777})
        for where in ("opened", "final"):
            for fields in changes:
                with self.subTest(where=where, fields=fields), \
                        self.assertRaisesRegex(ValueError, "linked or unsafe") as caught:
                    self.simulated(**{where: self.changed(self.opened, **fields)})
                self.assertIn(str(self.path), str(caught.exception))
                for field, value in fields.items():
                    self.assertIn(f"{field}={value}", str(caught.exception))

    def test_read_checks_final_handle_and_corresponding_path(self):
        with mock.patch.object(repair, "_check_info", wraps=repair._check_info) as check, \
                mock.patch.object(repair, "_check_identity", wraps=repair._check_identity) as compare:
            self.simulated()
        self.assertEqual(check.call_args_list,
                         [mock.call(self.path, self.opened), mock.call(self.path, self.opened)])
        self.assertEqual(compare.call_args_list, [
            mock.call(self.path, self.before, self.opened, "before reading (P0/H0)", cross_api=True),
            mock.call(self.path, self.before, self.before, "during reading (P0/P1)"),
            mock.call(self.path, self.opened, self.opened, "during reading (H0/H1)"),
            mock.call(self.path, self.before, self.opened, "during reading (P1/H1)", cross_api=True),
        ])

    def test_posix_keeps_all_original_fields_without_requiring_birthtime(self):
        before = self.changed(self.before)
        del before.st_birthtime_ns
        self.assertEqual(self.simulated(before=before, opened=before, windows=False)[0], b"stable\n")
        self.assertEqual(self.simulated(opened=self.changed(self.before, st_birthtime_ns=0),
                                        windows=False)[0], b"stable\n")
        for field in self.FIELDS:
            changed = self.changed(before, **{field: getattr(before, field) + 1})
            for where in ("opened", "after", "final"):
                snapshots = dict(before=before, opened=before, after=before, final=before)
                snapshots[where] = changed
                with self.subTest(field=field, where=where), self.assertRaisesRegex(ValueError, field):
                    self.simulated(windows=False, **snapshots)
        zero = self.changed(before, st_dev=0, st_ino=0)
        self.assertEqual(self.simulated(before=zero, opened=zero, windows=False)[0], b"stable\n")

    def test_native_readonly_and_temporary_reads(self):
        self.path.chmod(0o444)
        try:
            payload, info = repair._read(self.path)
            self.assertEqual(payload, b"stable\n")
            self.assertEqual(repair._identity(info), repair._identity(self.path.lstat()))
        finally:
            self.path.chmod(0o666)
        with tempfile.TemporaryDirectory(dir=self.path.parent) as directory:
            fd, name = tempfile.mkstemp(dir=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(b"temporary\n")
            self.assertEqual(repair._read(Path(name))[0], b"temporary\n")

    def test_windows_limit_and_disappearance_still_fail_closed(self):
        self.assertEqual(self.simulated(limit=7)[0], b"stable\n")
        with self.assertRaisesRegex(ValueError, "oversized") as caught:
            self.simulated(limit=6)
        self.assertIn(str(self.path), str(caught.exception))
        self.assertIn("bytes_read=7, limit=6", str(caught.exception))
        for snapshots in ([None], [self.before, None]):
            with self.subTest(snapshots=snapshots), mock.patch.object(repair, "WINDOWS", True), \
                    mock.patch.object(repair, "_safe_stat", side_effect=snapshots), \
                    mock.patch.object(repair.os, "fstat", return_value=self.opened), \
                    self.assertRaisesRegex(ValueError, "disappeared") as caught:
                repair._read(self.path)
            self.assertIn(str(self.path), str(caught.exception))


class WindowsMidlRepairTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="windows midl ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.src = self.root / "src"
        self.script = self.src / repair.SCRIPT
        self.script.parent.mkdir(parents=True)
        self.script.write_bytes(ORIGINAL)
        self.identity = dict(repair.IDENTITIES["x64"])

    def apply(self, **kwargs):
        return repair.apply(self.src, kwargs.get("platform", "windows"),
                            kwargs.get("arch", "x64"), kwargs.get("identity", self.identity))

    def test_actual_fixture_exact_transform_and_idempotence(self):
        self.assertEqual(hashlib.sha256(ORIGINAL).hexdigest(), repair.ORIGINAL_SHA256)
        repaired = repair.transform(ORIGINAL)
        self.assertEqual(repaired, ORIGINAL.replace(repair.BEFORE, repair.AFTER, 1)
                         .replace(repair.COMPARE_BEFORE, repair.COMPARE_AFTER, 1))
        self.assertEqual(hashlib.sha256(repaired).hexdigest(), repair.REPAIRED_SHA256)
        self.assertIs(repair.transform(repaired), repaired)
        compile(repaired, "midl.py", "exec")

    def test_apply_atomic_and_already_repaired_keeps_mtime_and_mode(self):
        self.script.chmod(0o755)
        before = self.script.stat()
        original_replace = os.replace

        def replace(source, destination):
            if Path(destination) == self.script:
                self.assertEqual(self.script.read_bytes(), ORIGINAL)
                self.assertEqual(Path(source).read_bytes(), repair.transform(ORIGINAL))
                self.assertEqual(Path(source).parent, self.script.parent)
            return original_replace(source, destination)

        with mock.patch.object(repair.os, "replace", side_effect=replace) as publish:
            result = self.apply()
            self.assertEqual(publish.call_count, 2)
        self.assertEqual(result["before_sha256"], repair.ORIGINAL_SHA256)
        self.assertEqual(result["after_sha256"], repair.REPAIRED_SHA256)
        self.assertTrue(result["changed"])
        self.assertEqual(stat.S_IMODE(before.st_mode), stat.S_IMODE(self.script.stat().st_mode))
        os.utime(self.script, ns=(123456789000, 123456789000))
        before = self.script.stat()
        with mock.patch.object(repair.os, "replace") as publish:
            again = self.apply()
            publish.assert_not_called()
        self.assertFalse(again["changed"])
        self.assertEqual(again["before_sha256"], repair.REPAIRED_SHA256)
        self.assertEqual(self.script.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(list(self.script.parent.iterdir()), [self.script])

    def test_windows_ctime_difference_through_temporary_readonly_and_idempotent_apply(self):
        original_lstat, original_fstat = Path.lstat, os.fstat
        birthtime = 1700000000000000000
        reads = []

        def snapshot(info, *, handle=False):
            fields = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
            fields.update(st_birthtime_ns=birthtime,
                          st_ctime_ns=birthtime + 1234500 if handle else birthtime)
            return SimpleNamespace(**fields)

        def lstat(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if not stat.S_ISREG(info.st_mode):
                return info
            reads.append(path)
            return snapshot(info)

        def fstat(fd):
            return snapshot(original_fstat(fd), handle=True)

        self.script.chmod(0o444)
        try:
            with mock.patch.object(repair, "WINDOWS", True), \
                    mock.patch.object(Path, "lstat", lstat), \
                    mock.patch.object(repair.os, "fstat", side_effect=fstat):
                payload, before = repair._read(self.script)
                self.assertEqual(payload, ORIGINAL)
                self.assertEqual(before.st_ctime_ns, birthtime)
                self.assertEqual(stat.S_IMODE(before.st_mode), 0o444)
                self.script.chmod(0o666)
                self.assertTrue(self.apply()["changed"])
                self.assertEqual(stat.S_IMODE(self.script.stat().st_mode), 0o666)
                self.assertEqual(self.script.read_bytes(), repair.transform(ORIGINAL))
                record = self.src / repair.RECORD
                baseline = {p: (p.read_bytes(), repair._identity(p.lstat())) for p in (self.script, record)}
                with mock.patch.object(repair.os, "replace") as publish:
                    self.assertFalse(self.apply()["changed"])
                    publish.assert_not_called()
                for path, state in baseline.items():
                    self.assertEqual((path.read_bytes(), repair._identity(path.lstat())), state)
                self.assertTrue(any(p.name.startswith(".midl-repair-") for p in reads))
                self.assertTrue(any(p.name.startswith(".midl-record-") for p in reads))
                self.assertIn(record, reads)
                self.assertEqual(list(self.script.parent.iterdir()), [self.script])
                self.assertEqual(list(self.src.glob(".midl-record-*")), [])
        finally:
            self.script.chmod(0o666)

    def test_unknown_partial_duplicate_and_crlf_scripts_fail_closed(self):
        repaired = repair.transform(ORIGINAL)
        for raw in (b"", b"MSFT invalid", ORIGINAL + b"# unknown\n",
                    ORIGINAL.replace(repair.BEFORE, repair.AFTER, 1),
                    ORIGINAL.replace(repair.BEFORE, repair.BEFORE * 2),
                    ORIGINAL.replace(b"\n", b"\r\n"), repaired + b"# modified\n",
                    repaired.replace(b"source_file == tlb", b"True")):
            with self.subTest(sha256=hashlib.sha256(raw).hexdigest()):
                self.script.write_bytes(raw)
                before = self.script.stat().st_mtime_ns
                with mock.patch.object(repair.os, "replace") as publish, \
                        self.assertRaisesRegex(ValueError, "unknown restored Windows MIDL script"):
                    self.apply()
                publish.assert_not_called()
                self.assertEqual(self.script.read_bytes(), raw)
                self.assertEqual(self.script.stat().st_mtime_ns, before)
                self.assertEqual(list(self.script.parent.iterdir()), [self.script])

    def test_corrupted_transform_pin_and_anchor_cannot_publish(self):
        for attribute, value in (("REPAIRED_SHA256", "0" * 64), ("BEFORE", b"missing anchor")):
            with self.subTest(attribute=attribute), mock.patch.object(repair, attribute, value), \
                    self.assertRaises(ValueError):
                self.apply()
            self.assertEqual(self.script.read_bytes(), ORIGINAL)

    def test_every_upstream_identity_field_and_unpinned_arch_fail_closed(self):
        for key in self.identity:
            value = "153.0.8010.36" if key == "chromium_version" else "unknown"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "upstream identity"):
                self.apply(identity=dict(self.identity, **{key: value}))
        for identity in ({"chromium_version": "153.0.8010.47"}, dict(self.identity, extra=True)):
            with self.assertRaisesRegex(ValueError, "upstream identity"):
                self.apply(identity=identity)
        with self.assertRaisesRegex(ValueError, "upstream identity"):
            self.apply(arch="arm64")
        self.assertEqual(self.script.read_bytes(), ORIGINAL)

    def test_non_windows_other_version_and_absent_targets_are_noops(self):
        for platform in ("linux", "macos"):
            with mock.patch.object(repair, "_safe_stat") as inspect:
                self.assertEqual(self.apply(platform=platform, identity={})["status"], "not_applicable")
                inspect.assert_not_called()
        result = self.apply(identity=dict(self.identity, chromium_version="152.0.7977.82"))
        self.assertEqual(result["status"], "not_applicable")
        self.assertEqual(self.script.read_bytes(), ORIGINAL)
        self.script.unlink()
        self.assertEqual(self.apply(identity={})["status"], "no_repair_targets")
        self.assertEqual(repair.apply(self.root / "absent", "windows", "x64", {})["status"],
                         "no_repair_targets")

    def test_script_symlink_hardlink_and_dangling_link_are_rejected(self):
        outside = self.root / "outside.py"
        outside.write_bytes(ORIGINAL)
        for kind in ("symlink", "hardlink", "dangling"):
            with self.subTest(kind=kind):
                self.script.unlink()
                if kind == "hardlink":
                    os.link(outside, self.script)
                else:
                    self.script.symlink_to(outside if kind == "symlink" else self.root / "absent.py")
                with self.assertRaisesRegex(ValueError, "linked or unsafe"):
                    self.apply()
                self.assertEqual(outside.read_bytes(), ORIGINAL)
                self.script.unlink()
                self.script.write_bytes(ORIGINAL)

    def test_linked_parents_and_non_regular_script_are_rejected(self):
        for parent in (self.script.parent, self.src / "build", self.src):
            with self.subTest(parent=parent):
                outside = self.root / "outside"
                parent.rename(outside)
                parent.symlink_to(outside, target_is_directory=True)
                try:
                    with self.assertRaisesRegex(ValueError, "linked or unsafe"):
                        self.apply()
                    self.assertEqual(self.script.read_bytes(), ORIGINAL)
                finally:
                    parent.unlink()
                    outside.rename(parent)
        self.script.unlink()
        self.script.mkdir()
        with self.assertRaisesRegex(ValueError, "linked or unsafe"):
            self.apply()

    def test_reparse_attribute_on_file_or_parent_is_rejected(self):
        original_lstat = Path.lstat
        for target in (self.script, self.script.parent, self.src):
            def lstat(path, *args, **kwargs):
                info = original_lstat(path, *args, **kwargs)
                if path != target:
                    return info
                fields = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
                return SimpleNamespace(**dict(fields, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT))
            with self.subTest(target=target), mock.patch.object(Path, "lstat", lstat), \
                    self.assertRaisesRegex(ValueError, "linked or unsafe"):
                self.apply()
        self.assertEqual(self.script.read_bytes(), ORIGINAL)

    def test_read_and_publish_failures_leave_original_and_no_temporary_files(self):
        for target in ("fsync", "replace"):
            with self.subTest(target=target), mock.patch.object(repair.os, target, side_effect=OSError("failed")), \
                    self.assertRaisesRegex(OSError, "failed"):
                self.apply()
            self.assertEqual(self.script.read_bytes(), ORIGINAL)
            self.assertEqual(list(self.script.parent.iterdir()), [self.script])
        with mock.patch.object(repair.os, "open", side_effect=OSError("unreadable")), \
                self.assertRaisesRegex(OSError, "unreadable"):
            self.apply()
        self.assertEqual(self.script.read_bytes(), ORIGINAL)

    def test_concurrent_unexpected_script_change_is_not_overwritten(self):
        real_fsync = os.fsync
        def fsync(fd):
            self.script.write_bytes(b"other writer")
            real_fsync(fd)
        with mock.patch.object(repair.os, "fsync", side_effect=fsync), \
                self.assertRaisesRegex(ValueError, "changed before replacement"):
            self.apply()
        self.assertEqual(self.script.read_bytes(), b"other writer")
        self.assertEqual(list(self.script.parent.iterdir()), [self.script])

    def test_temporary_content_or_link_swap_cannot_publish(self):
        outside = self.root / "outside.py"
        outside.write_bytes(repair.transform(ORIGINAL))
        real_chmod = os.chmod
        for kind in ("content", "hardlink", "symlink"):
            def chmod(path, mode):
                real_chmod(path, mode)
                if kind == "content":
                    Path(path).write_bytes(b"tampered temporary")
                else:
                    Path(path).unlink()
                    if kind == "hardlink":
                        os.link(outside, path)
                    else:
                        Path(path).symlink_to(outside)
            with self.subTest(kind=kind), mock.patch.object(repair.os, "chmod", side_effect=chmod), \
                    self.assertRaises(ValueError):
                self.apply()
            self.assertEqual(self.script.read_bytes(), ORIGINAL)
            self.assertEqual(outside.read_bytes(), repair.transform(ORIGINAL))
            self.assertEqual(list(self.script.parent.iterdir()), [self.script])

    def graph(self, *, include=True, names=("gen/midl/a.h", "gen/midl/a_i.c", "gen/midl/a.tlb")):
        out = self.src / "out/Default"
        out.mkdir(parents=True, exist_ok=True)
        edge = graph_text(names)
        graph = out / "build.ninja"
        graph.write_text("subninja toolchain.ninja\n" if include else edge)
        if include:
            (out / "toolchain.ninja").write_text(edge)
        for name in names:
            path = out / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"stale output")
        return out, [out / name for name in names]

    def test_graph_variable_script_reference_fails_before_any_write(self):
        out, outputs = self.graph()
        graph = out / "toolchain.ninja"
        for reference in ("$midl_script", "${midl_script}"):
            graph.write_text("midl_script = ../../build/toolchain/win/midl.py\n" +
                             "build gen/midl/a.h: midl | " + reference + "\n")
            future = time.time_ns() + 86400 * 10**9
            for path in outputs:
                os.utime(path, ns=(future, future))
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in [*outputs, self.script]}
            with self.subTest(reference=reference), self.assertRaisesRegex(ValueError, "unsupported variable"):
                self.apply()
            for path, state in before.items():
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), state)
            self.assertFalse((self.src / repair.RECORD).exists())

    def test_tab_include_and_subninja_traverse_actual_format_rule(self):
        out, outputs = self.graph()
        for directive in ("include", "subninja"):
            (out / "build.ninja").write_text(directive + "\ttoolchain.ninja\n")
            with self.subTest(directive=directive):
                names, graphs = repair._graph(self.src)
                self.assertEqual(names, sorted(p.relative_to(out).as_posix() for p in outputs))
                self.assertEqual(graphs["file_count"], 2)
        result = self.apply()
        self.assertEqual(len(result["invalidated_outputs"]), 3)
        self.assertTrue(all(not path.exists() for path in outputs))
        self.assertEqual(self.apply()["invalidated_outputs"], [])

    def test_phony_script_dependency_is_not_a_midl_action(self):
        out, outputs = self.graph()
        unrelated = out / "gen/unrelated.h"
        unrelated.write_bytes(b"unrelated header")
        graph = out / "toolchain.ninja"
        graph.write_text(graph.read_text() +
                         "build gen/unrelated.h: phony ../../build/toolchain/win/midl.py\n")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in [*outputs, unrelated, self.script]}
        with self.assertRaisesRegex(ValueError, "edge ownership"):
            self.apply()
        for path, state in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), state)
        self.assertFalse((self.src / repair.RECORD).exists())

    def test_midl_rule_and_command_identity_are_required(self):
        out, outputs = self.graph()
        graph = out / "toolchain.ninja"
        original = graph.read_text()
        for content in (
                original.replace(RULE, ""),
                original.replace(RULE_COMMAND, "python3 unrelated.py"),
                original.replace(RULE_COMMAND, "python3 echo.py ../../build/toolchain/win/midl.py environment.x64"),
                original.replace("ACTION //chrome/elevation_service", "ACTION //other"),
                original.replace("  restat = 1", "  restat = 0"),
                original.replace("  command = ", "  command = echo\n  command = "),
                original + "  command = python3 unrelated.py\n",
                original.replace(" | ../../build/toolchain/win/midl.py", " ../../build/toolchain/win/midl.py")):
            graph.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.apply()
            self.assertTrue(all(path.exists() for path in outputs))
            self.assertEqual(self.script.read_bytes(), ORIGINAL)

    def test_actual_format_rule_continuation_and_source_binding(self):
        names = ["gen/chrome/elevation_service/elevation_service_idl" + suffix
                 for suffix in (".h", "_i.c", ".tlb", ".dlldata.c", "_p.c")]
        out, outputs = self.graph(names=names)
        graph = out / "toolchain.ninja"
        graph.write_text(graph.read_text().replace(" | ../../", " ../../chrome/elevation_service/elevation_service_idl.idl | ../../")
                         .replace("build " + names[0] + " ", "build\t" + names[0] + " $\n    ") +
                         "  source_name_part = elevation_service_idl\n")
        self.assertEqual(repair._graph(self.src)[0], sorted(p.relative_to(out).as_posix() for p in outputs))

    def test_local_invalidation_is_once_and_preserves_future_unrelated_objects(self):
        out, outputs = self.graph()
        other = out / "unrelated.obj"
        other.write_bytes(b"object")
        placeholder = self.src / "third_party/win_build_output/a.tlb"
        placeholder.parent.mkdir(parents=True)
        placeholder.write_bytes(b"")
        future = time.time_ns() + 86400 * 10**9
        for path in [*outputs, other]:
            os.utime(path, ns=(future, future))
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in [other, placeholder]}
        first = self.apply()
        self.assertEqual(first["invalidated_outputs"], sorted(p.relative_to(out).as_posix() for p in outputs))
        self.assertTrue(all(not path.exists() for path in outputs))
        for path in outputs:
            path.write_bytes(b"native regenerated")
        record = self.src / repair.RECORD
        baseline = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in [*outputs, self.script, record]}
        self.assertEqual(self.apply()["invalidated_outputs"], [])
        for path, state in {**before, **baseline}.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), state)

    def test_repaired_script_without_record_and_reverted_script_fail_closed(self):
        out, outputs = self.graph()
        self.script.write_bytes(repair.transform(ORIGINAL))
        with self.assertRaisesRegex(ValueError, "lacks one-time"):
            self.apply()
        self.assertTrue(all(path.exists() for path in outputs))
        self.script.write_bytes(ORIGINAL)
        self.apply()
        self.script.write_bytes(ORIGINAL)
        for path in outputs:
            path.write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "reverted"):
            self.apply()
        self.assertTrue(all(path.read_bytes() == b"keep" for path in outputs))

    def test_record_publish_failure_cannot_claim_proof_or_repeat_invalidation(self):
        _, outputs = self.graph()
        with mock.patch.object(repair, "_record", side_effect=OSError("record failure")), \
                self.assertRaisesRegex(OSError, "record failure"):
            self.apply()
        self.assertEqual(self.script.read_bytes(), repair.transform(ORIGINAL))
        for path in outputs:
            path.write_bytes(b"later output")
        with self.assertRaisesRegex(ValueError, "lacks one-time"):
            self.apply()
        self.assertTrue(all(path.read_bytes() == b"later output" for path in outputs))

    def test_graph_output_and_record_links_are_rejected_before_deletion(self):
        out, outputs = self.graph()
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        for path in [out / "toolchain.ninja", outputs[-1], self.src / repair.RECORD]:
            original = path.read_bytes() if path.exists() else None
            for hard in (False, True):
                path.unlink(missing_ok=True)
                os.link(outside, path) if hard else path.symlink_to(outside)
                with self.subTest(path=path, hard=hard), self.assertRaisesRegex(ValueError, "linked or unsafe"):
                    self.apply()
                self.assertTrue(outputs[0].exists())
                self.assertEqual(self.script.read_bytes(), ORIGINAL)
                self.assertEqual(outside.read_bytes(), b"outside")
                path.unlink()
            if original is not None:
                path.write_bytes(original)

    def test_graph_escapes_unknown_paths_and_missing_includes_fail_closed(self):
        out, outputs = self.graph()
        graph = out / "toolchain.ninja"
        for line in ("subninja missing.ninja\n", "subninja ../../outside.ninja\n",
                     "build ../../source.h: midl | ../../build/toolchain/win/midl.py\n",
                     "build obj/unrelated.obj: midl | ../../build/toolchain/win/midl.py\n",
                     "build gen/$unknown.h: midl | ../../build/toolchain/win/midl.py\n"):
            graph.write_text(line)
            with self.subTest(line=line), self.assertRaises(ValueError):
                self.apply()
            self.assertTrue(all(path.exists() for path in outputs))
            self.assertEqual(self.script.read_bytes(), ORIGINAL)

    def test_malformed_record_is_rejected_without_repeat_invalidation(self):
        _, outputs = self.graph()
        self.apply()
        record = self.src / repair.RECORD
        value = json.loads(record.read_text())
        for path in outputs:
            path.write_bytes(b"native regenerated")
        for changed in (dict(value, after_sha256="unknown"), dict(value, outputs=[42]),
                        dict(value, outputs=["../../source.h"]), dict(value, graphs={"file_count": -1})):
            record.write_text(json.dumps(changed))
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.apply()
            self.assertTrue(all(path.read_bytes() == b"native regenerated" for path in outputs))

    def test_new_unproven_action_after_record_fails_without_deleting_outputs(self):
        out, _ = self.graph()
        self.apply()
        target = out / "gen/new.h"
        target.write_bytes(b"future unknown output")
        (out / "toolchain.ninja").write_text(graph_text(["gen/new.h"]))
        with self.assertRaisesRegex(ValueError, "without one-time"):
            self.apply()
        self.assertEqual(target.read_bytes(), b"future unknown output")

    @unittest.skipUnless(shutil.which("ninja"), "Ninja required for scheduling proof")
    def test_real_ninja_future_output_and_log_repair_then_clean_second_call(self):
        out, outputs = self.graph(include=False)
        action = out / "action.py"
        action.write_text("from pathlib import Path\n" +
                          "[Path(p).write_bytes(b'generated') for p in " +
                          repr([p.relative_to(out).as_posix() for p in outputs]) + "]\n")
        graph = out / "build.ninja"
        original_graph = "pool build_toolchain_action_pool\n  depth = 1\n" + graph.read_text()
        graph.write_text(original_graph)
        diagnostic_graph = out / "diagnostic.ninja"
        diagnostic_graph.write_text(original_graph.replace(RULE_COMMAND, os.sys.executable + " action.py"))
        def ninja(*args):
            return subprocess.run(["ninja", "-f", "diagnostic.ninja", *args], cwd=out,
                                  text=True, capture_output=True, check=True).stdout
        ninja()
        future = time.time_ns() + 86400 * 10**9
        for path in outputs:
            os.utime(path, ns=(future, future))
        log = out / ".ninja_log"
        lines = []
        for line in log.read_text().splitlines():
            fields = line.split("\t")
            if len(fields) == 5:
                fields[2] = str(future)
            lines.append("\t".join(fields))
        log.write_text("\n".join(lines) + "\n")
        self.assertIn("no work to do", ninja("-n"))
        self.apply()
        self.assertIn("action.py", ninja("-n", "-v"))
        ninja()
        self.apply()
        self.assertIn("no work to do", ninja("-n"))

    def test_concurrent_same_repair_does_not_rewrite_correct_bytes(self):
        repaired = repair.transform(ORIGINAL)
        timestamp = 123456789000
        def fsync(fd):
            self.script.write_bytes(repaired)
            os.utime(self.script, ns=(timestamp, timestamp))
        with mock.patch.object(repair.os, "fsync", side_effect=fsync), \
                mock.patch.object(repair.os, "replace") as publish, \
                self.assertRaisesRegex(ValueError, "lacks one-time"):
            self.apply()
        publish.assert_not_called()
        self.assertEqual(self.script.stat().st_mtime_ns, timestamp)


class WindowsMidlControlFlowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native midl mock ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.midl = load_midl()
        self.source = self.root / "third_party/win_build_output/midl/chrome/updater/app/server/win"
        self.source_arch = self.source / "x64"
        self.source_arch.mkdir(parents=True)
        self.names = ("updater.h", "updater_i.c", "updater_p.c", "updater.tlb")
        for name in self.names:
            (self.source_arch / name).write_bytes(msft_tlb() if name.endswith(".tlb") else GUID.encode())
        self.tlb = self.source_arch / "updater.tlb"
        self.env = self.root / "environment.x64"
        self.env.write_bytes(b"PATH=fixture\0\0")
        self.idl = self.root / "updater.idl.template"
        self.idl.write_text("PLACEHOLDER-GUID-" + GUID)
        self.native_calls = []
        self.parser_inputs = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Upstream intentionally uses bare open().read/write throughout this fixture.
        self.stack.enter_context(warnings.catch_warnings())
        warnings.simplefilter("ignore", ResourceWarning)
        real_parser = self.midl["get_tlb_contents"]
        def parser(path):
            self.parser_inputs.append(Path(path).read_bytes())
            return real_parser(path)
        self.midl["get_tlb_contents"] = parser
        real_mkdtemp = tempfile.mkdtemp
        self.midl["tempfile"] = SimpleNamespace(mkdtemp=lambda: real_mkdtemp(dir=self.root))

    def invoke(self, out="out/Default/gen/updater", *, dynamic=True, tlb=True, ignored=False,
               replacement=REPLACEMENT):
        directory = self.root / out
        directory.mkdir(parents=True, exist_ok=True)
        def open_input(path, *args, **kwargs):
            return builtins.open(self.env if path == "environment.x64" else path, *args, **kwargs)
        self.midl["open"] = open_input
        result = self.midl["main"](
            "environment.x64", str(self.source), str(directory),
            (("ignore_proxy_stub," if ignored else "") + f"PLACEHOLDER-GUID-{GUID}={replacement}")
            if dynamic else "none",
            "updater.tlb" if tlb else "none", "updater.h", "none", "updater_i.c",
            "updater_p.c", "clang-cl.exe", str(self.idl), "/env", "x64")
        return result, directory

    def mock_native(self, *, barrier=None, returncode=0, missing=None):
        def popen(args, **kwargs):
            self.native_calls.append((args, kwargs))
            if barrier:
                barrier.wait(timeout=10)
            directory = Path(args[args.index("/out") + 1])
            template = Path(args[-3]).read_text()
            self.assertNotIn("PLACEHOLDER-GUID-", template)
            for option in ("/h", "/iid", "/proxy", "/tlb"):
                if option in args:
                    filename = args[args.index(option) + 1]
                    if filename != missing:
                        (directory / filename).write_bytes(msft_tlb(template) if option == "/tlb"
                                                           else template.encode())
            self.assertTrue(kwargs["shell"])
            self.assertEqual(kwargs["env"], {"PATH": "fixture"})
            return SimpleNamespace(returncode=returncode, communicate=lambda: ("", None))
        self.stack.enter_context(mock.patch.object(self.midl["subprocess"], "Popen", side_effect=popen))

    def assert_valid_output(self, directory, replacement=REPLACEMENT):
        self.assertEqual((directory / "updater.h").read_bytes(), replacement.encode())
        self.assertEqual((directory / "updater_i.c").read_bytes(), replacement.encode())
        payload = (directory / "updater.tlb").read_bytes()
        self.assertGreater(len(payload), 8)
        self.assertEqual(payload[:8], b"MSFT\x02\x00\x01\x00")
        contents, _, _, guid_offset, _ = self.midl["get_tlb_contents"](str(directory / "updater.tlb"))
        self.assertEqual(self.midl["getguid"](contents, guid_offset), replacement.encode())
        self.assertIn(b"at a redacted point in time\n", payload)

    def test_original_reproduces_empty_tlb_guid_assertion_before_native_midl(self):
        self.midl = load_midl(ORIGINAL)
        self.tlb.write_bytes(b"")
        with mock.patch.object(self.midl["subprocess"], "Popen") as popen, self.assertRaises(AssertionError):
            self.invoke()
        popen.assert_not_called()

    def test_empty_tlb_skips_guid_parser_runs_real_run_midl_and_copies_mismatch(self):
        self.tlb.write_bytes(b"")
        before = self.tlb.stat()
        self.mock_native()
        code, directory = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(self.parser_inputs, [])
        self.assertEqual(len(self.native_calls), 1)
        self.assertEqual(self.native_calls[0][0][0], "midl")
        self.assertIn(REPLACEMENT, (directory / "updater.idl.idl").read_text())
        self.assert_valid_output(directory)
        self.assertEqual(self.tlb.read_bytes(), b"")
        self.assertEqual(self.tlb.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.tlb.stat().st_ino, before.st_ino)

    def test_equal_size_mtime_uses_deep_comparison_for_all_included_outputs(self):
        self.tlb.write_bytes(b"")
        self.mock_native()
        compare = self.midl["filecmp"].cmpfiles
        def cmpfiles(left, right, names, **kwargs):
            for name in names:
                for root in (left, right):
                    os.utime(Path(root) / name, ns=(123456789000, 123456789000))
                if not name.endswith(".tlb"):
                    self.assertEqual((Path(left) / name).stat().st_size, (Path(right) / name).stat().st_size)
            self.assertEqual(kwargs, {"shallow": False})
            return compare(left, right, names, **kwargs)
        with mock.patch.object(self.midl["filecmp"], "cmpfiles", side_effect=cmpfiles):
            code, directory = self.invoke()
        self.assertEqual(code, 0)
        self.assert_valid_output(directory)
        self.assertEqual((directory / "updater_p.c").read_bytes(), REPLACEMENT.encode())

    def test_ignored_proxy_retains_source_but_header_iid_tlb_use_native_bytes(self):
        self.tlb.write_bytes(b"")
        self.mock_native()
        code, directory = self.invoke(ignored=True)
        self.assertEqual(code, 0)
        self.assert_valid_output(directory)
        self.assertEqual((directory / "updater_p.c").read_bytes(), GUID.encode())

    def test_missing_native_output_still_fails_in_repaired_branch(self):
        self.tlb.write_bytes(b"")
        self.mock_native(missing="updater.h")
        with self.assertRaises(AssertionError):
            self.invoke()

    def test_nonempty_valid_tlb_keeps_guid_rewrite_and_native_verification(self):
        before = self.tlb.read_bytes()
        self.mock_native()
        code, directory = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(self.parser_inputs, [before])
        self.assertEqual(len(self.native_calls), 1)
        self.assert_valid_output(directory)
        self.assertEqual(self.tlb.read_bytes(), before)

    def test_nonempty_invalid_tlb_still_asserts_before_native_midl(self):
        for payload in (b"invalid", b"\0", b"MSFT\x02\x00\x01\x00"):
            with self.subTest(payload=payload):
                self.tlb.write_bytes(payload)
                with mock.patch.object(self.midl["subprocess"], "Popen") as popen, \
                        self.assertRaises((AssertionError, struct.error)):
                    self.invoke()
                popen.assert_not_called()
                self.assertEqual(self.tlb.read_bytes(), payload)

    def test_missing_tlb_keeps_existing_placeholder_and_native_generation_path(self):
        self.tlb.unlink()
        self.mock_native()
        code, directory = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(self.parser_inputs, [])
        self.assert_valid_output(directory)
        self.assertEqual(self.tlb.read_bytes(), b"")

    def test_missing_directory_keeps_existing_native_generation_path(self):
        shutil.rmtree(self.source_arch)
        self.mock_native()
        code, directory = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(self.parser_inputs, [])
        self.assert_valid_output(directory)
        self.assertEqual(self.tlb.read_bytes(), b"")

    def test_empty_non_tlb_file_does_not_disable_guid_parsing(self):
        self.tlb.write_bytes(b"invalid nonempty TLB")
        (self.source_arch / "updater.h").write_bytes(b"")
        with mock.patch.object(self.midl["subprocess"], "Popen") as popen, self.assertRaises(AssertionError):
            self.invoke()
        self.assertEqual(self.parser_inputs, [b"invalid nonempty TLB"])
        popen.assert_not_called()

    def test_native_failure_remains_failure_without_valid_output_claim(self):
        self.tlb.write_bytes(b"")
        self.mock_native(returncode=7)
        code, directory = self.invoke()
        self.assertEqual(code, 7)
        self.assertEqual(self.parser_inputs, [])
        self.assertEqual((directory / "updater.tlb").read_bytes(), b"")

    def test_no_tlb_and_no_dynamic_guids_keep_native_path(self):
        self.mock_native()
        code, _ = self.invoke(tlb=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.parser_inputs, [])
        self.assertNotIn("/tlb", self.native_calls[-1][0])
        self.idl.write_text(GUID)
        self.tlb.write_bytes(b"")
        code, directory = self.invoke(dynamic=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.parser_inputs, [])
        self.assert_valid_output(directory, GUID)

    def test_posix_valid_empty_and_missing_tlb_behaviors_are_unchanged(self):
        self.midl["sys"].platform = "linux"
        with mock.patch.object(self.midl["subprocess"], "Popen") as popen:
            code, directory = self.invoke()
            self.assertEqual(code, 0)
            self.assertNotEqual((directory / "updater.tlb").read_bytes(), self.tlb.read_bytes())
            self.tlb.write_bytes(b"")
            with self.assertRaises(AssertionError):
                self.invoke()
            self.tlb.unlink()
            code, _ = self.invoke()
            self.assertEqual(code, 1)
            self.assertFalse(self.tlb.exists())
            popen.assert_not_called()

    def test_parallel_actions_share_placeholder_without_deletion_or_mutation(self):
        self.tlb.write_bytes(b"")
        before = self.tlb.stat()
        self.mock_native(barrier=threading.Barrier(4))
        real_unlink = os.unlink
        def unlink(path, *args, **kwargs):
            self.assertNotEqual(Path(path), self.tlb)
            return real_unlink(path, *args, **kwargs)
        replacements = [str(uuid.uuid5(uuid.NAMESPACE_URL, str(i))).upper() for i in range(4)]
        with mock.patch.object(os, "unlink", side_effect=unlink), ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: self.invoke(out=f"out/Default/gen/action-{i}",
                                                        replacement=replacements[i]), range(4)))
        self.assertEqual(self.parser_inputs, [])
        self.assertEqual(len(self.native_calls), 4)
        for (code, directory), replacement in zip(results, replacements):
            self.assertEqual(code, 0)
            self.assert_valid_output(directory, replacement)
        self.assertEqual(self.tlb.read_bytes(), b"")
        self.assertEqual(self.tlb.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.tlb.stat().st_ino, before.st_ino)


class WindowsMidlPrepareTest(unittest.TestCase):
    def setUp(self):
        self.support = prepare_tests.PrepareRestoredBuildTest()
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.work, self.src, self.out = self.support.work, self.support.src, self.support.out

    def fixture(self, platform="windows", arch="x64"):
        receipt = self.support.fixture(platform, arch)
        if platform == "windows":
            from tools.tests.test_fetch_upstream_cache import pinned_windows153_source

            repo = self.support.fixture_repo
            path = repo / "build/upstream-cache.json"
            source_manifest = json.loads(path.read_text())
            source_manifest["sources"]["windows"] = pinned_windows153_source(repo)
            self.support.write(path, json.dumps(source_manifest))
            self.support.write(self.src / "chrome/VERSION", "MAJOR=153\nMINOR=0\nBUILD=8010\nPATCH=47\n")
            identity, _, manifest = restore.identities(repo, platform, arch)
            receipt.update(identity=identity, manifest=manifest,
                           original_args=restore.source_args(self.src, identity))
            self.support.write(self.src / restore.MARKER, json.dumps(receipt))
            original_prepare = prepare.prepare
            patcher = mock.patch.object(prepare, "prepare", side_effect=lambda *args, **kwargs:
                                        original_prepare(*args, repo=repo, **kwargs))
            patcher.start()
            self.addCleanup(patcher.stop)
        return receipt

    def install(self, payload=ORIGINAL):
        return self.support.write(self.src / repair.SCRIPT, payload)

    def test_existing_marker_each_inspect_and_finish_rechecks_without_global_invalidation(self):
        self.fixture()
        self.support.deps({"obj/keep.obj": ["../../include/a.h"]})
        with self.support.native_context("windows", "x64"):
            first = prepare.prepare(self.work, "windows", "x64")
            self.assertEqual(first["windows_midl"]["status"], "no_repair_targets")
            marker = self.src / prepare.MARKER
            old = json.loads(marker.read_text())
            del old["windows_midl"]
            marker.write_text(json.dumps(old))
            keep = self.support.object("keep.obj")
            product = self.support.write(self.out / "chrome.dll", b"MZpreserved")
            generated = self.support.write(self.out / "gen/unrelated.h", b"generated")
            self.support.write(self.out / ".ninja_log", "# ninja log v5\n0\t1\t1\tgen/unrelated.h\tabc\n")
            baseline = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (
                keep, product, generated, *(self.out / name for name in prepare.METADATA))}
            script = self.install()
            for index, phase in enumerate(("inspect", "finish", "inspect", "finish", "finish")):
                with self.subTest(phase=phase):
                    result = prepare.prepare(self.work, "windows", "x64", phase=phase)
                    self.assertEqual(script.read_bytes(), repair.transform(ORIGINAL))
                    self.assertEqual(result["windows_midl"]["status"], "verified" if index else "repaired")
                    self.assertEqual(result["windows_midl"]["before_sha256"],
                                     repair.REPAIRED_SHA256 if index else repair.ORIGINAL_SHA256)
                    self.assertEqual(result["windows_midl"]["after_sha256"], repair.REPAIRED_SHA256)
                    self.assertFalse(result["needs_invalidation"])
                    self.assertFalse(result["generators_changed"])
                    if phase == "finish":
                        for name in ("tool_swap_invalidations", "toolchain_invalidated_outputs", "generator_rechecks"):
                            self.assertEqual(result["counters"][name], 0)
                        self.assertEqual(result["dependencies"]["removed_outputs"], 0)
                    for path, expected in baseline.items():
                        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)
            before = script.stat().st_mtime_ns
            again = prepare.prepare(self.work, "windows", "x64")
            self.assertEqual(again["windows_midl"]["status"], "verified")
            self.assertEqual(script.stat().st_mtime_ns, before)

    def test_existing_marker_unknown_script_fails_each_phase_with_report_and_no_writes(self):
        self.fixture()
        with self.support.native_context("windows", "x64"):
            prepare.prepare(self.work, "windows", "x64")
            marker = (self.src / prepare.MARKER).read_bytes()
            keep = self.support.object("keep.obj")
            for phase in ("inspect", "finish"):
                with self.subTest(phase=phase):
                    script = self.install(b"# unknown\n")
                    with self.assertRaisesRegex(ValueError, "unknown restored Windows MIDL"):
                        prepare.prepare(self.work, "windows", "x64", phase=phase)
                    report = json.loads((self.work / "upstream-cache-preparation.json").read_text())
                    self.assertFalse(report["ready_for_gn"])
                    self.assertEqual(report["operation"], "repair_windows_midl")
                    self.assertIn(hashlib.sha256(script.read_bytes()).hexdigest(), report["error"])
                    self.assertEqual((self.src / prepare.MARKER).read_bytes(), marker)
                    self.assertEqual(script.read_bytes(), b"# unknown\n")
                    self.assertTrue(keep.exists())

    def test_existing_marker_pre_repaired_script_without_proof_is_rejected(self):
        self.fixture()
        with self.support.native_context("windows", "x64"):
            prepare.prepare(self.work, "windows", "x64")
            script = self.install(repair.transform(ORIGINAL))
            marker = (self.src / prepare.MARKER).read_bytes()
            before = script.stat().st_mtime_ns
            for phase in ("inspect", "finish"):
                with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, "lacks one-time"):
                    prepare.prepare(self.work, "windows", "x64", phase=phase)
                self.assertEqual(script.stat().st_mtime_ns, before)
                self.assertEqual((self.src / prepare.MARKER).read_bytes(), marker)

    def test_existing_marker_only_declared_midl_outputs_are_invalidated_once(self):
        self.fixture()
        with self.support.native_context("windows", "x64"):
            prepare.prepare(self.work, "windows", "x64")
            self.install()
            names = ["gen/midl/a.h", "gen/midl/a_i.c", "gen/midl/a.tlb"]
            graph = graph_text(names)
            self.support.write(self.out / "build.ninja", graph)
            outputs = [self.support.write(self.out / name, b"stale") for name in names]
            obj = self.support.object("unrelated.obj")
            self.support.deps({"obj/unrelated.obj": ["../../include/a.h"]})
            before = obj.stat().st_mtime_ns
            inspected = prepare.prepare(self.work, "windows", "x64", phase="inspect")
            self.assertEqual(inspected["windows_midl"]["invalidated_outputs"], sorted(names))
            self.assertTrue(all(not path.exists() for path in outputs))
            for path in outputs:
                path.write_bytes(b"regenerated")
            finished = prepare.prepare(self.work, "windows", "x64")
            self.assertEqual(finished["windows_midl"]["invalidated_outputs"], [])
            self.assertEqual(finished["counters"]["toolchain_invalidated_outputs"], 0)
            self.assertEqual(finished["counters"]["generator_rechecks"], 0)
            self.assertEqual(obj.stat().st_mtime_ns, before)
            self.assertTrue(all(path.read_bytes() == b"regenerated" for path in outputs))

    def test_unverified_receipt_fails_before_midl_repair(self):
        self.fixture()
        script = self.install()
        with self.support.native_context("windows", "x64"), \
                mock.patch.object(prepare, "verify_restored", side_effect=ValueError("bad receipt")), \
                mock.patch.object(prepare, "repair_windows_midl") as apply, self.assertRaises(ValueError):
            prepare.prepare(self.work, "windows", "x64")
        apply.assert_not_called()
        self.assertEqual(script.read_bytes(), ORIGINAL)

    def test_posix_prepare_does_not_call_helper_or_touch_midl(self):
        self.fixture("macos", "arm64")
        script = self.install(b"unknown posix MIDL script")
        before = script.stat().st_mtime_ns
        with self.support.native_context("macos", "arm64"), \
                mock.patch.object(prepare, "repair_windows_midl") as apply:
            for phase in ("inspect", "finish", "finish"):
                result = prepare.prepare(self.work, "macos", "arm64", phase=phase,
                                         repo=self.support.fixture_repo)
                self.assertNotIn("windows_midl", result)
            apply.assert_not_called()
        self.assertEqual(script.read_bytes(), b"unknown posix MIDL script")
        self.assertEqual(script.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
