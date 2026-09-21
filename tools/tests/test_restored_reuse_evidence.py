import hashlib
import io
import json
import os
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import restored_reuse_evidence as evidence
from tools import restore_upstream_cache as restore


NINJAS = [Path(f"/tmp/chromix-ninja-v{version}/ninja") for version in ("1.11.1", "1.12.1", "1.13.2")]
AVAILABLE = [path for path in NINJAS if path.is_file()]
XNNPACK_OBJECT = ("obj/third_party/xnnpack/f16-avgpool_arch=armv8.2-a+fp16/"
                  "f16-avgpool-9p-minmax-neonfp16arith.o")
UNSUPPORTED_OBJECT_NAMES = (
    "../escape.o", "/escape.o", "obj/../escape.o", "obj//a.o", "./obj/a.o",
    "C:/escape.obj", "obj\\escape.obj", "'obj/a.o'", '"obj/a.o"', "'obj/space name.o'", "obj/$a.o",
    "obj/lib.a:member.o", "obj/lib.a(member.o)", "'obj/lib.a(member.o)'", "obj/é.o",
)


def elf_object(machine=183):
    ident = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    return ident + struct.pack("<HHIQQQIHHHHHH", 1, machine, 1,
                               0, 0, 0, 0, 64, 0, 0, 0, 0, 0)


class RestoredReuseEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.out = self.work / "src/out/Default"
        self.out.mkdir(parents=True)
        self.ninja = self.root / "ninja"
        self.ninja.write_bytes(b"fixture, never executed")
        self.receipt = {"identity": {"head_sha": "a" * 40, "platform": "linux", "arch": "x64"},
                        "manifest": {"sha256": "b" * 64, "path": "/original/manifest.json"}}
        self.verify = mock.patch.object(restore, "verify_restored", return_value=self.receipt).start()
        self.addCleanup(mock.patch.stopall)
        self.inputs = ["obj/a.o", "obj/b.obj", "../../source.cc"]
        self.version = 5
        self.make_log()
        self.real_query = evidence.query
        self.query = mock.patch.object(evidence, "query", side_effect=self.query_result).start()

    def query_result(self, ninja, out, args, limit):
        if args == ["--version"]:
            return b"1.11.1\n"
        self.assertEqual(args[:2], ["-t", "inputs"])
        self.assertEqual(ninja, self.ninja)
        self.assertEqual(out, self.out)
        return ("\n".join(self.inputs) + "\n").encode() if self.inputs else b""

    def make_log(self, names=("obj/a.o", "obj/b.obj"), *, extra=()):
        self.records = []
        for index, name in enumerate((*names, *extra)):
            path = self.out / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"tiny-object")
            mtime = 1_700_000_000_000_000_000 + index
            os.utime(path, ns=(mtime, mtime))
            self.records.append(f"{10 + index}\t{20 + index}\t{mtime}\t{name}\tdeadbeef\n")
        self.log = self.out / ".ninja_log"
        self.log.write_text(f"# ninja log v{self.version}\n" + "".join(self.records))

    def architecture_fixture(self, files, *, platform="linux", arch="arm64"):
        self.receipt["identity"].update(platform=platform, arch=arch)
        self.inputs = list(files)
        self.make_log(names=self.inputs)
        for name, content in files.items():
            path = self.out / name
            info = path.stat()
            path.write_bytes(content)
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))

    def before(self, **kwargs):
        identity = self.receipt["identity"]
        return evidence.before(self.work, identity["platform"], identity["arch"], self.ninja, **kwargs)

    def after(self, code=0, **kwargs):
        identity = self.receipt["identity"]
        return evidence.after(self.work, identity["platform"], identity["arch"], self.ninja, exit_code=code, **kwargs)

    def result(self):
        return json.loads((self.work / "upstream-reuse/result.json").read_text())

    def cli(self, phase, code=None):
        identity = self.receipt["identity"]
        args = [phase, "--workdir", str(self.work), "--platform", identity["platform"], "--arch", identity["arch"],
                "--ninja", str(self.ninja), "--target", "chrome"]
        if code is not None:
            args += ["--exit-code", str(code)]
        with mock.patch("sys.stdout", new=io.StringIO()), mock.patch("sys.stderr", new=io.StringIO()):
            return evidence.main(args)

    def test_retention_full_latest_records_and_scope_v5_v6_v7(self):
        for version in (5, 6, 7):
            with self.subTest(version=version):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.version = version
                self.make_log()
                with self.log.open("a") as stream:
                    stream.write(self.records[0].replace("10\t20\t", "30\t40\t"))
                before_log = self.log.read_bytes()
                baseline = self.before()
                self.assertFalse(baseline["retention_proven"])
                sample = baseline["samples"][0]
                self.assertEqual(sample["record"], {"start": 30, "end": 40,
                    "mtime": 1_700_000_000_000_000_000, "output": "obj/a.o", "hash": "deadbeef", "version": version})
                self.assertEqual(sample["file"]["sha256"], hashlib.sha256(b"tiny-object").hexdigest())
                self.assertEqual(self.log.read_bytes(), before_log)
                report = self.after()
                self.assertEqual(report["retained_count"], 2)
                self.assertTrue(report["retention_proven"])
                self.assertTrue(report["invocation_successful"])
                self.assertTrue(report["upstream_source_verified"])
                self.assertFalse(report["original_donor_records_verified"])
                self.assertEqual(report["scope"], evidence.SCOPE)
                self.assertLess(len(json.dumps(report)), 16 * 1024)
                self.verify.assert_called_with(self.work, "linux", "x64", repo=evidence.REPO)

    def test_windows_arm64_retention_keeps_baseline_and_does_not_claim_elf_target_evidence(self):
        self.architecture_fixture({"obj/target.obj": b"\x64\xaa" + b"coff-target" * 8,
                                   "clang_x64/obj/host.obj": b"\x64\x86" + b"coff-host" * 8},
                                  platform="windows", arch="arm64")
        baseline = self.before()
        path = self.work / "upstream-reuse/baseline.json"
        raw = path.read_bytes()
        self.assertFalse(self.after(124)["retention_proven"])
        self.assertEqual(self.before(), baseline)
        report = self.after()
        self.assertTrue(report["retention_proven"])
        self.assertEqual(report["retained_count"], 2)
        self.assertFalse(report["architecture_evidence"]["target_retention_proven"])
        self.assertEqual(report["architecture_evidence"]["retained_by_arch"],
                         {"x64": 0, "arm64": 0, "unknown": 2})
        self.assertEqual(path.read_bytes(), raw)
        self.before()
        with self.log.open("a") as stream:
            stream.write(self.records[0])
        report = self.after()
        self.assertEqual(report["retained_count"], 1)
        self.assertEqual(report["disqualified"], {"obj/target.obj": "appended_record_repeated"})
        self.assertEqual(path.read_bytes(), raw)

    def test_mixed_elf_architectures_ignore_paths_and_preserve_baseline_schema(self):
        self.architecture_fixture({"clang_x64/obj/target.o": elf_object(),
                                   "obj/arm64/host.o": elf_object(62),
                                   "obj/bitcode.o": b"BC\xc0\xdeaarch64-unknown-linux-gnu"})
        baseline = self.before()
        path = self.work / "upstream-reuse/baseline.json"
        raw, info = path.read_bytes(), path.stat()
        self.assertNotIn("architecture_evidence", baseline)
        self.assertEqual(baseline["schema_version"], 1)
        for sample in baseline["samples"]:
            self.assertEqual(set(sample), {"output", "record", "file"})
            self.assertEqual(set(sample["file"]), {"sha256", "size", "mtime_ns"})
        self.assertEqual(self.result()["architecture_evidence"], {
            "schema_version": 1, "method": "linux-elf64-le-et-rel", "retained_outputs": {},
            "retained_by_arch": {"x64": 0, "arm64": 0, "unknown": 0},
            "target_retained_count": 0, "target_retention_proven": False})
        for _ in range(2):
            report = self.after()
            data = report["architecture_evidence"]
            self.assertEqual(report["retained_count"], 3)
            self.assertTrue(report["retention_proven"])
            self.assertEqual(data["retained_outputs"], {"clang_x64/obj/target.o": "arm64",
                "obj/arm64/host.o": "x64", "obj/bitcode.o": "unknown"})
            self.assertEqual(data["retained_by_arch"], {"x64": 1, "arm64": 1, "unknown": 1})
            self.assertEqual(data["target_retained_count"], 1)
            self.assertTrue(data["target_retention_proven"])
            self.assertEqual(report["baseline_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(self.before(), baseline)
            pending = self.result()["architecture_evidence"]
            self.assertEqual(pending["retained_outputs"], data["retained_outputs"])
            self.assertFalse(pending["target_retention_proven"])
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_mtime_ns, info.st_mtime_ns)

    def test_128_host_samples_do_not_count_unsampled_arm64_or_resample(self):
        files = {f"clang_x64/obj/host{index:03}.o": elf_object(62) for index in range(128)}
        files["obj/target.o"] = elf_object()
        self.architecture_fixture(files)
        baseline = self.before()
        raw = (self.work / "upstream-reuse/baseline.json").read_bytes()
        self.assertEqual(len(baseline["samples"]), 128)
        self.assertNotIn("obj/target.o", {sample["output"] for sample in baseline["samples"]})
        for _ in range(2):
            report = self.after()
            self.assertEqual(report["retained_count"], 128)
            self.assertTrue(report["retention_proven"])
            self.assertEqual(report["architecture_evidence"]["retained_by_arch"],
                             {"x64": 128, "arm64": 0, "unknown": 0})
            self.assertEqual(report["architecture_evidence"]["target_retained_count"], 0)
            self.assertFalse(report["architecture_evidence"]["target_retention_proven"])
            self.assertEqual(self.before(), baseline)
        self.assertEqual((self.work / "upstream-reuse/baseline.json").read_bytes(), raw)

    def test_nonrel_invalid_headers_and_bitcode_remain_unknown(self):
        files = {"obj/short.o": elf_object()[:63], "obj/other-machine.o": elf_object(40),
                 "obj/raw-bitcode.o": b"BC\xc0\xdeaarch64-unknown-linux-gnu" + b"x" * 64,
                 "obj/wrapped-bitcode.o": b"\xde\xc0\x17\x0baarch64-unknown-linux-gnu" + b"x" * 64}
        for name, offset, value in (("class", 4, b"\x01"), ("endian", 5, b"\x02"),
                ("ident-version", 6, b"\x02"), ("executable", 16, b"\x02\0"),
                ("shared", 16, b"\x03\0"), ("version", 20, b"\x02\0\0\0"),
                ("header-size", 52, b"\x3f\0"), ("magic", 0, b"nope")):
            content = bytearray(elf_object())
            content[offset:offset + len(value)] = value
            files[f"obj/{name}.o"] = content
        self.architecture_fixture(files)
        self.before()
        report = self.after()
        self.assertEqual(report["retained_count"], len(files))
        self.assertTrue(report["retention_proven"])
        self.assertEqual(report["architecture_evidence"]["retained_by_arch"],
                         {"x64": 0, "arm64": 0, "unknown": len(files)})
        self.assertFalse(report["architecture_evidence"]["target_retention_proven"])

    def test_architecture_proof_requires_linux_matching_target_and_successful_after(self):
        for platform, arch, machine in (("linux", "arm64", 183), ("linux", "x64", 62),
                                        ("linux", "arm64", 62), ("macos", "arm64", 183)):
            with self.subTest(platform=platform, arch=arch, machine=machine):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.architecture_fixture({"obj/a.o": elf_object(machine)}, platform=platform, arch=arch)
                matching = platform == "linux" and evidence.ELF_ARCHITECTURES[machine] == arch
                for code in (1, 124, -9, 0):
                    self.before()
                    self.assertFalse(self.result()["architecture_evidence"]["target_retention_proven"])
                    report = self.after(code)
                    data = report["architecture_evidence"]
                    self.assertEqual(data["target_retained_count"], int(matching))
                    self.assertEqual(data["target_retention_proven"], matching and code == 0)
                    self.assertEqual(report["retention_proven"], code == 0)
                    if platform != "linux":
                        self.assertEqual(data["retained_by_arch"]["unknown"], 1)

    def test_elf_header_and_body_changes_with_same_size_and_mtime_disqualify(self):
        for changed in (elf_object(62) + b"old-body", elf_object() + b"new-body"):
            with self.subTest(header_changed=changed[:64] != elf_object()):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                original = elf_object() + b"old-body"
                self.architecture_fixture({"obj/a.o": original})
                self.before()
                path = self.out / "obj/a.o"
                info = path.stat()
                path.write_bytes(changed)
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                report = self.after()
                self.assertEqual(report["samples"][0]["status"], "content_changed")
                self.assertEqual(report["object_hash_bytes"], len(changed))
                self.assertEqual(report["architecture_evidence"]["retained_outputs"], {})
                self.assertEqual(report["architecture_evidence"]["target_retained_count"], 0)
                path.write_bytes(original)
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                self.before()
                self.assertFalse(self.after()["architecture_evidence"]["target_retention_proven"])

    def test_elf_architecture_disqualification_keeps_all_existing_log_and_graph_guards(self):
        for kind, reason in (("repeat", "appended_record_repeated"), ("changed", "appended_record_changed"),
                             ("alias", "unsupported_appended_output"), ("prefix", "log_prefix_mismatch"),
                             ("graph", "target_inputs_changed")):
            with self.subTest(kind=kind):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.architecture_fixture({"obj/a.o": elf_object()})
                self.before()
                self.assertTrue(self.after()["architecture_evidence"]["target_retention_proven"])
                self.before()
                raw = self.log.read_bytes()
                if kind == "graph":
                    self.inputs.append("../../new-source.cc")
                elif kind == "prefix":
                    self.log.write_text(self.log.read_text().replace("deadbeef", "beefdead"))
                else:
                    with self.log.open("a") as stream:
                        stream.write(self.records[0] if kind == "repeat" else
                                     self.records[0].replace("10\t20\t", "30\t40\t") if kind == "changed" else
                                     "1\t2\t3\t/absolute-alias.o\tabc\n")
                report = self.after()
                self.assertEqual(report["disqualified"], {"obj/a.o": reason})
                self.assertEqual(report["architecture_evidence"]["retained_outputs"], {})
                self.assertEqual(report["architecture_evidence"]["target_retained_count"], 0)
                self.assertFalse(report["architecture_evidence"]["target_retention_proven"])
                if kind == "prefix":
                    self.log.write_bytes(raw)
                if kind == "graph":
                    self.inputs.pop()
                self.before()
                self.assertFalse(self.after()["architecture_evidence"]["target_retention_proven"])

    def test_legacy_architecture_extension_absence_preserves_baseline_and_disqualification(self):
        self.architecture_fixture({"obj/a.o": elf_object(), "obj/b.o": elf_object()})
        baseline = self.before()
        baseline_path = self.work / "upstream-reuse/baseline.json"
        state_path = self.work / "upstream-reuse/result.json"
        raw, info = baseline_path.read_bytes(), baseline_path.stat()
        pending = self.result()
        del pending["architecture_evidence"]
        state_path.write_bytes(evidence.json_bytes(pending))
        self.assertTrue(self.after()["architecture_evidence"]["target_retention_proven"])
        self.before()
        with self.log.open("a") as stream:
            stream.write(self.records[0])
        previous = self.after()
        del previous["architecture_evidence"]
        state_path.write_bytes(evidence.json_bytes(previous))
        self.assertEqual(self.before(), baseline)
        report = self.after()
        self.assertEqual(report["disqualified"], {"obj/a.o": "appended_record_repeated"})
        self.assertEqual(report["architecture_evidence"]["retained_outputs"], {"obj/b.o": "arm64"})
        self.assertEqual(report["architecture_evidence"]["target_retained_count"], 1)
        self.assertEqual(report["baseline_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(baseline_path.read_bytes(), raw)
        self.assertEqual(baseline_path.stat().st_mtime_ns, info.st_mtime_ns)

    def test_architecture_classification_is_recomputed_not_carried_from_previous_result(self):
        for machine, forged_arch in ((183, "x64"), (62, "arm64")):
            with self.subTest(machine=machine, forged_arch=forged_arch):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.architecture_fixture({"obj/a.o": elf_object(machine)})
                baseline = self.before()
                previous = self.after()
                previous["architecture_evidence"] = evidence.architecture_report(
                    previous["source"], {"obj/a.o": forged_arch}, successful=True)
                evidence.state_valid(previous, baseline)
                (self.work / "upstream-reuse/result.json").write_bytes(evidence.json_bytes(previous))
                self.before()
                actual = evidence.ELF_ARCHITECTURES[machine]
                self.assertEqual(self.result()["architecture_evidence"]["retained_outputs"], {"obj/a.o": actual})
                self.assertEqual(self.after()["architecture_evidence"]["target_retention_proven"], machine == 183)

    def test_malformed_architecture_metadata_fails_closed_without_changing_baseline(self):
        self.architecture_fixture({"obj/a.o": elf_object()})
        self.before()
        previous = self.after()
        state_path = self.work / "upstream-reuse/result.json"
        baseline_path = self.work / "upstream-reuse/baseline.json"
        raw = baseline_path.read_bytes()
        mutations = [
            ((), None), (("schema_version",), 2), (("schema_version",), True),
            (("method",), "path-guess"), (("extra",), 0),
            (("retained_outputs",), []), (("retained_outputs",), {}),
            (("retained_outputs",), {"../outside.o": "arm64"}),
            (("retained_outputs", "obj/a.o"), "aarch64"), (("retained_outputs", "obj/a.o"), []),
            (("retained_by_arch",), []), (("retained_by_arch",), {"arm64": 1}),
            (("retained_by_arch", "other"), 0), (("retained_by_arch", "arm64"), True),
            (("retained_by_arch", "arm64"), -1), (("retained_by_arch", "arm64"), 129),
            (("retained_by_arch", "arm64"), 0), (("target_retained_count",), True),
            (("target_retained_count",), -1), (("target_retained_count",), 0),
            (("target_retention_proven",), 1), (("target_retention_proven",), False),
        ]
        for keys, value in mutations:
            with self.subTest(keys=keys, value=value):
                state = json.loads(evidence.json_bytes(previous))
                parent = state
                for key in ("architecture_evidence", *keys)[:-1]:
                    parent = parent[key]
                parent[("architecture_evidence", *keys)[-1]] = value
                state_path.write_bytes(evidence.json_bytes(state))
                self.assertEqual(self.cli("before"), 1)
                self.assertEqual(self.result()["status"], "error")
                self.assertFalse(self.result()["retention_proven"])
                self.assertEqual(baseline_path.read_bytes(), raw)
        for key in previous["architecture_evidence"]:
            with self.subTest(missing=key):
                state = json.loads(evidence.json_bytes(previous))
                del state["architecture_evidence"][key]
                state_path.write_bytes(evidence.json_bytes(state))
                self.assertEqual(self.cli("before"), 1)
        for keys, value in ((("samples",), []), (("samples",), None),
                            (("samples", 0, "status"), "content_changed"),
                            (("samples", 0, "file", "sha256"), "f" * 64),
                            (("samples", 0, "record", "hash"), "beefdead"),
                            (("retained_count",), True), (("retained_count",), 0),
                            (("exit_code",), True), (("exit_code",), 124)):
            with self.subTest(state_keys=keys, value=value):
                state = json.loads(evidence.json_bytes(previous))
                parent = state
                for key in keys[:-1]:
                    parent = parent[key]
                parent[keys[-1]] = value
                state_path.write_bytes(evidence.json_bytes(state))
                self.assertEqual(self.cli("before"), 1)
        state = json.loads(evidence.json_bytes(previous))
        state.update(disqualified={"obj/a.o": "content_changed"}, disqualified_count=1,
                     disqualification_reasons={"content_changed": 1})
        state_path.write_bytes(evidence.json_bytes(state))
        self.assertEqual(self.cli("before"), 1)
        self.assertEqual(baseline_path.read_bytes(), raw)
        state_path.write_bytes(evidence.json_bytes(previous))
        self.before()
        pending = self.result()
        pending["architecture_evidence"]["target_retention_proven"] = True
        state_path.write_bytes(evidence.json_bytes(pending))
        self.assertEqual(self.cli("after", 0), 1)

    def test_architecture_header_uses_single_full_hash_read_and_stable_file_check(self):
        self.architecture_fixture({"obj/a.o": elf_object() + b"x" * (1024 * 1024)})
        path = self.out / "obj/a.o"
        header = bytearray()
        original_open = Path.open
        opened = []

        def tracked_open(candidate, *args, **kwargs):
            opened.append(candidate)
            return original_open(candidate, *args, **kwargs)

        with mock.patch.object(Path, "open", tracked_open):
            record = evidence.file_record(path, header=header)
        self.assertEqual(opened, [path])
        self.assertEqual(header, elf_object())
        self.assertEqual(record["size"], 64 + 1024 * 1024)
        self.assertEqual(record["sha256"], hashlib.sha256(elf_object() + b"x" * (1024 * 1024)).hexdigest())
        regular = evidence.regular
        calls = 0

        def changing_regular(candidate):
            nonlocal calls
            calls += 1
            if calls == 2:
                info = candidate.stat()
                os.utime(candidate, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
            return regular(candidate)

        header = bytearray()
        with mock.patch.object(evidence, "regular", side_effect=changing_regular):
            with self.assertRaisesRegex(evidence.EvidenceError, "changed during hashing"):
                evidence.file_record(path, header=header)
        self.assertEqual(header, b"")

    def test_rebuild_only_start_end_changed_does_not_count(self):
        self.before()
        self.log.write_text(self.log.read_text() + "".join(
            line.replace("\t20\t", "\t80\t").replace("\t21\t", "\t81\t") for line in self.records))
        report = self.after()
        self.assertFalse(report["retention_proven"])
        self.assertEqual(report["status"], "unproven")
        self.assertEqual({item["status"] for item in report["samples"]}, {"appended_record_changed"})

    def test_content_change_even_with_same_size_and_mtime(self):
        self.before()
        path = self.out / "obj/a.o"
        info = path.stat()
        path.write_bytes(b"evil-object")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        report = self.after()
        self.assertEqual(report["retained_count"], 1)
        self.assertEqual(report["samples"][0]["status"], "content_changed")

    def test_size_or_mtime_change_excludes_object_without_hashing(self):
        for kind in ("size", "mtime"):
            with self.subTest(kind=kind):
                self.before()
                path = self.out / "obj/a.o"
                if kind == "size":
                    path.write_bytes(b"different size")
                else:
                    info = path.stat()
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
                with mock.patch.object(evidence, "file_record", wraps=evidence.file_record) as hashing:
                    report = self.after()
                self.assertEqual(report["samples"][0]["status"], "file_metadata_changed")
                self.assertEqual(hashing.call_count, 1)

    def test_out_of_graph_and_missing_files_or_records_never_sampled(self):
        self.make_log(extra=("obj/ignored.o",))
        (self.out / "obj/b.obj").unlink()
        (self.out / "obj/no-log.o").write_bytes(b"not logged")
        self.inputs.append("obj/no-log.o")
        baseline = self.before()
        self.assertEqual([item["output"] for item in baseline["samples"]], ["obj/a.o"])
        self.assertEqual(baseline["skipped"]["missing_record"], 1)
        self.assertEqual(baseline["skipped"]["missing_file"], 1)
        self.assertEqual(self.after()["retained_count"], 1)

    def test_objects_removed_from_graph_after_or_between_invocations(self):
        self.before()
        self.inputs = ["obj/b.obj"]
        report = self.after()
        self.assertEqual(report["retained_count"], 0)
        self.assertEqual(report["samples"][0]["status"], "not_in_target_inputs")
        self.before()
        report = self.after()
        self.assertEqual(report["retained_count"], 0)
        self.assertEqual(report["samples"][0]["status"], "not_in_target_inputs")
        self.assertEqual(report["disqualification_reasons"], {"not_in_target_inputs": 1, "target_inputs_changed": 1})

    def test_missing_after_files_or_records_are_not_retained(self):
        self.before()
        (self.out / "obj/a.o").unlink()
        # Replacing an original record breaks continuity even without a size change.
        self.log.write_text(self.log.read_text().replace("obj/b.obj", "obj/z.obj"))
        report = self.after()
        self.assertEqual([item["status"] for item in report["samples"]], ["log_prefix_mismatch"] * 2)
        self.assertFalse(report["retention_proven"])

    def test_log_reset_compaction_or_version_change_is_unproven(self):
        for kind in ("empty", "compacted", "version"):
            with self.subTest(kind=kind):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.make_log()
                self.log.write_text(self.log.read_text() + self.records[0])
                self.before()
                if kind == "empty":
                    self.log.write_text("# ninja log v5\n")
                elif kind == "compacted":
                    self.log.write_text("# ninja log v5\n" + "".join(self.records))
                else:
                    self.log.write_text(self.log.read_text().replace("log v5", "log v6"))
                report = self.after()
                self.assertFalse(report["retention_proven"])
                self.assertEqual({item["status"] for item in report["samples"]}, {"log_prefix_mismatch"})

    def test_resume_never_resets_baseline_or_counts_rebuilt_outputs(self):
        baseline = self.before()
        path = self.work / "upstream-reuse/baseline.json"
        raw, info = path.read_bytes(), path.stat()
        self.after(124)
        self.log.write_text(self.log.read_text() + self.records[0].replace("10\t20\t", "30\t40\t"))
        self.assertEqual(self.before(), baseline)
        report = self.after()
        self.assertEqual(report["retained_count"], 1)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_mtime_ns, info.st_mtime_ns)
        self.assertEqual(self.before(), baseline)
        self.assertEqual(self.result()["status"], "unproven")
        self.assertFalse(self.result()["retention_proven"])

    def test_resumed_workspace_relocation_and_new_run_are_supported(self):
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "101"}, clear=True):
            baseline = self.before()
            self.after(124)
        destination = self.root / "relocated"
        self.work.rename(destination)
        self.work = destination
        self.out = self.work / "src/out/Default"
        self.receipt["manifest"]["path"] = "/another/manifest.json"
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "102"}, clear=True):
            self.assertEqual(self.before(), baseline)
            report = self.after()
        self.assertEqual(report["baseline_run"]["GITHUB_RUN_ID"], "101")
        self.assertEqual(report["run"]["GITHUB_RUN_ID"], "102")
        self.assertTrue(report["retention_proven"])

    def test_nonzero_exit_is_incomplete_even_when_objects_unchanged(self):
        for code in (1, 124, -9):
            with self.subTest(code=code):
                self.before()
                report = self.after(code)
                self.assertEqual(report["status"], "incomplete")
                self.assertFalse(report["retention_proven"])
                self.assertFalse(report["invocation_successful"])
                self.assertEqual(report["retained_count"], 2)
                self.assertEqual(report["exit_code"], code)

    def test_zero_samples_is_unproven(self):
        self.inputs = []
        self.assertEqual(self.before()["samples"], [])
        self.assertEqual(self.after()["status"], "unproven")

    def test_receipt_identity_change_fails_closed_and_preserves_baseline(self):
        self.before()
        path = self.work / "upstream-reuse/baseline.json"
        raw = path.read_bytes()
        self.receipt["identity"]["head_sha"] = "c" * 40
        self.assertEqual(self.cli("before"), 1)
        self.assertEqual(self.result()["status"], "error")
        self.assertFalse(self.result()["retention_proven"])
        self.assertEqual(path.read_bytes(), raw)
        self.verify.side_effect = restore.Miss("invalid receipt")
        self.assertEqual(self.cli("after", 0), 1)

    def test_local_verification_errors_return_nonzero_without_traceback(self):
        self.verify.side_effect = restore.LocalError("unsupported target")
        self.assertEqual(self.cli("before"), 1)
        self.assertFalse(self.result()["retention_proven"])
        self.assertEqual(self.result()["status"], "error")

    def test_target_mismatch_or_unpaired_after_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            self.after()
        self.before()
        with self.assertRaisesRegex(evidence.EvidenceError, "identity, targets"):
            self.after(targets=("other",))
        self.after()
        self.assertEqual(self.cli("after", 0), 1)
        self.assertFalse(self.result()["retention_proven"])

    def test_repeated_targets_are_normalized_for_windows(self):
        first = evidence.before(self.work, "windows", "x64", self.ninja, targets=("chrome", "other", "chrome"))
        report = evidence.after(self.work, "windows", "x64", self.ninja, targets=("other", "chrome"), exit_code=0)
        self.assertEqual(first["targets"], ["chrome", "other"])
        self.assertTrue(report["retention_proven"])

    def test_raw_equals_unselected_log_append_preserves_selected_object(self):
        for version in (5, 6, 7):
            with self.subTest(version=version):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.version = version
                self.make_log(names=("keep.o",), extra=(XNNPACK_OBJECT,))
                self.inputs = ["keep.o", f"'{XNNPACK_OBJECT}'"]
                baseline = self.before()
                baseline_path = self.work / "upstream-reuse/baseline.json"
                saved = baseline_path.read_bytes()
                self.assertEqual([sample["output"] for sample in baseline["samples"]], ["keep.o"])
                self.assertEqual(baseline["membership"]["excluded_object_inputs"], 1)
                self.assertEqual(baseline["log"]["unselected_unsupported_object_records"], 0)
                with self.log.open("a") as stream:
                    stream.write(self.records[1].replace("11\t21\t", "31\t41\t"))
                report = self.after()
                self.assertEqual(report["retained_count"], 1)
                self.assertTrue(report["retention_proven"])
                self.assertEqual(report["disqualified"], {})
                self.assertTrue(report["log"]["prefix_matches_previous"])
                self.assertEqual(report["log"]["unselected_unsupported_object_records"], 0)
                self.assertEqual(self.before(), baseline)
                self.assertTrue(self.after()["retention_proven"])
                self.assertEqual(baseline_path.read_bytes(), saved)

    def test_selected_raw_equals_output_append_still_permanently_disqualifies(self):
        for repeated in (False, True):
            with self.subTest(repeated=repeated):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.make_log(names=("keep.o", XNNPACK_OBJECT))
                self.inputs = ["keep.o", XNNPACK_OBJECT]
                baseline = self.before()
                self.assertEqual({sample["output"] for sample in baseline["samples"]}, set(self.inputs))
                with self.log.open("a") as stream:
                    stream.write(self.records[1] if repeated else
                                 self.records[1].replace("11\t21\t", "31\t41\t"))
                report = self.after()
                reason = "appended_record_repeated" if repeated else "appended_record_changed"
                self.assertEqual(report["disqualified"], {XNNPACK_OBJECT: reason})
                self.assertEqual(report["retained_count"], 1)
                self.assertEqual(self.before(), baseline)
                self.assertEqual(self.after()["disqualified"], {XNNPACK_OBJECT: reason})

    def test_equals_paths_keep_canonical_and_shell_quoted_input_restrictions(self):
        self.assertEqual(evidence.object_name(XNNPACK_OBJECT), XNNPACK_OBJECT)
        for name in (f"'{XNNPACK_OBJECT}'", f'"{XNNPACK_OBJECT}"', f"/{XNNPACK_OBJECT}",
                     f"../{XNNPACK_OBJECT}", f"./{XNNPACK_OBJECT}", f"obj/../{XNNPACK_OBJECT}",
                     XNNPACK_OBJECT.replace("obj/", "obj//", 1), f"C:/{XNNPACK_OBJECT}",
                     XNNPACK_OBJECT.replace("/", "\\")):
            with self.subTest(name=name):
                with self.assertRaises(evidence.EvidenceError):
                    evidence.object_name(name)
        self.inputs = ["obj/a.o", f"'{XNNPACK_OBJECT}'", f'"{XNNPACK_OBJECT}"']
        with mock.patch.object(evidence, "object_path", wraps=evidence.object_path) as paths:
            baseline = self.before()
            report = self.after()
        self.assertEqual({call.args[1] for call in paths.call_args_list}, {"obj/a.o"})
        self.assertEqual(baseline["membership"]["excluded_object_inputs"], 2)
        self.assertEqual(baseline["membership"]["sha256"],
                         hashlib.sha256(("\n".join(self.inputs) + "\n").encode()).hexdigest())
        self.assertEqual(report["retained_count"], 1)

    def test_unsupported_inputs_excluded_without_normalization_or_file_access(self):
        for name in UNSUPPORTED_OBJECT_NAMES:
            for safe in (False, True):
                with self.subTest(name=name, safe=safe):
                    shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                    self.inputs = [name, "../../source.cc", *(["obj/a.o"] if safe else [])]
                    with mock.patch.object(evidence, "object_path", wraps=evidence.object_path) as paths:
                        baseline = self.before()
                        report = self.after()
                    self.assertEqual({call.args[1] for call in paths.call_args_list}, {"obj/a.o"} if safe else set())
                    self.assertEqual([sample["output"] for sample in baseline["samples"]], ["obj/a.o"] if safe else [])
                    membership = baseline["membership"]
                    self.assertEqual(membership["input_count"], len(self.inputs))
                    self.assertEqual(membership["object_count"], int(safe))
                    self.assertEqual(membership["excluded_object_inputs"], 1)
                    self.assertEqual(membership["excluded_object_input_examples"], [
                        {"origin": "ninja -t inputs", "line": 1, "path_escaped": ascii(name)}])
                    self.assertEqual(membership["sha256"], hashlib.sha256(("\n".join(self.inputs) + "\n").encode()).hexdigest())
                    self.assertEqual(report["retained_count"], int(safe))
                    self.assertEqual(report["status"], "retained" if safe else "unproven")
                    self.assertEqual(report["retention_proven"], safe)
                    with self.assertRaises(evidence.EvidenceError):
                        evidence.object_name(name)

    def test_unsupported_sample_and_record_names_still_fail_baseline_validation(self):
        baseline = self.before()
        path = self.work / "upstream-reuse/baseline.json"
        original = path.read_text()
        for name in UNSUPPORTED_OBJECT_NAMES:
            for field in ("output", "record", "both"):
                with self.subTest(name=name, field=field):
                    data = json.loads(original)
                    if field != "record":
                        data["samples"][0]["output"] = name
                    if field != "output":
                        data["samples"][0]["record"]["output"] = name
                    content = json.dumps(data)
                    path.write_text(content)
                    with self.assertRaises(evidence.EvidenceError):
                        evidence.baseline_valid(data, baseline["source"], baseline["targets"])
                    self.assertEqual(self.cli("before"), 1)
                    self.assertFalse(self.result()["retention_proven"])
                    self.assertEqual(path.read_text(), content)

    def test_preexisting_unsupported_log_outputs_are_diagnostic_only_and_prefix_stays_complete(self):
        for version in (5, 6, 7):
            with self.subTest(version=version):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.version = version
                self.make_log()
                self.inputs = ["obj/a.o"]
                with self.log.open("a") as stream:
                    for name in UNSUPPORTED_OBJECT_NAMES:
                        line = f"1\t2\t3\t{name}\tabc\n"
                        self.assertEqual(evidence.parse_record(line, version)["output"], name)
                        stream.write(line)
                baseline = self.before()
                self.assertEqual([sample["output"] for sample in baseline["samples"]], ["obj/a.o"])
                self.assertEqual(baseline["log"]["unselected_unsupported_object_records"], len(UNSUPPORTED_OBJECT_NAMES))
                self.assertEqual(len(baseline["log"]["unselected_unsupported_object_examples"]), evidence.MAX_PATH_EXAMPLES)
                self.assertEqual(baseline["log"]["unselected_unsupported_object_examples"][0],
                                 {"origin": ".ninja_log", "line": 4, "path_escaped": ascii(UNSUPPORTED_OBJECT_NAMES[0])})
                self.assertEqual(baseline["log"]["sha256"], hashlib.sha256(self.log.read_bytes()).hexdigest())
                report = self.after()
                self.assertEqual(report["retained_count"], 1)
                self.assertTrue(report["retention_proven"])
                self.assertEqual(report["log"]["unselected_unsupported_object_records"], len(UNSUPPORTED_OBJECT_NAMES))
                self.assertEqual(report["log"]["sha256"], hashlib.sha256(self.log.read_bytes()).hexdigest())
                self.log.write_text(self.log.read_text().replace("../escape.o", "../change.o"))
                self.before()
                self.assertEqual(self.result()["disqualified"], {"obj/a.o": "log_prefix_mismatch"})
                self.log.write_text(self.log.read_text().replace("../change.o", "../escape.o"))
                self.assertFalse(self.after()["retention_proven"])

    def test_appended_unsupported_outputs_permanently_disqualify_all_samples(self):
        for version in (5, 6, 7):
            for phase in ("before", "after"):
                for name in UNSUPPORTED_OBJECT_NAMES:
                    with self.subTest(version=version, phase=phase, name=name):
                        shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                        self.version = version
                        self.make_log()
                        # Even an identical unsupported record is unsafe when appended again.
                        line = f"1\t2\t0\t{name}\tabc\n"
                        with self.log.open("a") as stream:
                            stream.write(line)
                        baseline = self.before()
                        self.assertEqual(self.after()["retained_count"], 2)
                        self.assertEqual(self.before(), baseline)
                        with self.log.open("a") as stream:
                            stream.write(line)
                        with mock.patch.object(evidence, "object_path", side_effect=AssertionError("unexpected path access")):
                            self.before() if phase == "before" else self.after()
                        report = self.result()
                        expected = dict.fromkeys(("obj/a.o", "obj/b.obj"), "unsupported_appended_output")
                        self.assertEqual(report["disqualified"], expected)
                        self.assertEqual(report["disqualification_reasons"], {"unsupported_appended_output": 2})
                        self.assertEqual(report["retained_count"], 0)
                        self.assertFalse(report["retention_proven"])
                        self.assertTrue(report["log"]["prefix_matches_previous"])
                        self.assertEqual(report["log"]["unselected_unsupported_object_records"], 2)
                        self.assertEqual(self.before(), baseline)
                        self.assertEqual(self.after()["disqualified"], expected)
                        self.log.write_text(self.log.read_text()[:-len(line)])
                        self.before()
                        self.assertEqual(self.after()["disqualified"], expected)

    def test_unsupported_append_before_previous_observation_still_disqualifies(self):
        baseline = self.before()
        with self.log.open("a") as stream:
            stream.write("1\t2\t3\t../alias.o\tabc\n")
        pending_path = self.work / "upstream-reuse/result.json"
        pending = json.loads(pending_path.read_text())
        # Older collectors could record the suffix without disqualifying its aliases.
        pending["log"] = evidence.read_log(self.out, set())[1]
        pending_path.write_bytes(evidence.json_bytes(pending))
        report = self.after()
        self.assertGreater(pending["log"]["size_bytes"], baseline["log"]["size_bytes"])
        self.assertTrue(report["log"]["prefix_matches_previous"])
        self.assertEqual(report["disqualification_reasons"], {"unsupported_appended_output": 2})
        self.assertFalse(report["retention_proven"])

    def test_exclusion_diagnostics_are_bounded_and_do_not_affect_sample_cap(self):
        count = evidence.MAX_PATH_EXAMPLES + 5
        name = "../" + "é" * 1900 + ".o"
        self.inputs = [name] * count + ["obj/a.o", "obj/b.obj"]
        with self.log.open("a") as stream:
            stream.write(f"1\t2\t3\t{name}\tabc\n" * count)
        with mock.patch.object(evidence, "MAX_SAMPLES", 1):
            baseline = self.before()
            report = self.after()
        self.assertEqual([sample["output"] for sample in baseline["samples"]], ["obj/a.o"])
        self.assertEqual(report["retained_count"], 1)
        self.assertEqual(baseline["object_hash_bytes"], len(b"tiny-object"))
        self.assertEqual(baseline["membership"]["input_count"], count + 2)
        self.assertEqual(baseline["membership"]["excluded_object_inputs"], count)
        self.assertEqual(baseline["log"]["unselected_unsupported_object_records"], count)
        for examples in (baseline["membership"]["excluded_object_input_examples"],
                         baseline["log"]["unselected_unsupported_object_examples"]):
            self.assertEqual(len(examples), evidence.MAX_PATH_EXAMPLES)
            for example in examples:
                self.assertLessEqual(len(example["path_escaped"]), evidence.MAX_PATH_EXAMPLE_CHARS)
                self.assertTrue(example["path_escaped"].isascii())
                self.assertTrue(example["path_escaped"].endswith("..."))
        self.assertLess(len(json.dumps(report)), 24 * 1024)

    def test_excluded_input_changes_still_disqualify_changed_target_graph(self):
        self.inputs.append("../first.o")
        baseline = self.before()
        self.inputs[-1] = "../other.o"
        report = self.after()
        self.assertNotEqual(report["membership"]["sha256"], baseline["membership"]["sha256"])
        self.assertEqual(report["disqualification_reasons"], {"target_inputs_changed": 2})
        self.assertFalse(report["retention_proven"])

    def test_missing_and_nondirectory_candidates_do_not_prevent_safe_sampling(self):
        (self.out / "archive.a").write_bytes(b"not a directory")
        self.inputs = ["archive.a/member.o", "missing.o", "obj/a.o"]
        baseline = self.before()
        self.assertEqual(baseline["skipped"]["missing_file"], 2)
        self.assertEqual([sample["output"] for sample in baseline["samples"]], ["obj/a.o"])
        self.assertEqual(self.after()["retained_count"], 1)
        shutil.rmtree(self.work / "upstream-reuse")
        self.inputs = self.inputs[:2]
        self.assertEqual(self.before()["samples"], [])
        self.assertFalse(self.after()["retention_proven"])

    def test_nondirectory_parent_after_baseline_is_permanently_missing(self):
        self.before()
        (self.out / "obj").rename(self.out / "oldobj")
        (self.out / "obj").write_bytes(b"not a directory")
        report = self.after()
        self.assertEqual(report["disqualification_reasons"], {"missing_file": 2})
        (self.out / "obj").unlink()
        (self.out / "oldobj").rename(self.out / "obj")
        self.before()
        self.assertFalse(self.after()["retention_proven"])

    def test_crlf_inputs_keep_raw_digest_and_exclude_overlong_object_names(self):
        long_name = "obj/" + "x" * 2048 + ".o"
        raw = ("obj/a.o\r\n../escape.o\r\n" + long_name + "\r\nobj/a.o\r\n").encode()
        original = self.query.side_effect
        self.query.side_effect = lambda ninja, out, args, limit: (
            original(ninja, out, args, limit) if args == ["--version"] else raw)
        baseline = self.before()
        membership = baseline["membership"]
        self.assertEqual(membership["input_count"], 4)
        self.assertEqual(membership["object_count"], 1)
        self.assertEqual(membership["excluded_object_inputs"], 2)
        self.assertEqual(membership["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual([sample["output"] for sample in baseline["samples"]], ["obj/a.o"])
        self.assertEqual(self.after()["retained_count"], 1)

    def test_malformed_inputs_fail_with_bounded_origin_and_escaped_line(self):
        invalid = [(b"obj/a.o\nobj/b.obj", "unterminated", "line 2", "obj/b.obj"),
                   (b"obj/a.o\nobj/\x00bad.o\n", "malformed", "line 2", "\\x00"),
                   (b"obj/a.o\nobj/\tbad.o\n", "malformed", "line 2", "\\t"),
                   (b"obj/a.o\n\n", "malformed", "line 2", "''"),
                   (b"obj/a.o\n\xffbad.o\n", "invalid UTF-8", "byte 8", "\\xff"),
                   (b"x" * (evidence.MAX_LINE_BYTES + 1) + b".o\n", "malformed", "line 1", "...")]
        original = self.query.side_effect
        for raw, reason, location, fragment in invalid:
            with self.subTest(reason=reason, fragment=fragment):
                self.query.side_effect = lambda ninja, out, args, limit: (
                    original(ninja, out, args, limit) if args == ["--version"] else raw)
                self.assertEqual(self.cli("before"), 1)
                error = self.result()["reason"]
                for text in (reason, "ninja -t inputs", location, fragment):
                    self.assertIn(text, error)
                self.assertLessEqual(len(error), 512)
                self.assertNotIn("\x00", error)
                self.assertFalse(self.result()["retention_proven"])

    def test_malformed_unselected_log_record_keeps_origin_and_escaped_path(self):
        with self.log.open("a") as stream:
            stream.write("1\t2\t3\t../bad\x00.o\tabc\n")
        self.assertEqual(self.cli("before"), 1)
        self.assertIn("invalid Ninja log record at .ninja_log line 4", self.result()["reason"])
        self.assertIn("../bad\\x00.o", self.result()["reason"])
        self.assertFalse(self.result()["retention_proven"])

    def test_old_observations_without_path_diagnostics_can_resume(self):
        self.before()
        baseline_path = self.work / "upstream-reuse/baseline.json"
        pending_path = self.work / "upstream-reuse/result.json"
        baseline, pending = json.loads(baseline_path.read_text()), json.loads(pending_path.read_text())
        for data in (baseline, pending):
            for section, keys in (("membership", ("excluded_object_inputs", "excluded_object_input_examples")),
                                  ("log", ("unselected_unsupported_object_records", "unselected_unsupported_object_examples"))):
                for key in keys:
                    del data[section][key]
        pending["baseline_sha256"] = hashlib.sha256(evidence.json_bytes(baseline)).hexdigest()
        baseline_path.write_bytes(evidence.json_bytes(baseline))
        pending_path.write_bytes(evidence.json_bytes(pending))
        raw, info = baseline_path.read_bytes(), baseline_path.stat()
        report = self.after()
        self.assertEqual(report["retained_count"], 2)
        self.assertTrue(report["retention_proven"])
        self.assertEqual(report["disqualified"], {})
        self.assertEqual(report["before_membership"], pending["membership"])
        self.assertEqual(baseline_path.read_bytes(), raw)
        self.assertEqual(baseline_path.stat().st_mtime_ns, info.st_mtime_ns)
        self.assertEqual(self.before(), baseline)
        self.assertEqual(self.after()["retained_count"], 2)

    def test_membership_diagnostic_changes_do_not_disqualify_samples(self):
        self.inputs.append("../escape.o")
        self.before()
        pending_path = self.work / "upstream-reuse/result.json"
        pending = json.loads(pending_path.read_text())
        pending["membership"]["excluded_object_input_examples"][0]["path_escaped"] = "older excerpt"
        pending_path.write_bytes(evidence.json_bytes(pending))
        report = self.after()
        self.assertNotEqual(report["before_membership"], report["membership"])
        self.assertEqual(report["retained_count"], 2)
        self.assertEqual(report["disqualified"], {})

    def test_stable_membership_fields_still_detect_changes_without_diagnostics(self):
        for key in ("sha256", "input_count", "object_count"):
            with self.subTest(key=key):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.before()
                pending_path = self.work / "upstream-reuse/result.json"
                pending = json.loads(pending_path.read_text())
                del pending["membership"]["excluded_object_inputs"]
                del pending["membership"]["excluded_object_input_examples"]
                pending["membership"][key] = "f" * 64 if key == "sha256" else pending["membership"][key] + 1
                pending_path.write_bytes(evidence.json_bytes(pending))
                report = self.after()
                self.assertEqual(report["disqualification_reasons"], {"target_inputs_changed": 2})
                self.assertFalse(report["retention_proven"])

    def test_corrupt_path_diagnostics_fail_closed(self):
        self.inputs.append("../escape.o")
        with self.log.open("a") as stream:
            stream.write("1\t2\t3\t../escape.o\tabc\n")
        baseline = self.before()
        pending_path = self.work / "upstream-reuse/result.json"
        pending = pending_path.read_text()
        for section, count_key, examples_key, total_key in (
                ("membership", "excluded_object_inputs", "excluded_object_input_examples", "input_count"),
                ("log", "unselected_unsupported_object_records", "unselected_unsupported_object_examples", "record_count")):
            for change in ("negative", "bool", "excess_count", "missing_examples", "excess_examples", "wrong_origin",
                           "wrong_line", "oversize_path", "unescaped_path"):
                with self.subTest(section=section, change=change):
                    data = json.loads(pending)
                    value = data[section]
                    if change == "negative":
                        value[count_key] = -1
                    elif change == "bool":
                        value[count_key] = True
                    elif change == "excess_count":
                        value[count_key] = value[total_key] + 1
                    elif change == "missing_examples":
                        del value[examples_key]
                    elif change == "excess_examples":
                        value[examples_key] *= evidence.MAX_PATH_EXAMPLES + 1
                    elif change == "wrong_origin":
                        value[examples_key][0]["origin"] = "elsewhere"
                    elif change == "wrong_line":
                        value[examples_key][0]["line"] = 0
                    elif change == "oversize_path":
                        value[examples_key][0]["path_escaped"] = "x" * (evidence.MAX_PATH_EXAMPLE_CHARS + 1)
                    else:
                        value[examples_key][0]["path_escaped"] = "bad\npath"
                    pending_path.write_text(json.dumps(data))
                    self.assertEqual(self.cli("after", 0), 1)
                    self.assertFalse(self.result()["retention_proven"])
        self.assertEqual(json.loads((self.work / "upstream-reuse/baseline.json").read_text()), baseline)

    def test_symlinked_or_hardlinked_objects_and_linked_parents_rejected(self):
        original = self.out / "obj/a.o"
        target = self.root / "external"
        target.write_bytes(b"tiny-object")
        for kind in ("symlink", "hardlink", "parent"):
            with self.subTest(kind=kind):
                original.unlink()
                if kind == "symlink":
                    original.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, original)
                else:
                    (self.out / "obj").rename(self.out / "oldobj")
                    (self.out / "obj").symlink_to(self.out / "oldobj", target_is_directory=True)
                with self.assertRaises(ValueError):
                    self.before()
                if kind == "parent":
                    (self.out / "obj").unlink()
                    (self.out / "oldobj").rename(self.out / "obj")
                else:
                    original.unlink()
                original.write_bytes(b"tiny-object")

    def test_symlink_and_hardlink_candidates_are_not_hidden_by_exclusions(self):
        target = self.root / "external"
        target.write_bytes(b"tiny-object")
        self.inputs = ["../excluded.o", "obj/a.o"]
        original = self.out / "obj/a.o"
        for phase in ("before", "after"):
            for kind in ("symlink", "hardlink", "dangling", "parent"):
                with self.subTest(phase=phase, kind=kind):
                    shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                    self.make_log()
                    if phase == "after":
                        self.before()
                    original.unlink()
                    if kind in ("symlink", "dangling"):
                        original.symlink_to(target if kind == "symlink" else self.root / "missing")
                    elif kind == "hardlink":
                        os.link(target, original)
                    else:
                        (self.out / "obj").rename(self.out / "oldobj")
                        (self.out / "obj").symlink_to(self.out / "oldobj", target_is_directory=True)
                    self.assertEqual(self.cli(phase, 0 if phase == "after" else None), 1)
                    self.assertFalse(self.result()["retention_proven"])
                    if kind == "parent":
                        (self.out / "obj").unlink()
                        (self.out / "oldobj").rename(self.out / "obj")
                    else:
                        original.unlink()
                    self.assertEqual(target.read_bytes(), b"tiny-object")

    def test_linked_report_or_metadata_never_overwrites_external_file(self):
        target = self.root / "external"
        target.write_text("keep")
        directory = self.work / "upstream-reuse"
        directory.mkdir()
        (directory / "result.json").symlink_to(target)
        self.assertEqual(self.cli("before"), 1)
        self.assertEqual(target.read_text(), "keep")
        (directory / "result.json").unlink()
        self.log.unlink()
        self.log.symlink_to(target)
        self.assertEqual(self.cli("before"), 1)
        self.assertEqual(target.read_text(), "keep")

    def test_invalid_logs_hard_fail_and_replace_old_success_report(self):
        invalid = ["# ninja log v8\n", "# ninja log v5\n1\t2\t3\ta.o\tx\n",
                   "# ninja log v5\n2\t1\t3\ta.o\tabc\n", "# ninja log v5\n1\t2\t-1\ta.o\tabc\n",
                   "# ninja log v5\n1\t2\t3\ta.o\tabc", "# ninja log v5\n1\t2\t3\n"]
        for text in invalid:
            with self.subTest(text=text):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.make_log()
                self.before()
                self.after()
                self.log.write_text(text)
                self.assertEqual(self.cli("before"), 1)
                self.assertFalse(self.result()["retention_proven"])
                self.assertEqual(self.result()["status"], "error")

    def test_corrupt_baseline_is_never_replaced(self):
        self.before()
        path = self.work / "upstream-reuse/baseline.json"
        baseline = json.loads(path.read_text())
        for content in ("{", '{"owner":1,"owner":2}', json.dumps(dict(baseline, samples=[{}])),
                        json.dumps(dict(baseline, samples=baseline["samples"] * 2))):
            with self.subTest(content=content[:40]):
                path.write_text(content)
                self.assertEqual(self.cli("before"), 1)
                self.assertEqual(path.read_text(), content)
                self.assertFalse(self.result()["retention_proven"])

    def test_byte_and_sample_caps(self):
        self.make_log(names=("obj/a.o", "obj/b.obj", "obj/c.o"))
        self.inputs = ["obj/a.o", "obj/b.obj", "obj/c.o"]
        with mock.patch.object(evidence, "MAX_HASH_BYTES", 15), \
                mock.patch.object(evidence, "MAX_FILE_BYTES", 12):
            baseline = self.before()
            self.assertEqual(baseline["object_hash_bytes"], 11)
            self.assertEqual(baseline["skipped"]["total_byte_cap"], 2)
            self.assertLessEqual(self.after()["object_hash_bytes"], 15)
        shutil.rmtree(self.work / "upstream-reuse")
        (self.out / "obj/a.o").write_bytes(b"too large for cap")
        with mock.patch.object(evidence, "MAX_SAMPLES", 1), mock.patch.object(evidence, "MAX_FILE_BYTES", 12):
            baseline = self.before()
        self.assertEqual([sample["output"] for sample in baseline["samples"]], ["obj/b.obj"])
        self.assertEqual(baseline["skipped"]["file_byte_cap"], 1)

    def test_log_and_json_byte_caps_hard_fail(self):
        with mock.patch.object(evidence, "MAX_LOG_BYTES", 16):
            self.assertEqual(self.cli("before"), 1)
        shutil.rmtree(self.work / "upstream-reuse")
        self.before()
        with mock.patch.object(evidence, "MAX_JSON_BYTES", 16):
            self.assertEqual(self.cli("before"), 1)

    def test_missing_baseline_on_resume_is_not_recaptured(self):
        self.before()
        self.after()
        path = self.work / "upstream-reuse/baseline.json"
        path.unlink()
        self.assertEqual(self.cli("before"), 1)
        self.assertFalse(path.exists())
        self.assertFalse(self.result()["retention_proven"])

    def test_malformed_baseline_and_pending_metadata_fail_closed(self):
        self.before()
        baseline_path = self.work / "upstream-reuse/baseline.json"
        pending_path = self.work / "upstream-reuse/result.json"
        baseline, pending = baseline_path.read_bytes(), pending_path.read_bytes()
        for path, key, value in ((baseline_path, "log", []), (baseline_path, "membership", None),
                                 (pending_path, "log", "bad"), (pending_path, "membership", 3),
                                 (pending_path, "eligible_outputs", [True])):
            with self.subTest(key=key, value=value):
                baseline_path.write_bytes(baseline)
                pending_path.write_bytes(pending)
                data = json.loads(path.read_text())
                data[key] = value
                path.write_text(json.dumps(data))
                self.assertEqual(self.cli("after", 0), 1)
                self.assertFalse(self.result()["retention_proven"])

    def test_crlf_logs_are_supported_without_losing_full_records(self):
        self.log.write_bytes(self.log.read_bytes().replace(b"\n", b"\r\n"))
        baseline = self.before()
        self.assertEqual(baseline["samples"][0]["record"]["start"], 10)
        self.assertTrue(self.after()["retention_proven"])

    def test_only_safe_run_and_head_identifiers_are_recorded(self):
        values = {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
                  "GITHUB_JOB": "compile-x64", "GITHUB_HEAD_REF": "bad\nhead", "GH_TOKEN": "secret",
                  "GITHUB_TOKEN": "secret", "PASSWORD": "secret", "CREDENTIALS": "secret"}
        with mock.patch.dict(os.environ, values, clear=True):
            report = self.before()
        self.assertEqual(set(report["run"]), {"GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "GITHUB_JOB"})
        self.assertNotIn("secret", json.dumps(report))

    def test_disqualification_survives_restored_bytes_mtime_and_record_across_calls(self):
        for phase in ("before", "after"):
            for kind in ("content", "mtime", "record", "missing"):
                with self.subTest(phase=phase, kind=kind):
                    shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                    self.make_log()
                    baseline = self.before()
                    path = self.out / "obj/a.o"
                    raw, mtime = path.read_bytes(), path.stat().st_mtime_ns
                    if kind == "content":
                        path.write_bytes(b"evil-object")
                        os.utime(path, ns=(mtime, mtime))
                    elif kind == "mtime":
                        os.utime(path, ns=(mtime + 1, mtime + 1))
                    elif kind == "record":
                        with self.log.open("a") as stream:
                            stream.write(self.records[0].replace("10\t20\t", "30\t40\t"))
                    else:
                        path.unlink()
                    self.before() if phase == "before" else self.after()
                    rejected = self.result()["disqualified"]
                    self.assertIn("obj/a.o", rejected)
                    path.write_bytes(raw)
                    os.utime(path, ns=(mtime, mtime))
                    with self.log.open("a") as stream:
                        stream.write(self.records[0])
                    self.assertEqual(self.before(), baseline)
                    report = self.after()
                    self.assertEqual(report["retained_count"], 1)
                    self.assertEqual(report["disqualified"], rejected)
                    self.before()
                    self.assertEqual(self.after()["disqualified"], rejected)

    def test_identical_selected_record_appended_is_permanently_disqualified(self):
        for version in (5, 6, 7):
            for phase in ("before", "after"):
                with self.subTest(version=version, phase=phase):
                    shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                    self.version = version
                    self.make_log(names=("obj/a.o",))
                    self.inputs = ["obj/a.o"]
                    baseline = self.before()
                    with self.log.open("a") as stream:
                        stream.write(self.records[0])
                    self.before() if phase == "before" else self.after()
                    report = self.result()
                    self.assertEqual(report["samples"][0]["record"], baseline["samples"][0]["record"])
                    self.assertEqual(report["disqualified"], {"obj/a.o": "appended_record_repeated"})
                    self.assertFalse(report["retention_proven"])
                    self.assertEqual(self.before(), baseline)
                    self.assertEqual(self.after()["retained_count"], 0)

    def test_changed_then_baseline_appended_in_one_interval_is_disqualified(self):
        for phase in ("before", "after"):
            with self.subTest(phase=phase):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.make_log()
                baseline = self.before()
                with self.log.open("a") as stream:
                    stream.write(self.records[0].replace("10\t20\t", "30\t40\t") + self.records[0])
                self.before() if phase == "before" else self.after()
                report = self.result()
                self.assertEqual(report["samples"][0]["record"], baseline["samples"][0]["record"])
                self.assertEqual(report["disqualified"], {"obj/a.o": "appended_record_changed"})
                self.before()
                self.assertEqual(self.after()["retained_count"], 1)

    def test_truncation_then_growth_breaks_full_previous_prefix_permanently(self):
        baseline = self.before()
        with self.log.open("a") as stream:
            stream.write("1\t2\t0\tunrelated\tabc\n")
        previous = self.after()
        # The original baseline is intact, but the last observation's suffix changed.
        self.log.write_text(self.log.read_text().replace("unrelated", "different") + self.records[0] * 4)
        self.before()
        report = self.result()
        self.assertGreater(report["log"]["size_bytes"], previous["log"]["size_bytes"])
        self.assertEqual(report["log"]["prefix_size_bytes"], previous["log"]["size_bytes"])
        self.assertFalse(report["log"]["prefix_matches_previous"])
        self.assertEqual(report["disqualification_reasons"], {"log_prefix_mismatch": 2})
        self.log.write_text("# ninja log v5\n" + "".join(self.records))
        self.before()
        self.assertFalse(self.after()["retention_proven"])
        self.assertEqual(json.loads((self.work / "upstream-reuse/baseline.json").read_text()), baseline)

    def test_timeout_or_killed_build_resume_inspects_appended_records(self):
        for completed_after in (False, True):
            with self.subTest(completed_after=completed_after):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.make_log()
                self.before()
                if completed_after:
                    self.assertEqual(self.after(124)["status"], "incomplete")
                with self.log.open("a") as stream:
                    stream.write(self.records[0].replace("10\t20\t", "30\t40\t") + self.records[0])
                self.before()
                self.assertEqual(self.result()["disqualified"], {"obj/a.o": "appended_record_changed"})
                self.assertEqual(self.after()["retained_count"], 1)

    def test_original_history_before_baseline_does_not_disqualify(self):
        for version in (5, 6, 7):
            with self.subTest(version=version):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.version = version
                self.make_log()
                self.log.write_text(f"# ninja log v{version}\n" + self.records[0].replace(
                    "10\t20\t", "30\t40\t") + "".join(self.records))
                baseline = self.before()
                self.assertEqual(baseline["log"]["sha256"], hashlib.sha256(self.log.read_bytes()).hexdigest())
                for _ in range(2):
                    report = self.after()
                    self.assertTrue(report["retention_proven"])
                    self.assertEqual(report["disqualified"], {})
                    self.assertEqual(report["log"]["prefix_sha256"], baseline["log"]["sha256"])
                    self.before()

    def test_zero_log_mtimes_are_valid_but_not_object_candidates(self):
        zero = self.records[0].replace("1700000000000000000", "0")
        self.log.write_text("# ninja log v5\n" + zero + self.records[1] + "1\t2\t0\tunrelated\tabc\n")
        baseline = self.before()
        self.assertEqual([sample["output"] for sample in baseline["samples"]], ["obj/b.obj"])
        self.assertEqual(baseline["skipped"]["nonpositive_log_mtime"], 1)
        with self.log.open("a") as stream:
            stream.write("1\t2\t0\tanother\tabc\n")
        self.assertEqual(self.after()["retained_count"], 1)

    def test_nonpositive_appended_object_mtime_permanently_disqualifies(self):
        self.before()
        with self.log.open("a") as stream:
            stream.write(self.records[0].replace("1700000000000000000", "0") + self.records[0])
        report = self.after()
        self.assertEqual(report["disqualified"], {"obj/a.o": "appended_record_changed"})
        self.assertEqual(report["retained_count"], 1)

    def test_invalid_disqualification_reason_or_counter_fails_closed(self):
        self.before()
        path = self.work / "upstream-reuse/result.json"
        pending = path.read_bytes()
        for change in ({"disqualified": {"obj/a.o": "retained"}, "disqualified_count": 1},
                       {"disqualified_count": 1}, {"disqualification_reasons": {"log_prefix_mismatch": 2}}):
            with self.subTest(change=change):
                data = json.loads(pending)
                data.update(change)
                path.write_text(json.dumps(data))
                self.assertEqual(self.cli("after", 0), 1)
                self.assertFalse(self.result()["retention_proven"])

    def test_missing_or_error_previous_state_cannot_restart_observations(self):
        self.before()
        path = self.work / "upstream-reuse/result.json"
        raw = path.read_bytes()
        path.unlink()
        self.assertEqual(self.cli("before"), 1)
        self.assertFalse(self.result()["retention_proven"])
        self.assertEqual(self.cli("before"), 1)
        data = json.loads(raw)
        data["disqualified"] = []
        path.write_text(json.dumps(data))
        self.assertEqual(self.cli("before"), 1)

    @unittest.skipUnless(os.name == "posix", "executable fixture requires POSIX")
    def test_real_query_failure_after_successful_build_never_proves_or_restarts_retention(self):
        baseline = self.before()
        baseline_path = self.work / "upstream-reuse/baseline.json"
        original, info = baseline_path.read_bytes(), baseline_path.stat()
        self.ninja.write_text(f"#!{sys.executable}\nimport sys, time\n"
                              "if sys.argv[1:] == ['--version']:\n"
                              "    print('1.11.1')\n"
                              "else:\n"
                              "    sys.stderr.write('loading manifest\\n')\n"
                              "    sys.stderr.flush()\n"
                              "    time.sleep(30)\n")
        self.ninja.chmod(0o755)
        self.query.side_effect = self.real_query
        with mock.patch.object(evidence, "NINJA_INPUTS_TIMEOUT", 0.3):
            self.assertEqual(self.cli("after", 0), 1)
        report = self.result()
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["retention_proven"])
        for text in ("'-t', 'inputs', 'chrome'", "timed out", "timeout=0.3s", "loading manifest\\n"):
            self.assertIn(text, report["reason"])
        self.query.side_effect = self.query_result
        self.assertEqual(self.cli("before"), 1)
        self.assertFalse(self.result()["retention_proven"])
        self.assertEqual(baseline_path.read_bytes(), original)
        self.assertEqual(baseline_path.stat().st_mtime_ns, info.st_mtime_ns)
        self.assertEqual(json.loads(original), baseline)

    def test_ninja_110_requires_newer_selector_before_inputs_query(self):
        self.query.side_effect = lambda *args: b"1.10.2\n"
        self.assertEqual(self.cli("before"), 1)
        self.assertEqual(self.query.call_count, 1)
        self.assertIn("requires 1.11", self.result()["reason"])
        self.assertFalse(self.result()["retention_proven"])

    def test_selected_chromium_ninja_version_suffix_is_supported(self):
        original = self.query.side_effect
        for minor in (11, 12, 13):
            with self.subTest(minor=minor):
                self.query.side_effect = lambda ninja, out, args, limit: (
                    f"1.{minor}.1.chromium.4\n".encode() if args == ["--version"] else original(ninja, out, args, limit))
                self.before()
                self.assertEqual(self.result()["ninja"]["version"], f"1.{minor}.1.chromium.4")
                self.assertTrue(self.after()["retention_proven"])

    def test_cli_supports_both_phase_spellings_and_reports_build_failure_without_collector_failure(self):
        self.assertEqual(self.cli("before"), 0)
        self.assertEqual(self.cli("after", 124), 0)
        self.assertEqual(self.result()["status"], "incomplete")
        args = ["--phase", "before", "--workdir", str(self.work), "--platform", "linux", "--arch", "x64",
                "--ninja", str(self.ninja), "--target", "chrome"]
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(evidence.main(args), 0)
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            evidence.main([*args, "--exit-code", "0"])
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            evidence.main([value if value != "before" else "after" for value in args])


@unittest.skipUnless(os.name == "posix", "executable fixtures require POSIX")
class NinjaQueryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.ninja = self.out / "ninja"
        self.sigterm_handler = signal.getsignal(signal.SIGTERM)
        self.addCleanup(lambda: self.assertIs(signal.getsignal(signal.SIGTERM), self.sigterm_handler))
        self.spawn = subprocess.Popen
        self.popen = mock.patch.object(evidence.subprocess, "Popen", wraps=self.spawn).start()
        self.addCleanup(mock.patch.stopall)

    def executable(self, body):
        self.ninja.write_text(f"#!{sys.executable}\nimport os, sys, time\n" + body)
        self.ninja.chmod(0o755)

    def query(self, args, limit=4096):
        return evidence.query(self.ninja, self.out, args, limit)

    def assert_cleaned(self):
        process = self.popen.call_args
        self.assertEqual(process.kwargs["cwd"], self.out)
        self.assertEqual(process.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(process.kwargs["start_new_session"], True)
        pid = int((self.out / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_sleeping_executable_times_out_and_is_reaped_even_after_stdout_eof(self):
        for close_stdout in (False, True):
            with self.subTest(close_stdout=close_stdout):
                self.executable("open('pid', 'w').write(str(os.getpid()))\n"
                                "sys.stderr.write('still loading\\n'); sys.stderr.flush()\n"
                                + ("os.close(1)\n" if close_stdout else "") + "time.sleep(30)\n")
                threads = set(threading.enumerate())
                started = time.monotonic()
                with mock.patch.object(evidence, "NINJA_TIMEOUT", 0.3):
                    with self.assertRaises(evidence.EvidenceError) as raised:
                        self.query(["--version"])
                reason = str(raised.exception)
                for text in ("'--version'", "timed out", "timeout=0.3s", "elapsed=", "returncode=-9",
                             "stdout_bytes=0", "stderr=b'still loading\\n'"):
                    self.assertIn(text, reason)
                self.assertLess(time.monotonic() - started, 5)
                self.assertLessEqual(len(reason), 512)
                self.assertEqual(set(threading.enumerate()), threads)
                self.assert_cleaned()

    def test_inputs_has_its_own_bounded_timeout_and_version_stays_short(self):
        self.assertEqual(evidence.NINJA_INPUTS_TIMEOUT, 600)
        self.assertLessEqual(evidence.NINJA_TIMEOUT, 120)
        self.assertLess(evidence.NINJA_TIMEOUT, evidence.NINJA_INPUTS_TIMEOUT)
        self.executable("time.sleep(0.75)\nsys.stdout.buffer.write(b'obj/a.o\\n')\n")
        with mock.patch.object(evidence, "NINJA_TIMEOUT", 0.3), \
                mock.patch.object(evidence, "NINJA_INPUTS_TIMEOUT", 2):
            with self.assertRaisesRegex(evidence.EvidenceError, "timed out; timeout=0.3s"):
                self.query(["--version"])
            names, metadata = evidence.target_inputs(self.ninja, self.out, ["chrome"])
            self.assertEqual(names, {"obj/a.o"})
            self.assertEqual(metadata["sha256"], hashlib.sha256(b"obj/a.o\n").hexdigest())
        with mock.patch.object(evidence, "NINJA_INPUTS_TIMEOUT", 0.3):
            with self.assertRaisesRegex(evidence.EvidenceError, "timed out; timeout=0.3s"):
                self.query(["-t", "inputs", "chrome"])

    def test_stdout_cap_kills_streaming_executable_without_waiting_for_timeout(self):
        self.executable("open('pid', 'w').write(str(os.getpid()))\n"
                        "while True:\n    os.write(1, b'x' * 65536)\n")
        started = time.monotonic()
        with self.assertRaises(evidence.EvidenceError) as raised:
            self.query(["-t", "inputs", "chrome"], limit=128)
        reason = str(raised.exception)
        for text in ("exceeds stdout byte cap", "stdout_bytes=129", "stdout_cap=128", "timeout=600s"):
            self.assertIn(text, reason)
        self.assertNotIn("timed out", reason)
        self.assertLess(time.monotonic() - started, 5)
        self.assert_cleaned()

    def test_nonzero_exit_has_bounded_escaped_stderr_and_separate_stdout(self):
        self.executable("open('pid', 'w').write(str(os.getpid()))\n"
                        "sys.stdout.buffer.write(b'obj/a.o\\n')\n"
                        "sys.stderr.buffer.write(b'ninja: missing target\\n\\r\\x1b[31m\\xff' + b'x' * 200000)\n"
                        "sys.exit(7)\n")
        with self.assertRaises(evidence.EvidenceError) as raised:
            self.query(["-t", "inputs", "chrome"], limit=8)
        reason = str(raised.exception)
        for text in ("failed (nonzero exit)", "returncode=7", "stdout_bytes=8", "stdout_cap=8",
                     "stderr_bytes=200029", "missing target\\n\\r\\x1b[31m\\xff", "..."):
            self.assertIn(text, reason)
        self.assertTrue(all(32 <= ord(char) < 127 for char in reason))
        self.assertLessEqual(len(reason), 512)
        self.assertNotIn("timed out", reason)
        self.assertNotIn("exceeds", reason)
        self.assert_cleaned()

    def test_exact_stdout_cap_and_empty_stdout_succeed_and_cancel_timer(self):
        for data in (b"", b"obj/a.o\n"):
            with self.subTest(data=data):
                self.executable(f"open('pid', 'w').write(str(os.getpid()))\n"
                                f"sys.stdout.buffer.write({data!r})\n")
                threads = set(threading.enumerate())
                self.assertEqual(self.query(["-t", "inputs", "chrome"], limit=len(data)), data)
                self.assertEqual(set(threading.enumerate()), threads)
                self.assert_cleaned()

    def test_stderr_never_becomes_membership_even_with_success_exit(self):
        self.executable("sys.stdout.buffer.write(b'obj/a.o\\n')\n"
                        "sys.stderr.buffer.write(b'obj/forged.o\\n')\n")
        with self.assertRaisesRegex(evidence.EvidenceError, "unexpected stderr.*returncode=0"):
            evidence.target_inputs(self.ninja, self.out, ["chrome"])

    def test_spawn_failure_restores_sigterm_handler(self):
        with self.assertRaises(FileNotFoundError):
            self.query(["--version"])
        self.assertIs(signal.getsignal(signal.SIGTERM), self.sigterm_handler)

    def test_sigterm_during_spawn_cleans_query_and_restores_previous_handler(self):
        self.executable("time.sleep(30)\n")
        processes, handled = [], []

        def spawning(*args, **kwargs):
            process = self.spawn(*args, **kwargs)
            processes.append(process)
            os.kill(os.getpid(), signal.SIGTERM)
            return process

        def previous_handler(signum, frame):
            handled.append(signum)

        signal.signal(signal.SIGTERM, previous_handler)
        self.popen.side_effect = spawning
        try:
            with self.assertRaisesRegex(evidence.EvidenceError, "interrupted by SIGTERM"):
                self.query(["-t", "inputs", "chrome"])
            self.assertEqual(handled, [signal.SIGTERM])
            self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)
            self.assertEqual(processes[0].returncode, -signal.SIGKILL)
            with self.assertRaises(ProcessLookupError):
                os.killpg(processes[0].pid, 0)
        finally:
            signal.signal(signal.SIGTERM, self.sigterm_handler)
            for process in processes:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)

    def test_worker_thread_does_not_detach_without_a_sigterm_handler(self):
        self.executable("print('1.12.1')\n")
        results = []
        worker = threading.Thread(target=lambda: results.append(self.query(["--version"])), daemon=True)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [b"1.12.1\n"])
        self.assertIs(self.popen.call_args.kwargs["start_new_session"], False)
        self.assertIs(signal.getsignal(signal.SIGTERM), self.sigterm_handler)

    @unittest.skipUnless(sys.platform == "linux", "process group check requires procfs")
    def test_caller_sigterm_cleans_detached_query_group(self):
        self.executable("import subprocess\n"
                        "open('pid', 'w').write(str(os.getpid()))\n"
                        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                        "with open('child', 'w') as stream:\n    stream.write(str(child.pid))\n"
                        "time.sleep(30)\n")
        script = ("import sys\nfrom pathlib import Path\n"
                  "from tools import restored_reuse_evidence as evidence\n"
                  "out = Path(sys.argv[1])\n"
                  "evidence.query(out / 'ninja', out, ['-t', 'inputs', 'chrome'], 4096)\n"
                  "(out / 'returned').write_text('query must not return')\n")
        for group_signal in (False, True):
            with self.subTest(group_signal=group_signal):
                for name in ("pid", "child"):
                    (self.out / name).unlink(missing_ok=True)
                with subprocess.Popen([sys.executable, "-c", script, str(self.out)], cwd=evidence.REPO,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      start_new_session=True) as caller:
                    try:
                        deadline = time.monotonic() + 5
                        ready = self.out / "child"
                        while (not ready.exists() or not ready.read_text()) and time.monotonic() < deadline:
                            self.assertIsNone(caller.poll(), "evidence caller exited before query started")
                            time.sleep(0.01)
                        self.assertTrue(ready.exists() and ready.read_text(), "query did not start within 5s")
                        pid = int((self.out / "pid").read_text())
                        self.assertNotEqual(os.getpgid(pid), os.getpgid(caller.pid))
                        if group_signal:
                            os.killpg(caller.pid, signal.SIGTERM)
                        else:
                            caller.send_signal(signal.SIGTERM)
                        stdout, stderr = caller.communicate(timeout=5)
                        self.assertEqual(caller.returncode, -signal.SIGTERM, stderr.decode())
                        self.assertEqual(stdout, b"")
                        self.assertEqual(stderr, b"")
                        self.assertFalse((self.out / "returned").exists())
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                        child = Path("/proc") / ready.read_text() / "stat"
                        if child.exists():
                            fields = child.read_text().rsplit(")", 1)[1].split()
                            self.assertEqual(int(fields[2]), pid)
                            self.assertEqual(fields[0], "Z", "query child is still running")
                    finally:
                        if (self.out / "pid").exists():
                            try:
                                os.killpg(int((self.out / "pid").read_text()), signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        if caller.poll() is None:
                            os.killpg(caller.pid, signal.SIGKILL)
                        caller.communicate(timeout=5)

    @unittest.skipUnless(sys.platform == "linux", "process state check requires procfs")
    def test_timeout_kills_pipe_inheriting_child_after_parent_exits(self):
        self.executable("import subprocess\n"
                        "open('pid', 'w').write(str(os.getpid()))\n"
                        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                        "open('child', 'w').write(str(child.pid))\n")
        started = time.monotonic()
        with mock.patch.object(evidence, "NINJA_TIMEOUT", 0.3):
            with self.assertRaisesRegex(evidence.EvidenceError, "timed out.*returncode=0"):
                self.query(["--version"])
        self.assertLess(time.monotonic() - started, 5)
        self.assert_cleaned()
        child = Path("/proc") / (self.out / "child").read_text() / "stat"
        if child.exists():
            self.assertEqual(child.read_text().split()[2], "Z")


@unittest.skipUnless(AVAILABLE and os.name == "posix", "official tiny-fixture Ninja binaries unavailable")
class RealNinjaEvidenceTest(unittest.TestCase):
    def make_work(self, root: Path, ninja: Path):
        work = root / "work"
        out = work / "src/out/Default"
        out.mkdir(parents=True)
        (out / "source").write_text("tiny object, no compiler\n")
        (out / "build.ninja").write_text(
            "rule copy\n  command = cp $in $out\n"
            "build a.o: copy source\n"
            "build b.obj: copy source\n"
            "build ordered.o: copy source\n"
            "build ignored.o: copy source\n"
            "build archive: copy a.o | b.obj || ordered.o\n"
            "build chrome: phony archive\n"
            "build other: copy ignored.o\n")
        subprocess.run([str(ninja), "chrome", "other"], cwd=out, check=True, capture_output=True)
        identity, _, manifest = restore.identities(restore.REPO, "linux", "x64")
        src = work / "src"
        (src / "chrome").mkdir()
        (src / "chrome/VERSION").write_text("\n".join(f"{name}={value}" for name, value in zip(
            ("MAJOR", "MINOR", "BUILD", "PATCH"), identity["chromium_version"].split("."))) + "\n")
        (src / "BUILD.gn").write_text('group("chrome") {}\n')
        args = 'target_cpu = "x64"\n'
        (out / "args.gn").write_text(args)
        (out / ".ninja_deps").write_bytes(b"# ninjadeps\n\x04\0\0\0")
        receipt = {"schema_version": 1, "owner": restore.OWNER, "status": "restored",
                   "extraction_scope": restore.fetcher.SOURCE_SCOPE, "identity": identity, "manifest": manifest,
                   "platform": "linux", "arch": "x64", "external_symlink_paths": [],
                   "original_args": {"path": "out/Default/args.gn", "text": args, "bytes": len(args),
                                     "sha256": hashlib.sha256(args.encode()).hexdigest(),
                                     "assignments": restore.parse_gn_assignments(args)}}
        (src / restore.MARKER).write_text(json.dumps(receipt))
        return work, out

    def test_real_target_closure_noop_retention_and_rebuild_v5_v6_v7(self):
        for ninja in AVAILABLE:
            with self.subTest(ninja=ninja), tempfile.TemporaryDirectory() as tmp:
                work, out = self.make_work(Path(tmp), ninja)
                names, metadata = evidence.target_inputs(ninja, out, ["chrome"])
                self.assertEqual(names, {"a.o", "b.obj", "ordered.o"})
                self.assertFalse(metadata["validation_inputs_included"])
                union, _ = evidence.target_inputs(ninja, out, ["chrome", "other"])
                self.assertIn("ignored.o", union)
                baseline = evidence.before(work, "linux", "x64", ninja)
                expected = {"chromix-ninja-v1.11.1": 5, "chromix-ninja-v1.12.1": 6, "chromix-ninja-v1.13.2": 7}
                self.assertEqual(baseline["log"]["version"], expected[ninja.parent.name])
                log = (out / ".ninja_log").read_bytes()
                result = subprocess.run([str(ninja), "chrome"], cwd=out, check=True, capture_output=True)
                self.assertIn(b"no work to do", result.stdout)
                report = evidence.after(work, "linux", "x64", ninja, exit_code=result.returncode)
                self.assertEqual(report["retained_count"], 3)
                self.assertTrue(report["retention_proven"])
                self.assertEqual((out / ".ninja_log").read_bytes(), log)
                evidence.before(work, "linux", "x64", ninja)
                (out / "a.o").unlink()
                result = subprocess.run([str(ninja), "chrome"], cwd=out, check=True, capture_output=True)
                report = evidence.after(work, "linux", "x64", ninja, exit_code=result.returncode)
                self.assertEqual(report["retained_count"], 2)
                self.assertEqual(report["samples"][0]["status"], "appended_record_changed")
                baseline_path = work / "upstream-reuse/baseline.json"
                self.assertEqual(json.loads(baseline_path.read_text()), baseline)

    def test_real_equals_input_quoting_and_raw_log_append_preserve_retention_v5_v6_v7(self):
        for ninja in AVAILABLE:
            with self.subTest(ninja=ninja), tempfile.TemporaryDirectory() as tmp:
                work, out = self.make_work(Path(tmp), ninja)
                build = out / "build.ninja"
                text = build.read_text().replace("build chrome: phony archive\n",
                                                "build chrome: phony equals-archive\n")
                text += (f"build keep.o: copy source\nbuild {XNNPACK_OBJECT}: copy source\n"
                         f"build equals-archive: copy keep.o | {XNNPACK_OBJECT}\n")
                build.write_text(text)
                subprocess.run([str(ninja), "chrome"], cwd=out, check=True, capture_output=True)
                raw_inputs = evidence.query(ninja, out, ["-t", "inputs", "chrome"], evidence.MAX_INPUT_BYTES)
                self.assertIn(f"'{XNNPACK_OBJECT}'".encode(), raw_inputs.splitlines())
                self.assertNotIn(XNNPACK_OBJECT.encode(), raw_inputs.splitlines())
                names, membership = evidence.target_inputs(ninja, out, ["chrome"])
                self.assertEqual(names, {"keep.o"})
                self.assertEqual(membership["excluded_object_inputs"], 1)
                self.assertEqual(membership["sha256"], hashlib.sha256(raw_inputs).hexdigest())
                baseline = evidence.before(work, "linux", "x64", ninja)
                expected = {"chromix-ninja-v1.11.1": 5, "chromix-ninja-v1.12.1": 6, "chromix-ninja-v1.13.2": 7}
                self.assertEqual(baseline["log"]["version"], expected[ninja.parent.name])
                self.assertEqual([sample["output"] for sample in baseline["samples"]], ["keep.o"])
                self.assertEqual(baseline["log"]["unselected_unsupported_object_records"], 0)
                baseline_path = work / "upstream-reuse/baseline.json"
                saved = baseline_path.read_bytes()
                prefix = (out / ".ninja_log").read_bytes()
                (out / XNNPACK_OBJECT).unlink()
                result = subprocess.run([str(ninja), "chrome"], cwd=out, check=True, capture_output=True)
                raw_log = (out / ".ninja_log").read_bytes()
                self.assertEqual(raw_log[:len(prefix)], prefix)
                suffix = raw_log[len(prefix):]
                self.assertIn(f"\t{XNNPACK_OBJECT}\t".encode(), suffix)
                self.assertNotIn(f"\t'{XNNPACK_OBJECT}'\t".encode(), suffix)
                report = evidence.after(work, "linux", "x64", ninja, exit_code=result.returncode)
                self.assertEqual(report["retained_count"], 1)
                self.assertTrue(report["retention_proven"])
                self.assertEqual(report["disqualified"], {})
                self.assertTrue(report["log"]["prefix_matches_previous"])
                self.assertEqual(report["log"]["unselected_unsupported_object_records"], 0)
                self.assertEqual(baseline_path.read_bytes(), saved)

    def test_real_unsupported_closure_and_log_outputs_never_sampled_v5_v6_v7(self):
        for ninja in AVAILABLE:
            with self.subTest(ninja=ninja), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                work, out = self.make_work(root, ninja)
                (out / "obj").mkdir()
                unsupported = ["../outside.o", str(root / "absolute.obj"), "obj/space name.o",
                               "obj/lib.a:member.o", "obj/lib.a(member.o)"]
                escaped = [name.replace(":", "$:").replace(" ", "$ ") for name in unsupported]
                build = out / "build.ninja"
                text = build.read_text().replace("| b.obj ||", "| b.obj " + " ".join(escaped) + " ||")
                text += "".join(f"build {name}: copy source\n" for name in escaped)
                text += "build unsupported: copy source | " + " ".join(escaped) + "\n"
                build.write_text(text)
                subprocess.run([str(ninja), "chrome", "unsupported"], cwd=out, check=True, capture_output=True)
                names, membership = evidence.target_inputs(ninja, out, ["chrome"])
                self.assertEqual(names, {"a.o", "b.obj", "ordered.o"})
                self.assertEqual(membership["excluded_object_inputs"], len(unsupported))
                raw = evidence.query(ninja, out, ["-t", "inputs", "chrome"], evidence.MAX_INPUT_BYTES)
                self.assertEqual(membership["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(membership["input_count"], len(raw.splitlines()))
                with mock.patch.object(evidence, "MAX_SAMPLES", 1):
                    baseline = evidence.before(work, "linux", "x64", ninja)
                    self.assertEqual([sample["output"] for sample in baseline["samples"]], ["a.o"])
                    self.assertEqual(baseline["log"]["unselected_unsupported_object_records"], len(unsupported))
                    result = subprocess.run([str(ninja), "chrome"], cwd=out, check=True, capture_output=True)
                    self.assertIn(b"no work to do", result.stdout)
                    report = evidence.after(work, "linux", "x64", ninja, exit_code=result.returncode)
                    self.assertEqual(report["retained_count"], 1)
                    evidence.before(work, "linux", "x64", ninja)
                    (out / "a.o").unlink()
                    result = subprocess.run([str(ninja), "chrome"], cwd=out, check=True, capture_output=True)
                    report = evidence.after(work, "linux", "x64", ninja, exit_code=result.returncode)
                    self.assertEqual(report["disqualified"], {"a.o": "appended_record_changed"})
                    self.assertFalse(report["retention_proven"])
                shutil.rmtree(work / "upstream-reuse")
                baseline = evidence.before(work, "linux", "x64", ninja, targets=["unsupported"])
                self.assertEqual(baseline["samples"], [])
                result = subprocess.run([str(ninja), "unsupported"], cwd=out, check=True, capture_output=True)
                report = evidence.after(work, "linux", "x64", ninja, targets=["unsupported"], exit_code=result.returncode)
                self.assertEqual(report["retained_count"], 0)
                self.assertEqual(report["status"], "unproven")
                self.assertFalse(report["retention_proven"])

    def test_real_absolute_output_alias_cp_p_cannot_prove_retention_v5_v6_v7(self):
        for ninja in AVAILABLE:
            with self.subTest(ninja=ninja), tempfile.TemporaryDirectory() as tmp:
                work, out = self.make_work(Path(tmp), ninja)
                alias = str(out / "a.o")
                shutil.copy2(out / "a.o", out / "alias-source")
                build = out / "build.ninja"
                text = build.read_text().replace("| b.obj ||", f"| b.obj {alias} ||")
                text += ("rule preserve\n  command = cp -p $in $out\n"
                         "build force: phony\n"
                         f"build {alias}: preserve alias-source || force\n")
                build.write_text(text)
                baseline = evidence.before(work, "linux", "x64", ninja)
                selected = {sample["output"] for sample in baseline["samples"]}
                self.assertEqual(selected, {"a.o", "b.obj", "ordered.o"})
                self.assertEqual(baseline["membership"]["excluded_object_inputs"], 1)
                expected_version = {"chromix-ninja-v1.11.1": 5, "chromix-ninja-v1.12.1": 6, "chromix-ninja-v1.13.2": 7}
                self.assertEqual(baseline["log"]["version"], expected_version[ninja.parent.name])
                baseline_path = work / "upstream-reuse/baseline.json"
                saved = baseline_path.read_bytes()
                prefix = (out / ".ninja_log").read_bytes()
                result = subprocess.run([str(ninja), "chrome", "-v"], cwd=out, check=True, capture_output=True)
                self.assertIn(f"cp -p alias-source {alias}".encode(), result.stdout)
                raw = (out / ".ninja_log").read_bytes()
                self.assertEqual(raw[:len(prefix)], prefix)
                self.assertIn(f"\t{alias}\t".encode(), raw[len(prefix):])
                records = evidence.read_log(out, selected)[0]
                for sample in baseline["samples"]:
                    self.assertEqual(records[sample["output"]], sample["record"])
                    self.assertEqual(evidence.file_record(out / sample["output"]), sample["file"])
                report = evidence.after(work, "linux", "x64", ninja, exit_code=result.returncode)
                expected = dict.fromkeys(selected, "unsupported_appended_output")
                self.assertEqual(report["disqualified"], expected)
                self.assertEqual(report["disqualification_reasons"], {"unsupported_appended_output": 3})
                self.assertEqual(report["retained_count"], 0)
                self.assertEqual(report["retained_bytes"], 0)
                self.assertFalse(report["retention_proven"])
                self.assertTrue(report["log"]["prefix_matches_previous"])
                self.assertEqual(evidence.before(work, "linux", "x64", ninja), baseline)
                result = subprocess.run([str(ninja), "chrome"], cwd=out, check=True, capture_output=True)
                report = evidence.after(work, "linux", "x64", ninja, exit_code=result.returncode)
                self.assertEqual(report["disqualified"], expected)
                self.assertFalse(report["retention_proven"])
                self.assertEqual(baseline_path.read_bytes(), saved)

    def test_real_inputs_ignores_deps_database_and_does_not_modify_build_files(self):
        for ninja in AVAILABLE:
            with self.subTest(ninja=ninja), tempfile.TemporaryDirectory() as tmp:
                work, out = self.make_work(Path(tmp), ninja)
                (out / "header").write_text("implicit compiler dependency\n")
                build = out / "build.ninja"
                build.write_text(build.read_text() +
                    "rule depcopy\n"
                    "  command = cp source $out && printf '$out: source header\\n' > $out.d\n"
                    "  depfile = $out.d\n  deps = gcc\n"
                    "build dep.o: depcopy source\n"
                    "build dep-archive: copy dep.o\n")
                before = evidence.target_inputs(ninja, out, ["dep-archive"])
                subprocess.run([str(ninja), "dep-archive"], cwd=out, check=True, capture_output=True)
                deps = out / ".ninja_deps"
                self.assertGreater(deps.stat().st_size, 16)
                dep_query = subprocess.run([str(ninja), "-t", "deps", "dep.o"], cwd=out,
                                           check=True, capture_output=True)
                self.assertIn(b"header", dep_query.stdout)
                for content in (deps.read_bytes(), b"invalid deps log, must not be loaded or repaired\n"):
                    with self.subTest(valid=content.startswith(b"# ninjadeps")):
                        deps.write_bytes(content)
                        files = {path: (path.read_bytes(), evidence.stamp(path.stat()))
                                 for path in out.rglob("*") if path.is_file()}
                        self.assertEqual(evidence.target_inputs(ninja, out, ["dep-archive"]), before)
                        self.assertEqual(evidence.query(ninja, out, ["--version"], 128).strip(),
                                         ninja.parent.name.removeprefix("chromix-ninja-v").encode())
                        after = {path: (path.read_bytes(), evidence.stamp(path.stat()))
                                 for path in out.rglob("*") if path.is_file()}
                        self.assertEqual(after, files)

    def test_real_query_nonzero_reports_ninja_stderr(self):
        for ninja in AVAILABLE:
            with self.subTest(ninja=ninja), tempfile.TemporaryDirectory() as tmp:
                work, out = self.make_work(Path(tmp), ninja)
                with self.assertRaises(evidence.EvidenceError) as raised:
                    evidence.query(ninja, out, ["-t", "inputs", "nonexistent"], evidence.MAX_INPUT_BYTES)
                reason = str(raised.exception)
                for text in ("failed (nonzero exit)", "returncode=1", "unknown target", "nonexistent"):
                    self.assertIn(text, reason)

    def test_query_stdout_cap_on_real_ninja(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, out = self.make_work(Path(tmp), AVAILABLE[0])
            with self.assertRaisesRegex(evidence.EvidenceError, "stdout byte cap"):
                evidence.query(AVAILABLE[0], out, ["-t", "inputs", "chrome"], 4)


if __name__ == "__main__":
    unittest.main()
