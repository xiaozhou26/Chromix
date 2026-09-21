import errno
import hashlib
import io
import json
import os
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import fetch_upstream_cache as fetcher
from tools import restore_upstream_cache as restore
from tools.tests.test_fetch_upstream_cache import synthetic_windows_source


def windows_source(repo):
    source = synthetic_windows_source(repo)
    source["artifacts"]["arm64"] = dict(source["artifacts"]["x64"], id=104, run_id=103,
        name="build-artifact-arm", workflow_path=".github/workflows/build-arm.yml")
    return source


def archive_bytes(entries, zipped=False):
    output = io.BytesIO()
    if zipped:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, kind, data in entries:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = ((stat.S_IFLNK | 0o777) if kind == "sym"
                                      else (stat.S_IFREG | 0o644)) << 16
                archive.writestr(info, data)
    else:
        with tarfile.open(fileobj=output, mode="w") as archive:
            for name, kind, data in entries:
                info = tarfile.TarInfo(name)
                info.mode = 0o777 if kind == "sym" else 0o644
                if kind == "sym":
                    info.type, info.linkname = tarfile.SYMTYPE, data
                else:
                    info.size = len(data)
                archive.addfile(info, io.BytesIO(data) if kind == "file" else None)
    return output.getvalue()


def write_ninja_metadata(out, records, version=5):
    names = dict.fromkeys(name for output, _, _, inputs in records for name in (output, *inputs))
    ids = {name: index for index, name in enumerate(names)}
    raw = bytearray(b"# ninjadeps\n\x04\x00\x00\x00")
    for name, index in ids.items():
        encoded = name.encode()
        payload = encoded + b"\0" * (-len(encoded) % 4) + struct.pack("<I", ~index & 0xffffffff)
        raw += struct.pack("<I", len(payload)) + payload
    for output, recorded, _, inputs in records:
        payload = struct.pack(f"<{len(inputs) + 3}I", ids[output], recorded & 0xffffffff,
                              recorded >> 32, *(ids[name] for name in inputs))
        raw += struct.pack("<I", 0x80000000 | len(payload)) + payload
    (out / ".ninja_deps").write_bytes(raw)
    (out / ".ninja_log").write_text(f"# ninja log v{version}\n" + "".join(
        f"0\t1\t{logged}\t{output}\t123456789abcdef0\n" for output, _, logged, _ in records))


class RestoreUpstreamCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.work = root / "work"
        self.cache = root / "cache"
        for relative in ("CHROMIUM_VERSION", "CHROMIUM_LINUX_VERSION", "CHROMIUM_MACOS_VERSION", "CHROMIUM_WINDOWS_VERSION",
                         "build/ungoogled-revisions.psd1", "build/upstream-cache.json"):
            source = restore.REPO / relative
            if relative in ("CHROMIUM_LINUX_VERSION", "CHROMIUM_MACOS_VERSION", "CHROMIUM_WINDOWS_VERSION") and not source.exists():
                continue
            destination = self.repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        self.platform, self.arch = "linux", "x64"
        self.make_cache()

    def write(self, path, value, mode=0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode() if isinstance(value, str) else value)
        path.chmod(mode)

    def make_cache(self, platform="linux", arch="x64"):
        self.platform, self.arch = platform, arch
        if platform == "windows":
            path = self.repo / "build/upstream-cache.json"
            manifest = json.loads(path.read_text())
            manifest["sources"]["windows"] = windows_source(self.repo)
            self.write(path, json.dumps(manifest))
        if self.cache.exists():
            shutil.rmtree(self.cache)
        self.cache.mkdir()
        identity, pin, manifest = restore.identities(self.repo, platform, arch)
        root_name = pin["source_roots"][0]
        self.donor = self.cache / "tree" / root_name
        version = "\n".join(f"{key}={value}" for key, value in zip(
            ("MAJOR", "MINOR", "BUILD", "PATCH"), identity["chromium_version"].split("."))) + "\n"
        for relative, value in {
            "chrome/VERSION": version,
            "BUILD.gn": "group(\"fixture\") {}\n",
            "out/Default/args.gn": f'target_cpu = "{arch}"\nis_debug = false\n',
            "out/Default/build.ninja": "# tiny fixture\n",
            "out/Default/.ninja_log": "# ninja log v5\n",
            "out/Default/.ninja_deps": b"# ninjadeps\n\x04\x00\x00\x00",
            "chrome/source.cc": "upstream\n",
        }.items():
            self.write(self.donor / relative, value)
        self.write(self.donor / "out/Default/obj/output.o", b"object")
        result = {
            "owner": fetcher.OWNER, "status": "hit", "source": str(self.donor),
            "destination": str(self.cache), "platform": platform, "arch": arch,
            "manifest": manifest, "extraction_scope": fetcher.SOURCE_SCOPE,
            "skipped_external_symlinks": 0, "external_symlink_paths": [],
        }
        self.write(self.cache / "result.json", json.dumps(result))
        self.result = result

    def fetch_cache(self, platform="linux", arch="x64", source=None, extra=()):
        if platform != "windows" and not shutil.which("zstd"):
            self.skipTest("host zstd is unavailable")
        self.make_cache(platform, arch)
        source = source or self.donor.relative_to(self.cache / "tree").as_posix()
        entries = [(f"{source}/{path.relative_to(self.donor).as_posix()}", "file", path.read_bytes())
                   for path in sorted(self.donor.rglob("*")) if path.is_file()]
        entries += list(extra)
        inner = archive_bytes(entries, zipped=platform == "windows")
        if platform != "windows":
            inner = subprocess.check_output([shutil.which("zstd"), "-q", "-c"], input=inner)
        manifest_path = self.repo / "build/upstream-cache.json"
        manifest = json.loads(manifest_path.read_text())
        artifact = manifest["sources"][platform]["artifacts"][arch]
        outer = archive_bytes([(artifact["inner_archive"], "file", inner)], zipped=True)
        artifact.update(digest="sha256:" + hashlib.sha256(outer).hexdigest(), size_in_bytes=len(outer))
        self.write(manifest_path, json.dumps(manifest))
        pin, _ = fetcher.load_manifest(platform, arch, root=self.repo)
        repository = {"full_name": pin["repository"], "id": pin["repository_id"], "private": False}
        run = {key: pin[key] for key in ("head_sha", "head_branch", "event")}
        run.update(id=pin["run_id"], path=pin["workflow_path"], status="completed", conclusion="success",
                   repository=repository, head_repository=repository)
        metadata = dict(artifact, expired=False, expires_at="2099-01-01T00:00:00Z", workflow_run={
            "id": pin["run_id"], "head_sha": pin["head_sha"], "head_branch": pin["head_branch"],
            "repository_id": pin["repository_id"], "head_repository_id": pin["repository_id"]})
        client = fetcher.GitHub("fixture-token")
        responses = [io.BytesIO(json.dumps(run).encode()),
                     io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(outer)]
        for response in responses:
            response.status = 200
        client.open = mock.Mock(side_effect=responses)
        shutil.rmtree(self.cache)
        with mock.patch.object(fetcher, "require_space"), \
                mock.patch("sys.stderr", new=io.StringIO()), \
                mock.patch.object(client.opener, "open", side_effect=AssertionError("no network")):
            self.result = fetcher.fetch(platform, arch, self.cache, root=self.repo, client=client)
        self.assertEqual(client.open.call_count, 3)
        self.assertEqual(json.loads((self.cache / "result.json").read_text()), self.result)
        self.donor = self.cache / "tree" / source
        return self.result

    def invoke(self, phase="restore", cache=True):
        return restore.run_restore(phase, self.platform, self.arch, self.work,
                                   self.cache if cache else None, repo=self.repo)

    def timestamp_outputs(self):
        out = self.donor / "out/Default"
        recorded = 1_700_000_000_123_456_789
        floor = recorded // 10**9 * 10**9
        os.utime(self.donor / "chrome/source.cc", ns=(floor - 10**9,) * 2)
        records = []
        for name in ("obj/output.o", "obj/second.o"):
            self.write(out / name, name)
            os.utime(out / name, ns=(floor,) * 2)
            records.append((name, recorded, recorded, ["../../chrome/source.cc"]))
        write_ninja_metadata(out, records)
        return recorded, floor

    def donor_snapshot(self):
        return {path.relative_to(self.donor): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.donor.rglob("*") if path.is_file()}

    def test_restore_moves_complete_source_and_writes_receipt(self):
        source_mtime = (self.donor / "chrome/source.cc").stat().st_mtime_ns
        output_mtime = (self.donor / "out/Default/obj/output.o").stat().st_mtime_ns
        entry = self.invoke()
        self.assertEqual(entry["status"], "hit", entry)
        src = self.work / "src"
        self.assertFalse(self.donor.exists())
        self.assertTrue((src / "out/Default/build.ninja").exists())
        self.assertEqual((src / "chrome/source.cc").stat().st_mtime_ns, source_mtime)
        self.assertEqual((src / "out/Default/obj/output.o").stat().st_mtime_ns, output_mtime)
        receipt = restore.verify_restored(self.work, "linux", "x64", self.repo)
        self.assertEqual(receipt["owner"], restore.OWNER)
        self.assertEqual(receipt["original_args"]["bytes"], len(receipt["original_args"]["text"].encode()))
        self.assertFalse((src / ".chromix-source-ready").exists())
        self.assertFalse((src / ".chromix-patches").exists())

    def test_phase_reports_precede_work_and_preserve_receipt_semantics(self):
        counts = restore.donor_counts(self.donor)
        recorded = 1_700_000_000_123_456_789
        plan = {"outputs_restored": 1, "repairs": [{"output": "obj/output.o",
                "from_ns": recorded // 10**9 * 10**9, "to_ns": recorded}], "skipped": {"fixture": 2}}
        output = self.donor / "out/Default/obj/output.o"
        os.utime(output, ns=(plan["repairs"][0]["from_ns"],) * 2)
        observed = []
        install = restore.install

        def check(phase):
            report = json.loads((self.work / restore.REPORT).read_text())
            self.assertEqual(report["phase"], phase)
            self.assertEqual(report["status"], "miss")
            self.assertEqual(report["counts"]["files_moved"], 0)
            self.assertEqual(report["ninja_state"][".ninja_log"]["header_hex"], b"# ninja log v5\n".hex())
            self.assertNotIn("receipt", report)
            self.assertGreaterEqual(report["duration_seconds"], 0)
            observed.append(report)
            return report

        def counted(src):
            self.assertEqual(check("donor_counts")["progress"]["files_counted"], 0)
            return counts

        def planned(src):
            self.assertEqual(check("ninja_mtime_plan")["progress"]["files_counted"], counts["files_moved"])
            return plan

        def installed(donor, work, receipt):
            self.assertEqual(check("install")["progress"]["outputs_planned"], 1)
            self.assertNotIn("phase", receipt)
            self.assertNotIn("progress", receipt)
            self.assertEqual(receipt["ninja_mtimes"], plan)
            install(donor, work, receipt)

        with mock.patch.object(restore, "donor_counts", side_effect=counted), \
                mock.patch.object(restore, "ninja_mtime_plan", side_effect=planned), \
                mock.patch.object(restore, "install", side_effect=installed), \
                mock.patch("builtins.print") as printed:
            entry = self.invoke()
        self.assertEqual(entry["status"], "hit", entry)
        self.assertEqual(entry["phase"], "complete")
        self.assertEqual(entry["counts"], counts)
        self.assertEqual(entry["progress"]["outputs_skipped"], 2)
        self.assertEqual(set(entry["phase_durations_seconds"]),
                         {"validate_cache", "validate_source", "donor_counts", "ninja_mtime_plan", "install"})
        self.assertTrue(all(value >= 0 for value in entry["phase_durations_seconds"].values()))
        self.assertEqual(len(observed), 3)
        messages = [call for call in printed.call_args_list if "upstream cache restore: phase=" in call.args[0]]
        self.assertEqual(len(messages), 6)
        self.assertTrue(all(call.kwargs == {"file": sys.stderr, "flush": True} for call in messages))
        self.assertEqual(json.loads((self.work / restore.REPORT).read_text()),
                         {key: value for key, value in entry.items() if key != "receipt"})

    def test_failed_phase_and_headers_are_written_before_owned_cleanup(self):
        cleanup = restore._cleanup_owned_miss

        def inspect_cleanup(cache, result, reason):
            report = json.loads((self.work / restore.REPORT).read_text())
            self.assertEqual(report["phase"], "failed")
            self.assertEqual(report["failed_phase"], "ninja_mtime_plan")
            self.assertEqual(report["reasons"], ["fixture plan failure"])
            self.assertGreater(report["progress"]["files_counted"], 0)
            self.assertIn("ninja_mtime_plan", report["phase_durations_seconds"])
            self.assertIn("header_hex", report["ninja_state"][".ninja_deps"])
            self.assertTrue(self.donor.exists())
            return cleanup(cache, result, reason)

        with mock.patch.object(restore, "ninja_mtime_plan", side_effect=restore.Miss("fixture plan failure")), \
                mock.patch.object(restore, "_cleanup_owned_miss", side_effect=inspect_cleanup) as cleaned, \
                mock.patch.object(restore, "install") as install:
            entry = self.invoke()
        cleaned.assert_called_once()
        install.assert_not_called()
        self.assertEqual(entry["phase"], "failed")
        self.assertEqual(entry["cleanup"]["status"], "removed")
        self.assertFalse((self.cache / ".lock").exists())
        self.assertEqual(json.loads((self.work / restore.REPORT).read_text()), entry)

    def test_interrupted_counting_leaves_early_report_with_ninja_headers(self):
        with mock.patch.object(restore, "donor_counts", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.invoke()
        report = json.loads((self.work / restore.REPORT).read_text())
        self.assertEqual(report["phase"], "failed")
        self.assertEqual(report["failed_phase"], "donor_counts")
        self.assertIn("header_hex", report["ninja_state"][".ninja_log"])
        self.assertEqual(report["cleanup"]["status"], "preserved")
        self.assertTrue(self.donor.exists())
        self.assertFalse((self.cache / ".lock").exists())

    def test_early_reports_reject_a_report_link_appearing_during_validation(self):
        outside = Path(self.tmp.name) / "outside-report"
        outside.write_text("unowned")
        original = restore.source_args

        def linked_report(src, identity):
            report = self.work / restore.REPORT
            report.unlink()
            report.symlink_to(outside)
            return original(src, identity)

        with mock.patch.object(restore, "source_args", side_effect=linked_report), \
                mock.patch.object(restore, "donor_counts") as counts, \
                mock.patch.object(restore, "install") as install:
            with self.assertRaisesRegex(restore.LocalError, "diagnostic report is symlinked"):
                self.invoke()
        counts.assert_not_called()
        install.assert_not_called()
        self.assertEqual(outside.read_text(), "unowned")
        self.assertTrue(self.donor.exists())
        self.assertFalse((self.cache / ".lock").exists())

    def test_mutating_input_rejects_restore_without_installing_a_plan(self):
        out = self.donor / "out/Default"
        source = self.donor / "chrome/source.cc"
        recorded = 1_700_000_000_123_456_789
        floor = recorded // 10**9 * 10**9
        os.utime(source, ns=(floor - 10**9,) * 2)
        os.utime(out / "obj/output.o", ns=(floor,) * 2)
        write_ninja_metadata(out, [("obj/output.o", recorded, recorded, ["../../chrome/source.cc"])])
        fresh = restore.objects.input_is_fresh

        def mutate(mtime, record):
            source.write_text("changed during planning")
            return fresh(mtime, record)

        with mock.patch.object(restore.objects, "input_is_fresh", side_effect=mutate), \
                mock.patch.object(restore, "install") as install:
            entry = self.invoke()
        install.assert_not_called()
        self.assertEqual(entry["status"], "miss")
        self.assertEqual(entry["failed_phase"], "ninja_mtime_plan")
        self.assertIn("input changed during timestamp planning", entry["reasons"][0])
        self.assertFalse((self.work / "src").exists())

    def test_end_pass_io_error_preserves_donor_and_verified_fetch_receipt(self):
        self.timestamp_outputs()
        before = self.donor_snapshot()
        result_path = self.cache / "result.json"
        receipt = (result_path.read_bytes(), result_path.stat().st_mtime_ns)
        relative = restore.objects.relative_path
        attempts = 0

        def fail_revalidation(value, *args, **kwargs):
            nonlocal attempts
            if value == "../../chrome/source.cc":
                attempts += 1
                if attempts == 2:
                    raise OSError(errno.EIO, "one-shot end-pass input failure")
            return relative(value, *args, **kwargs)

        with mock.patch.object(restore.objects, "relative_path", side_effect=fail_revalidation), \
                mock.patch.object(restore, "install") as install, \
                mock.patch.object(restore, "_cleanup_owned_miss") as cleanup, \
                mock.patch.object(restore.shutil, "rmtree") as remove:
            entry = self.invoke()
        self.assertEqual(attempts, 2)
        install.assert_not_called()
        cleanup.assert_not_called()
        remove.assert_not_called()
        self.assertEqual(entry["status"], "miss")
        self.assertEqual(entry["failed_phase"], "ninja_mtime_plan")
        self.assertEqual(entry["cleanup"]["status"], "preserved")
        self.assertIn("one-shot end-pass input failure", entry["reasons"][0])
        self.assertEqual(self.donor_snapshot(), before)
        self.assertEqual((result_path.read_bytes(), result_path.stat().st_mtime_ns), receipt)
        self.assertFalse((self.work / "src").exists())
        self.assertFalse((self.cache / ".lock").exists())
        self.assertEqual(self.invoke()["status"], "hit")

    def test_windows_arm64_restore_keeps_source_objects_and_selected_pin_identity(self):
        self.make_cache("windows", "arm64")
        before = self.donor_snapshot()
        entry = self.invoke()
        self.assertEqual(entry["status"], "hit", entry)
        receipt = restore.verify_restored(self.work, "windows", "arm64", self.repo)
        self.assertEqual(receipt["identity"]["chromium_version"], "153.0.8010.47")
        self.assertEqual(receipt["identity"]["run_id"], 103)
        self.assertEqual(receipt["identity"]["artifact_id"], 104)
        self.assertEqual(receipt["identity"]["workflow_path"], ".github/workflows/build-arm.yml")
        for relative, expected in before.items():
            path = self.work / "src" / relative
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)
        with self.assertRaisesRegex(restore.Miss, "receipt does not match"):
            restore.verify_restored(self.work, "windows", "x64", self.repo)

    def test_windows_arm64_wrong_version_target_or_host_args_fail_closed(self):
        for name, value in (("target_cpu", "x64"), ("v8_target_cpu", "x64"),
                            ("host_cpu", "arm64"), ("target_os", "linux")):
            with self.subTest(name=name):
                self.make_cache("windows", "arm64")
                self.write(self.donor / "out/Default/args.gn",
                           f'target_cpu = "arm64"\n{name} = "{value}"\n')
                entry = self.invoke()
                self.assertEqual(entry["status"], "miss", entry)
                self.assertIn(f"GN {name} does not match", entry["reasons"][0])
                self.assertFalse((self.work / "src").exists())
        self.make_cache("windows", "arm64")
        self.write(self.donor / "chrome/VERSION", "MAJOR=153\nMINOR=0\nBUILD=8010\nPATCH=36\n")
        self.assertEqual(self.invoke()["status"], "miss")
        self.assertFalse((self.work / "src").exists())

    def test_all_six_manifest_targets_restore(self):
        for platform, arch in (("linux", "x64"), ("linux", "arm64"),
                               ("macos", "x64"), ("macos", "arm64"),
                               ("windows", "x64"), ("windows", "arm64")):
            for version in (5, 6, 7):
                with self.subTest(platform=platform, arch=arch, version=version):
                    self.make_cache(platform, arch)
                    self.work = Path(self.tmp.name) / f"work-{platform}-{arch}-v{version}"
                    header = f"# ninja log v{version}\n".encode()
                    self.write(self.donor / "out/Default/.ninja_log", header)
                    self.assertEqual(self.invoke()["status"], "hit")
                    self.assertEqual((self.work / "src/out/Default/.ninja_log").read_bytes(), header)
                    self.assertEqual(self.invoke("verify", cache=False)["status"], "verified")

    def test_fetch_then_restore_all_five_targets_with_archive_relative_omissions(self):
        linux_links = ["buildtools/linux64-format/clang-format",
                       "third_party/dawn/tools/golang/linux-amd64/bin/go",
                       "third_party/node/linux/node-linux-x64/bin/node",
                       "third_party/gperf/cipd/bin/gperf"]
        for platform, arch, source in (("linux", "x64", "build/src"), ("linux", "arm64", "build/src"),
                                       ("macos", "x64", "src"), ("macos", "arm64", "src"),
                                       ("windows", "x64", "src"), ("windows", "x64", "build/src")):
            with self.subTest(platform=platform, arch=arch, source=source):
                if platform == "linux":
                    relative = linux_links
                elif platform == "macos":
                    cpu = "amd64" if arch == "x64" else "arm64"
                    relative = [f"third_party/dawn/tools/golang/mac-{cpu}/bin/go",
                                "out/Default/sdk/xcode_links/MacOSX26.0.sdk",
                                "out/Default/sdk/xcode_links/MacOSX.platform",
                                "out/Default/sdk/xcode_links/XcodeDefault.xctoolchain"]
                else:
                    relative = ["third_party/gperf/cipd/bin/gperf"]
                archive_paths = [f"{source}/{name}" for name in relative]
                extra = [(name, "sym", "/unavailable-host/tool") for name in archive_paths]
                result = self.fetch_cache(platform, arch, source, extra)
                self.assertEqual(result["status"], "hit", result)
                self.assertEqual(result["source"], str(self.donor))
                self.assertEqual(result["external_symlink_paths"], archive_paths)
                self.assertEqual(result["skipped_external_symlinks"], len(archive_paths))
                self.work = Path(self.tmp.name) / f"work-{platform}-{arch}-{source.replace('/', '-')}"
                entry = self.invoke()
                self.assertEqual(entry["status"], "hit", entry)
                receipt = entry["receipt"]
                self.assertEqual(receipt["external_symlink_paths"], sorted(relative))
                self.assertEqual(receipt["archive_external_symlink_paths"], archive_paths)
                for name in relative:
                    path = self.work / "src" / name
                    self.assertFalse(path.exists() or restore.linked(path), name)
                self.assertEqual((self.work / "src/chrome/source.cc").read_bytes(), b"upstream\n")
                self.assertEqual((self.work / "src/out/Default/obj/output.o").read_bytes(), b"object")
                self.assertFalse(self.donor.exists())
                self.assertEqual(self.invoke("verify", cache=False)["receipt"], receipt)
                self.assertEqual(json.loads((self.cache / "result.json").read_text())["status"], "consumed")

    def test_fetch_then_restore_rejects_unknown_and_outside_donor_omissions(self):
        known = "third_party/gperf/cipd/bin/gperf"
        for platform, source in (("linux", "build/src"), ("macos", "src"), ("windows", "src")):
            names = [f"{source}/unknown/tool"]
            if platform == "windows":
                names.append("build/src/" + known)
            if platform != "macos":
                names.append(f"{source}/third_party/dawn/tools/golang/mac-arm64/bin/go")
            for name in names:
                with self.subTest(platform=platform, name=name):
                    outside = Path(self.tmp.name) / "outside/keep"
                    self.write(outside, "unowned")
                    result = self.fetch_cache(platform, source=source, extra=[(name, "sym", str(outside))])
                    self.assertEqual(result["status"], "hit", result)
                    self.assertEqual(result["external_symlink_paths"], [name])
                    entry = self.invoke()
                    self.assertEqual(entry["status"], "miss", entry)
                    self.assertIn("unknown external symlink", entry["reasons"][0])
                    self.assertEqual(entry["cleanup"]["status"], "removed", entry)
                    self.assertFalse((self.cache / "tree").exists())
                    self.assertFalse((self.work / "src").exists())
                    self.assertEqual(outside.read_text(), "unowned")

    def test_fetch_then_restore_rejects_corrupt_omission_receipts(self):
        name = "src/third_party/gperf/cipd/bin/gperf"
        cases = [(1, []), (0, [name]), (2, [name, name]), (True, [name]), (-1, []),
                 ("1", [name]), (1, None), (1, name), (1, [42]), (1, [""]),
                 (1, ["../" + name]), (1, ["/" + name]), (1, ["C:/" + name]),
                 (1, [name.replace("/", "\\")]), (1, ["./" + name]), (1, ["tree/" + name]),
                 (1, [name.replace("src/", "src/../src/")]),
                 (1, [name.replace("src/", "src//")]), (1, [name + "/"]),
                 (1, ["src-backup/" + name[4:]]), (1, ["foreign/" + name]),
                 (1, ["build/download_cache/" + name[4:]]), (1, ["build/" + name])]
        for count, paths in cases:
            with self.subTest(count=count, paths=paths):
                result = self.fetch_cache("windows", extra=[(name, "sym", "/unavailable-host/gperf")])
                self.assertEqual(result["status"], "hit", result)
                self.result.update(skipped_external_symlinks=count, external_symlink_paths=paths)
                self.write(self.cache / "result.json", json.dumps(self.result))
                entry = self.invoke()
                self.assertEqual(entry["status"], "miss", entry)
                self.assertEqual(entry["cleanup"]["status"], "removed", entry)
                self.assertFalse((self.cache / "tree").exists())
                self.assertFalse((self.work / "src").exists())

    def test_fetch_then_restore_rejects_existing_omissions_and_linked_parents(self):
        relative = "third_party/gperf/cipd/bin/gperf"
        name = "src/" + relative
        for kind in ("file", "directory", "symlink", "dangling_symlink", "internal_parent", "external_parent"):
            with self.subTest(kind=kind):
                outside = Path(self.tmp.name) / "outside/keep"
                self.write(outside, "unowned")
                result = self.fetch_cache("windows", extra=[(name, "sym", str(outside))])
                self.assertEqual(result["status"], "hit", result)
                path = self.donor / relative
                path.parent.mkdir(parents=True)
                if kind == "file":
                    path.write_text("not omitted")
                elif kind == "directory":
                    path.mkdir()
                elif kind in ("symlink", "dangling_symlink"):
                    path.symlink_to(outside if kind == "symlink" else outside.with_name("missing"))
                else:
                    path.parent.rmdir()
                    target = self.donor / "chrome" if kind == "internal_parent" else outside.parent
                    path.parent.symlink_to(target, target_is_directory=True)
                entry = self.invoke()
                self.assertEqual(entry["status"], "miss", entry)
                self.assertRegex(entry["reasons"][0], "unexpectedly exists|symlinked directory or input")
                self.assertEqual(entry["cleanup"]["status"], "removed", entry)
                self.assertFalse((self.cache / "tree").exists())
                self.assertFalse((self.work / "src").exists())
                self.assertEqual(outside.read_text(), "unowned")

    def test_missing_cache_is_normal_miss_and_does_not_create_source(self):
        shutil.rmtree(self.cache)
        entry = self.invoke()
        self.assertEqual(entry["status"], "miss")
        self.assertFalse((self.work / "src").exists())
        self.assertEqual(restore.main(["--phase", "restore", "--platform", "linux", "--arch", "x64",
                                       "--workdir", str(self.work), "--cache-dir", str(self.cache)]), 0)

    def test_existing_source_is_never_overwritten(self):
        self.write(self.work / "src/keep", "canonical")
        entry = self.invoke()
        self.assertEqual(entry["status"], "miss")
        self.assertEqual((self.work / "src/keep").read_text(), "canonical")
        self.assertTrue(self.donor.exists())

    def test_chromix_marker_and_unknown_omitted_link_are_rejected(self):
        self.write(self.donor / ".chromix-patches", "must be prepared later")
        entry = self.invoke()
        self.assertEqual(entry["status"], "miss")
        self.assertFalse(self.donor.exists())
        self.make_cache()
        self.result["skipped_external_symlinks"] = 1
        self.result["external_symlink_paths"] = ["build/src/unknown/tool"]
        self.write(self.cache / "result.json", json.dumps(self.result))
        self.assertEqual(self.invoke()["status"], "miss")
        self.assertFalse(self.donor.exists())
        self.assertFalse((self.work / "src").exists())

    def test_mac_external_omissions_allow_exact_tools_and_xcode_links(self):
        self.make_cache("macos", "arm64")
        relative = [
            "third_party/dawn/tools/golang/mac-arm64/bin/go",
            "third_party/dawn/tools/golang/mac-amd64/bin/go",
            "out/Default/sdk/xcode_links/MacOSX26.0.sdk",
            "out/Default/sdk/xcode_links/MacOSX26.sdk",
            "out/Default/sdk/xcode_links/MacOSX26.0.1.sdk",
            "out/Default/sdk/xcode_links/MacOSX.sdk",
            "out/Default/sdk/xcode_links/MacOSX.platform",
            "out/Default/sdk/xcode_links/XcodeDefault.xctoolchain",
        ]
        archive_paths = [f"src/{name}" for name in relative]
        self.result.update(skipped_external_symlinks=len(archive_paths),
                           external_symlink_paths=archive_paths)
        self.write(self.cache / "result.json", json.dumps(self.result))
        entry = self.invoke()
        self.assertEqual(entry["status"], "hit", entry)
        self.assertEqual(entry["receipt"]["external_symlink_paths"], sorted(relative))
        self.assertFalse(any((self.work / "src" / name).exists() for name in relative))
        self.assertEqual(self.invoke("verify", cache=False)["status"], "verified")
        for name in relative:
            self.write(self.work / "src" / name, "host link regenerated by GN")
        self.assertEqual(self.invoke("verify", cache=False)["status"], "verified")

    def test_mac_external_omissions_reject_unknown_or_nested_xcode_paths(self):
        self.make_cache("macos", "x64")
        for name in ("third_party/dawn/tools/golang/mac-x64/bin/go",
                     "out/Default/sdk/xcode_links/MacOSX26.sdk/Headers",
                     "out/Default/sdk/xcode_links/MacOSX26.0.sdk/Headers",
                     "out/Default/sdk/xcode_links/MacOSX26.beta.sdk",
                     "out/Default/sdk/xcode_links/MacOSX26..0.sdk",
                     "out/Default/sdk/xcode_links/MacOSX26.0.sdk.extra",
                     "out/Default/sdk/xcode_links/MacOSX.platform/Headers",
                     "out/Default/sdk/xcode_links/../MacOSX26.0.sdk",
                     "out/Default/sdk/xcode_links/MacOSX26.0.sdk\\evil",
                     "out/Default/sdk/xcode_links/XcodeDefault.xctoolchain/bin/clang"):
            with self.subTest(name=name):
                self.make_cache("macos", "x64")
                archive_name = "src/" + name
                self.result.update(skipped_external_symlinks=1,
                                   external_symlink_paths=[archive_name])
                self.write(self.cache / "result.json", json.dumps(self.result))
                self.assertEqual(self.invoke()["status"], "miss")
                self.assertFalse(self.donor.exists())

    def test_non_macos_cannot_use_macos_external_omissions(self):
        name = "build/src/third_party/dawn/tools/golang/mac-arm64/bin/go"
        self.result.update(skipped_external_symlinks=1, external_symlink_paths=[name])
        self.write(self.cache / "result.json", json.dumps(self.result))
        self.assertEqual(self.invoke()["status"], "miss")
        self.assertFalse(self.donor.exists())

    def test_real_core_mac_and_windows_flags_concatenation_last_wins(self):
        core = (
            "chrome_pgo_phase=0\nclang_use_chrome_plugins=false\n"
            "disable_fieldtrial_testing_config=true\nenable_hangout_services_extension=false\n"
            "enable_mdns=false\nenable_remoting=false\nenable_reporting=false\n"
            "enable_service_discovery=false\nenable_widevine=true\nexclude_unwind_tables=true\n"
            'google_api_key=""\ngoogle_default_client_id=""\ngoogle_default_client_secret=""\n'
            "safe_browsing_mode=0\ntreat_warnings_as_errors=false\n"
            "use_official_google_api_keys=false\nuse_unofficial_version_number=false\n"
            "v8_drumbrake_bounds_checks=true\n")
        mac = (
            "blink_symbol_level=0\nchrome_pgo_phase=2\nenable_iterator_debugging=false\n"
            "enable_mse_mpeg2ts_stream_parser=true\nenable_rust=true\nenable_swiftshader=true\n"
            'enable_updater=false\nfatal_linker_warnings=false\nffmpeg_branding="Chrome"\n'
            "is_clang=true\nis_debug=false\nis_official_build=true\nproprietary_codecs=true\n"
            "symbol_level=1\nuse_thin_lto=true\nuse_sysroot=false\n")
        windows = (
            'chrome_pgo_phase=2\nenable_swiftshader=false\nffmpeg_branding="Chrome"\n'
            "is_clang=true\nis_component_build=false\nis_debug=false\nis_official_build=true\n"
            'proprietary_codecs=true\ntarget_cpu="x64"\nuse_sysroot=false\ndcheck_always_on=false\n'
            "blink_symbol_level=0\nv8_symbol_level=0\nsymbol_level=0\nenable_rust=true\n"
            "enable_mse_mpeg2ts_stream_parser=true\n")
        from tools import merge_gn_args
        for platform, arch, flags in (("macos", "x64", mac), ("macos", "arm64", mac),
                                      ("windows", "x64", windows)):
            with self.subTest(platform=platform, arch=arch):
                self.make_cache(platform, arch)
                self.work = Path(self.tmp.name) / f"work-{platform}-{arch}"
                raw = core + flags + f'target_cpu="{arch}"\n'
                path = self.donor / "out/Default/args.gn"
                self.write(path, raw)
                _, merged = merge_gn_args.parse(path)
                entry = self.invoke()
                self.assertEqual(entry["status"], "hit", entry)
                args = entry["receipt"]["original_args"]
                self.assertEqual(args["text"], raw)
                self.assertEqual(args["assignments"]["chrome_pgo_phase"], "2")
                self.assertEqual(args["assignments"]["target_cpu"], json.dumps(arch))
                for key, value in merged.items():
                    parsed = json.loads(value.split("=", 1)[1])
                    self.assertEqual(args["assignments"][key], json.dumps(parsed, separators=(",", ":")))
                self.assertEqual(self.invoke("verify", cache=False)["status"], "verified")

    def test_gn_last_arch_mismatch_and_invalid_overridden_values_reject(self):
        for text in (
                'target_cpu="arm64"\ntarget_cpu="x64"\n',
                'target_cpu="arm64"\nv8_target_cpu="arm64"\nv8_target_cpu="x64"\n',
                'target_cpu=getenv("ARCH")\ntarget_cpu="arm64"\n',
                'bad=true || false\nbad=true\ntarget_cpu="arm64"\n',
                'target_cpu="arm64" + "x"\n', 'target_cpu=["arm64",]\n',
                'target_cpu={"cpu":"arm64"}\n', 'target_cpu=null\n',
                'target_cpu="arm64"\nchrome_pgo_phase=2.5\n',
                'target_cpu="arm64"\nimport("execute.gni")\n'):
            with self.subTest(text=text):
                self.make_cache("macos", "arm64")
                self.write(self.donor / "out/Default/args.gn", text)
                entry = self.invoke()
                self.assertEqual(entry["status"], "miss", entry)
                self.assertFalse(self.donor.exists())

    def test_gn_literal_comments_and_lists_do_not_evaluate_expressions(self):
        path = Path(self.tmp.name) / "args.gn"
        self.write(path, '# core\nchrome_pgo_phase = 0\n# platform\nchrome_pgo_phase = 2 # override\n'
                   'literal = ["a", 1, false]\nsigned = -1\n')
        self.assertEqual(restore.gn_assignments_last_wins(path), {
            "chrome_pgo_phase": "2", "literal": '["a",1,false]', "signed": "-1"})

    def test_owned_validated_miss_cleans_only_tree_and_records_diagnostic(self):
        self.write(self.donor / "out/Default/args.gn", "target_cpu = \"arm64\"\n")
        entry = self.invoke()
        self.assertEqual(entry["status"], "miss")
        self.assertEqual(entry["cleanup"]["status"], "removed", entry)
        self.assertFalse((self.cache / "tree").exists())
        self.assertTrue((self.cache / "result.json").is_file())
        self.assertEqual(json.loads((self.cache / "result.json").read_text())["status"], "miss")
        self.assertIn("cleanup", json.loads((self.work / restore.REPORT).read_text()))

    def test_unowned_or_prevalidation_miss_preserves_tree(self):
        self.result["owner"] = "not-chromix"
        self.write(self.cache / "result.json", json.dumps(self.result))
        entry = self.invoke()
        self.assertEqual(entry["status"], "miss")
        self.assertEqual(entry["cleanup"]["status"], "preserved")
        self.assertTrue(self.donor.exists())

    def test_verify_requires_current_pins(self):
        self.assertEqual(self.invoke()["status"], "hit")
        manifest = json.loads((self.repo / "build/upstream-cache.json").read_text())
        manifest["chromium_version"] = "0.0.0.0"
        (self.repo / "build/upstream-cache.json").write_text(json.dumps(manifest))
        with self.assertRaises(restore.Miss):
            restore.verify_restored(self.work, "linux", "x64", self.repo)

    def test_is_restored_absent_and_present(self):
        self.assertIsNone(restore.is_restored(self.work, "linux", "x64", self.repo))
        self.assertEqual(self.invoke()["status"], "hit")
        self.assertEqual(restore.is_restored(self.work, "linux", "x64", self.repo)["status"], "restored")

    def test_receipt_owner_provenance_digest_scope_and_target_rejections(self):
        cases = [("owner", "other-tool"), ("status", "miss"), ("destination", "/elsewhere"),
                 ("platform", "macos"), ("arch", "arm64"),
                 ("extraction_scope", fetcher.TOOLCHAIN_SCOPE)]
        for field, value in cases:
            with self.subTest(field=field):
                self.make_cache()
                self.result[field] = value
                self.write(self.cache / "result.json", json.dumps(self.result))
                self.assertEqual(self.invoke()["status"], "miss")
                self.assertTrue(self.donor.exists())
                self.assertFalse((self.work / "src").exists())
        for field in ("sha256", "artifact_digest", "artifact_id", "head_sha", "run_id", "repository", "target"):
            with self.subTest(manifest=field):
                self.make_cache()
                self.result["manifest"][field] = "incorrect"
                self.write(self.cache / "result.json", json.dumps(self.result))
                self.assertEqual(self.invoke()["status"], "miss")
                self.assertTrue(self.donor.exists())

    def test_incomplete_source_invalid_args_and_broken_ninja_metadata_miss(self):
        for relative in restore.REQUIRED:
            with self.subTest(missing=relative):
                self.make_cache()
                (self.donor / relative).unlink()
                entry = self.invoke()
                self.assertEqual(entry["status"], "miss")
                self.assertEqual(self.donor.exists(), relative == "chrome/VERSION")
        for args in ('is_debug = false\n', 'target_cpu = "arm64"\n',
                     'target_cpu = getenv("ARCH")\n', 'target_cpu = "x64"\nv8_target_cpu = "arm64"\n'):
            with self.subTest(args=args):
                self.make_cache()
                self.write(self.donor / "out/Default/args.gn", args)
                self.assertEqual(self.invoke()["status"], "miss")
        for relative in (".ninja_log", ".ninja_deps"):
            self.make_cache()
            self.write(self.donor / "out/Default" / relative, "broken metadata")
            self.assertEqual(self.invoke()["status"], "miss")

    def test_ninja_state_diagnostics_survive_restore_and_owned_miss_cleanup(self):
        for header, expected in ((b"# ninja log v5\n", "hit"),
                                 (b"# ninja log v99\n", "miss"),
                                 (b"\xff\x00broken\n", "miss"),
                                 (b"x" * 4096, "miss")):
            with self.subTest(header=header[:32]):
                self.make_cache()
                self.work = Path(self.tmp.name) / f"ninja-diagnostic-{len(header)}"
                self.write(self.donor / "out/Default/.ninja_log", header)
                entry = self.invoke()
                self.assertEqual(entry["status"], expected, entry)
                state = entry["ninja_state"]
                self.assertEqual(state[".ninja_log"], {
                    "path": "out/Default/.ninja_log", "size_bytes": len(header),
                    "header_hex": header[:128].hex(), "header_truncated": len(header) > 128})
                self.assertEqual(state[".ninja_deps"]["header_hex"],
                                 b"# ninjadeps\n\x04\x00\x00\x00".hex())
                self.assertEqual(json.loads((self.work / restore.REPORT).read_text())["ninja_state"], state)
                if expected == "hit":
                    self.assertEqual(entry["receipt"]["ninja_state"], state)
                else:
                    self.assertEqual(entry["cleanup"]["status"], "removed")
                    self.assertFalse((self.cache / "tree").exists())
                    self.assertFalse((self.work / "src").exists())

    def test_ninja_state_diagnostics_record_linked_metadata_without_reading_it(self):
        path = self.donor / "out/Default/.ninja_log"
        path.unlink()
        path.symlink_to(self.donor / "chrome/source.cc")
        original_open = Path.open

        def checked_open(candidate, *args, **kwargs):
            if candidate == path or candidate == self.donor / "chrome/source.cc":
                raise AssertionError("unsafe read")
            return original_open(candidate, *args, **kwargs)

        with mock.patch.object(Path, "open", checked_open):
            state = restore.ninja_state_diagnostics(self.donor)
        self.assertIn("error", state[".ninja_log"])
        self.assertNotIn("header_hex", state[".ninja_log"])

    def test_ninja_evidence_survives_earlier_source_validation_failures(self):
        header = b"# ninja log v99\n"
        for failure in ("arch", "missing_deps", "linked_deps", "linked_log"):
            with self.subTest(failure=failure):
                self.make_cache()
                self.write(self.donor / "out/Default/.ninja_log", header)
                metadata = self.donor / "out/Default/.ninja_deps"
                if failure == "arch":
                    self.write(self.donor / "out/Default/args.gn", 'target_cpu="arm64"\n')
                elif failure == "linked_log":
                    metadata = self.donor / "out/Default/.ninja_log"
                    metadata.unlink()
                    metadata.symlink_to(self.donor / "chrome/source.cc")
                else:
                    metadata.unlink()
                    if failure == "linked_deps":
                        metadata.symlink_to(self.donor / "chrome/source.cc")
                entry = self.invoke()
                self.assertEqual(entry["status"], "miss", entry)
                self.assertEqual(entry["cleanup"]["status"], "removed", entry)
                self.assertFalse((self.cache / "tree").exists())
                state = entry["ninja_state"]
                if failure == "linked_log":
                    self.assertIn("error", state[".ninja_log"])
                    self.assertNotIn("header_hex", state[".ninja_log"])
                else:
                    self.assertEqual(state[".ninja_log"]["header_hex"], header.hex())
                if failure in ("missing_deps", "linked_deps"):
                    self.assertIn("error", state[".ninja_deps"])
                    self.assertNotIn("header_hex", state[".ninja_deps"])

    def test_known_host_links_are_recorded_without_recreating_them(self):
        paths = ["build/src/" + name for name in sorted(restore.HOST_LINKS)]
        self.result.update(skipped_external_symlinks=len(paths), external_symlink_paths=paths)
        self.write(self.cache / "result.json", json.dumps(self.result))
        entry = self.invoke()
        self.assertEqual(entry["status"], "hit", entry)
        self.assertEqual(entry["receipt"]["external_symlink_paths"], sorted(restore.HOST_LINKS))
        self.assertEqual(entry["receipt"]["archive_external_symlink_paths"], paths)
        for relative in restore.HOST_LINKS:
            self.assertFalse((self.work / "src" / relative).exists())
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            donor = cache / "tree/build/src"
            donor.mkdir(parents=True)
            # The legacy importer uses cache-relative names.
            importer_result = dict(self.result, external_symlink_paths=["tree/" + name for name in paths])
            restore.importer.preserve_external_tool_lookups(cache, donor, importer_result)
            created = {path.relative_to(donor).as_posix() for path in donor.rglob("*") if path.is_file()}
            self.assertEqual(created, restore.HOST_LINKS)

    def test_incomplete_duplicate_or_traversing_omission_lists_miss(self):
        name = "build/src/" + sorted(restore.HOST_LINKS)[0]
        for count, paths in ((1, []), (0, [name]), (2, [name, name]), (True, [name]),
                             (1, ["../escape"]), (1, ["/absolute"]), (1, [42])):
            with self.subTest(count=count, paths=paths):
                self.make_cache()
                self.result.update(skipped_external_symlinks=count, external_symlink_paths=paths)
                self.write(self.cache / "result.json", json.dumps(self.result))
                self.assertEqual(self.invoke()["status"], "miss")
                self.assertFalse(self.donor.exists())

    def test_locked_resume_and_unowned_caches_are_not_consumed_or_deleted(self):
        for kind in ("lock", "resume", "unexpected"):
            with self.subTest(kind=kind):
                path = {"lock": self.cache / ".lock", "resume": self.work / ".chromix-resumed",
                        "unexpected": self.cache / "keep"}[kind]
                self.write(path, "owned elsewhere")
                self.assertEqual(self.invoke()["status"], "miss")
                self.assertEqual(path.read_text(), "owned elsewhere")
                self.assertTrue(self.donor.exists())
                path.unlink()

    def test_local_overlaps_and_symlinks_are_errors(self):
        for cache in (self.work, self.work.parent, self.work / "src/cache", self.repo):
            with self.subTest(cache=cache), self.assertRaises(restore.LocalError):
                restore.restore(self.work, "linux", "x64", cache, self.repo)
        with self.assertRaises(restore.LocalError):
            restore.restore(self.work, "windows", "x86", self.cache, self.repo)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        self.work.mkdir(exist_ok=True)
        for name in ("src", restore.REPORT):
            path = self.work / name
            path.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(restore.LocalError):
                self.invoke()
            self.assertEqual(list(outside.iterdir()), [])
            path.unlink()
        alias = Path(self.tmp.name) / "alias"
        alias.symlink_to(self.cache, target_is_directory=True)
        with self.assertRaises(restore.LocalError):
            restore.restore(self.work, "linux", "x64", alias, self.repo)

    def test_source_escape_and_external_donor_symlinks_do_not_touch_outside(self):
        outside = Path(self.tmp.name) / "outside"
        self.write(outside / "keep", "outside")
        self.result["source"] = str(outside)
        self.write(self.cache / "result.json", json.dumps(self.result))
        self.assertEqual(self.invoke()["status"], "miss")
        self.make_cache()
        (self.donor / "external").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.invoke()["status"], "miss")
        self.assertEqual((outside / "keep").read_text(), "outside")
        self.assertFalse(self.donor.exists())

    def test_internal_links_modes_mtimes_logs_and_raw_args_are_preserved(self):
        alias = self.donor / "chrome/alias.cc"
        alias.symlink_to("source.cc")
        self.write(self.donor / "out/Default/args.gn", b'# raw args\r\ntarget_cpu = "x64"\r\n')
        self.write(self.donor / "tools/never-run", "raise RuntimeError('must not run')", 0o755)
        before = {}
        for path in self.donor.rglob("*"):
            info = path.lstat()
            before[path.relative_to(self.donor)] = (info.st_mtime_ns, stat.S_IMODE(info.st_mode),
                                                   path.read_bytes() if path.is_file() else None)
        with mock.patch("subprocess.run", side_effect=AssertionError("no donor execution")), \
                mock.patch.object(restore.shutil, "move", wraps=shutil.move) as move:
            entry = self.invoke()
        self.assertEqual(entry["status"], "hit", entry)
        move.assert_called_once()
        for relative, expected in before.items():
            path = self.work / "src" / relative
            info = path.lstat()
            self.assertEqual((info.st_mtime_ns, stat.S_IMODE(info.st_mode),
                              path.read_bytes() if path.is_file() else None), expected, relative)
        self.assertEqual(os.readlink(self.work / "src/chrome/alias.cc"), "source.cc")
        self.assertIn("\r\n", entry["receipt"]["original_args"]["text"])
        self.assertIs(entry["receipt"]["environment"]["cache_hit_proven"], False)
        self.assertEqual(json.loads((self.cache / "result.json").read_text())["status"], "consumed")
        self.assertEqual([p.name for p in (self.work / "src").glob(".chromix*")], [restore.MARKER])

    def test_cleanup_rechecks_receipt_and_top_level_entries(self):
        original = restore.source_args
        for change in ("owner", "digest", "extra", "tree_link"):
            with self.subTest(change=change):
                self.make_cache()
                outside = Path(self.tmp.name) / "outside-cleanup"
                self.write(outside / "keep", "unowned")

                def changed(src, identity):
                    if change in ("owner", "digest"):
                        changed_result = dict(self.result)
                        if change == "owner":
                            changed_result["owner"] = "another-tool"
                        else:
                            changed_result["manifest"] = dict(self.result["manifest"], sha256="changed")
                        self.write(self.cache / "result.json", json.dumps(changed_result))
                    elif change == "extra":
                        self.write(self.cache / "keep", "unowned")
                    else:
                        shutil.rmtree(self.cache / "tree")
                        (self.cache / "tree").symlink_to(outside, target_is_directory=True)
                    raise restore.Miss("invalid source")

                with mock.patch.object(restore, "source_args", side_effect=changed):
                    entry = self.invoke()
                self.assertEqual(entry["status"], "miss", entry)
                self.assertEqual(entry["cleanup"]["status"], "failed", entry)
                self.assertEqual((outside / "keep").read_text(), "unowned")
                self.assertTrue((self.cache / "tree").exists())
                if change == "tree_link":
                    (self.cache / "tree").unlink()
        self.assertIs(restore.source_args, original)

    def test_cleanup_delete_error_invalidates_hit_and_reports_remaining_tree(self):
        self.write(self.donor / "out/Default/args.gn", 'target_cpu="arm64"\n')
        with mock.patch.object(restore.shutil, "rmtree", side_effect=OSError("cannot remove tree")):
            entry = self.invoke()
        self.assertEqual(entry["status"], "miss")
        self.assertEqual(entry["cleanup"]["status"], "failed", entry)
        self.assertTrue(self.donor.exists())
        self.assertFalse((self.cache / ".lock").exists())
        result = json.loads((self.cache / "result.json").read_text())
        self.assertEqual(result["status"], "miss")
        self.assertIsNone(result["source"])

    def test_cleanup_receipt_write_error_keeps_all_files(self):
        self.write(self.donor / "out/Default/args.gn", 'target_cpu="arm64"\n')
        write = restore.importer.write_json

        def fail_receipt(path, value):
            if path == self.cache / "result.json":
                raise OSError("receipt not writable")
            write(path, value)

        with mock.patch.object(restore.importer, "write_json", side_effect=fail_receipt):
            entry = self.invoke()
        self.assertEqual(entry["cleanup"]["status"], "failed", entry)
        self.assertTrue(self.donor.exists())
        self.assertFalse((self.cache / ".lock").exists())
        self.assertEqual(json.loads((self.cache / "result.json").read_text())["status"], "hit")

    def test_cross_device_move_fallback_cannot_copy_or_remove_donor(self):
        with mock.patch.object(restore.os, "rename", side_effect=OSError(errno.EXDEV, "different devices")):
            entry = self.invoke()
        self.assertEqual(entry["status"], "miss", entry)
        self.assertIn("copy fallback is disabled", entry["reasons"][0])
        self.assertEqual(entry["cleanup"]["status"], "preserved")
        self.assertTrue(self.donor.exists())
        self.assertFalse((self.work / "src").exists())
        self.assertEqual(list(self.work.glob(".chromix-upstream-restore-*")), [])

    def test_marker_failure_rolls_back_and_preserves_donor(self):
        write_json = restore.importer.write_json

        def fail_marker(path, value):
            if path.name == restore.MARKER:
                raise OSError("marker write failed")
            write_json(path, value)

        before = (self.donor / "out/Default/.ninja_deps").read_bytes()
        with mock.patch.object(restore.importer, "write_json", side_effect=fail_marker):
            entry = self.invoke()
        self.assertEqual(entry["status"], "miss", entry)
        self.assertTrue(self.donor.exists())
        self.assertFalse((self.donor / restore.MARKER).exists())
        self.assertEqual((self.donor / "out/Default/.ninja_deps").read_bytes(), before)
        self.assertFalse((self.work / "src").exists())

    def test_install_exceptions_and_interruptions_restore_donor_bytes_mtimes_and_receipt(self):
        for failure_type in (RuntimeError, KeyboardInterrupt):
            for point in ("after_move", "partial_repair", "after_marker", "after_publish"):
                with self.subTest(failure=failure_type.__name__, point=point):
                    self.make_cache()
                    recorded, floor = self.timestamp_outputs()
                    before = self.donor_snapshot()
                    root_mtime = self.donor.stat().st_mtime_ns
                    result_path = self.cache / "result.json"
                    receipt = (result_path.read_bytes(), result_path.stat().st_mtime_ns)
                    failure = failure_type("fixture install failure")
                    move, utime = shutil.move, os.utime
                    write_json, rename = restore.importer.write_json, os.rename
                    injected = False

                    def moved(source, destination, **kwargs):
                        result = move(source, destination, **kwargs)
                        if point == "after_move":
                            raise failure
                        return result

                    def repaired(path, *args, **kwargs):
                        nonlocal injected
                        if point == "partial_repair" and Path(path).name == "second.o" and not injected:
                            injected = True
                            first = Path(path).with_name("output.o")
                            self.assertEqual(first.stat().st_mtime_ns, recorded)
                            raise failure
                        return utime(path, *args, **kwargs)

                    def marked(path, value):
                        write_json(path, value)
                        if point == "after_marker" and path.name == restore.MARKER:
                            raise failure

                    def published(source, destination):
                        rename(source, destination)
                        if point == "after_publish" and Path(destination) == self.work / "src":
                            raise failure

                    with mock.patch.object(restore.shutil, "move", side_effect=moved), \
                            mock.patch.object(restore.os, "utime", side_effect=repaired), \
                            mock.patch.object(restore.importer, "write_json", side_effect=marked), \
                            mock.patch.object(restore.os, "rename", side_effect=published):
                        if failure_type is KeyboardInterrupt:
                            with self.assertRaises(KeyboardInterrupt) as caught:
                                self.invoke()
                            self.assertIs(caught.exception, failure)
                        else:
                            self.assertEqual(self.invoke()["status"], "miss")
                    self.assertTrue(self.donor.is_dir())
                    self.assertEqual(self.donor_snapshot(), before)
                    self.assertEqual(self.donor.stat().st_mtime_ns, root_mtime)
                    self.assertEqual((result_path.read_bytes(), result_path.stat().st_mtime_ns), receipt)
                    self.assertFalse((self.work / "src").exists())
                    self.assertFalse((self.cache / ".lock").exists())
                    self.assertEqual(list(self.work.glob(".chromix-upstream-restore-*")), [])
                    report = json.loads((self.work / restore.REPORT).read_text())
                    self.assertEqual(report["failed_phase"], "install")
                    self.assertEqual(report["cleanup"]["status"], "preserved")
                    self.assertNotIn("preserved_source", report)

    def test_install_rollback_failure_preserves_staging_and_reports_its_location(self):
        cases = ((RuntimeError, OSError), (RuntimeError, RuntimeError),
                 (RuntimeError, KeyboardInterrupt), (KeyboardInterrupt, OSError))
        for failure_type, rollback_type in cases:
            with self.subTest(failure=failure_type.__name__, rollback=rollback_type.__name__):
                self.make_cache()
                self.timestamp_outputs()
                before = self.donor_snapshot()
                result_path = self.cache / "result.json"
                receipt = (result_path.read_bytes(), result_path.stat().st_mtime_ns)
                failure = failure_type("fixture installation interrupted")
                rollback_error = rollback_type("fixture rollback rename failed")
                write_json, rename = restore.importer.write_json, os.rename

                def marked(path, value):
                    write_json(path, value)
                    if path.name == restore.MARKER:
                        raise failure

                def fail_return(source, destination):
                    if Path(destination) == self.donor:
                        raise rollback_error
                    return rename(source, destination)

                expected = KeyboardInterrupt if KeyboardInterrupt in (failure_type, rollback_type) else restore.LocalError
                with mock.patch.object(restore.importer, "write_json", side_effect=marked), \
                        mock.patch.object(restore.os, "rename", side_effect=fail_return), \
                        mock.patch.object(restore.shutil, "rmtree", wraps=shutil.rmtree) as remove:
                    with self.assertRaises(expected) as caught:
                        self.invoke()
                remove.assert_not_called()
                if failure_type is KeyboardInterrupt:
                    self.assertIs(caught.exception, failure)
                elif rollback_type is KeyboardInterrupt:
                    self.assertIs(caught.exception, rollback_error)
                staged = Path(caught.exception.preserved_source)
                self.assertTrue(staged.is_dir())
                self.assertEqual(staged.name, "src")
                self.assertTrue(staged.parent.name.startswith(".chromix-upstream-restore-"))
                self.assertFalse(self.donor.exists())
                self.assertFalse((self.work / "src").exists())
                self.assertFalse((self.cache / ".lock").exists())
                self.assertEqual({path.relative_to(staged): (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in staged.rglob("*") if path.is_file()}, before)
                self.assertEqual((result_path.read_bytes(), result_path.stat().st_mtime_ns), receipt)
                report = json.loads((self.work / restore.REPORT).read_text())
                self.assertEqual(report["cleanup"]["status"], "preserved")
                self.assertEqual(report["cleanup"]["path"], str(staged))
                self.assertEqual(report["preserved_source"], str(staged))
                self.assertNotIn("donor retained", report["cleanup"]["reason"])
                shutil.rmtree(staged.parent)

    def test_linked_donor_cannot_hide_the_only_staged_source_copy(self):
        self.timestamp_outputs()
        before = self.donor_snapshot()
        receipt = (self.cache / "result.json").read_bytes()

        def occupied(staged, plan, journal):
            self.donor.symlink_to(staged, target_is_directory=True)
            raise OSError("fixture apply failure with linked donor")

        with mock.patch.object(restore, "apply_mtime_plan", side_effect=occupied), \
                mock.patch.object(restore.shutil, "rmtree", wraps=shutil.rmtree) as remove:
            with self.assertRaises(restore.LocalError) as caught:
                self.invoke()
        remove.assert_not_called()
        staged = Path(caught.exception.preserved_source)
        self.assertTrue(staged.is_dir())
        self.assertTrue(self.donor.is_symlink())
        self.assertEqual(self.donor.resolve(), staged)
        self.assertEqual({path.relative_to(staged): (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in staged.rglob("*") if path.is_file()}, before)
        self.assertEqual((self.cache / "result.json").read_bytes(), receipt)
        self.assertFalse((self.work / "src").exists())
        report = json.loads((self.work / restore.REPORT).read_text())
        self.assertEqual(report["cleanup"]["path"], str(staged))
        self.assertNotIn("donor retained", report["cleanup"]["reason"])
        self.assertFalse((self.cache / ".lock").exists())

    def test_interrupt_after_rollback_rename_reports_the_real_donor_path(self):
        self.timestamp_outputs()
        before = self.donor_snapshot()
        receipt = (self.cache / "result.json").read_bytes()
        rename = os.rename

        def returned(source, destination):
            rename(source, destination)
            if Path(destination) == self.donor:
                raise KeyboardInterrupt("fixture signal after rollback rename")

        with mock.patch.object(restore, "apply_mtime_plan", side_effect=RuntimeError("fixture failure")), \
                mock.patch.object(restore.os, "rename", side_effect=returned):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.invoke()
        self.assertEqual(caught.exception.preserved_source, str(self.donor))
        self.assertEqual(self.donor_snapshot(), before)
        self.assertEqual((self.cache / "result.json").read_bytes(), receipt)
        self.assertFalse((self.work / "src").exists())
        report = json.loads((self.work / restore.REPORT).read_text())
        self.assertEqual(report["cleanup"]["path"], str(self.donor))
        self.assertEqual(report["preserved_source"], str(self.donor))
        self.assertFalse((self.cache / ".lock").exists())

    def test_destination_appearing_during_restore_is_preserved(self):
        move = shutil.move

        def race(source, destination, **kwargs):
            result = move(source, destination, **kwargs)
            self.write(self.work / "src/keep", "another preparer")
            return result

        with mock.patch.object(restore.shutil, "move", side_effect=race):
            with self.assertRaises(restore.LocalError):
                self.invoke()
        self.assertEqual((self.work / "src/keep").read_text(), "another preparer")
        self.assertTrue(self.donor.exists())

    def test_verify_does_not_require_cache_or_canonical_markers(self):
        self.assertEqual(self.invoke()["status"], "hit")
        shutil.rmtree(self.cache)
        self.assertEqual(self.invoke("verify", cache=False)["status"], "verified")
        self.assertEqual(restore.is_restored(self.work, repo=self.repo)["arch"], "x64")
        self.write(self.work / "src/out/Default/args.gn", 'target_cpu = "x64"\nis_debug = true\n')
        self.write(self.work / "src/.chromix-source-ready", "prep owns this")
        receipt = restore.verify_restored(self.work, "linux", "x64", self.repo)
        self.assertIn("is_debug = false", receipt["original_args"]["text"])

    def test_verify_rejects_changed_receipt_version_arch_or_missing_build_state(self):
        self.assertEqual(self.invoke()["status"], "hit")
        marker = self.work / "src" / restore.MARKER
        original = marker.read_text()
        for field, value in (("owner", "other"), ("platform", "macos"), ("arch", "arm64"),
                             ("manifest", {}), ("identity", {}), ("original_args", {})):
            with self.subTest(field=field):
                receipt = json.loads(original)
                receipt[field] = value
                marker.write_text(json.dumps(receipt))
                with self.assertRaises(restore.Miss):
                    restore.verify_restored(self.work, "linux", "x64", self.repo)
        marker.write_text(original)
        self.write(self.work / "src/chrome/VERSION", "MAJOR=1\nMINOR=0\nBUILD=0\nPATCH=0\n")
        with self.assertRaises(restore.Miss):
            restore.verify_restored(self.work, "linux", "x64", self.repo)
        with self.assertRaises(restore.Miss):
            restore.verify_restored(self.work, "linux", "arm64", self.repo)

    def test_cli_verify_without_cache_and_unsafe_arguments_exit_nonzero(self):
        command = [sys.executable, str(restore.REPO / "tools/restore_upstream_cache.py"),
                   "--platform", "linux", "--arch", "x64", "--workdir", str(self.work)]
        for args in (["--phase", "verify"], ["--phase", "restore"],
                     ["--phase", "restore", "--cache-dir", str(self.work)]):
            result = subprocess.run(command + args, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, result.stdout)
        result = subprocess.run(command + ["--phase", "restore", "--cache-dir", str(self.cache)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hit", result.stdout)
        result = subprocess.run(command + ["--phase", "verify"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified", result.stdout)

    @unittest.skipUnless(shutil.which("ninja") and shutil.which("tar") and os.name == "posix",
                         "tiny Ninja/GNU tar fixture requires POSIX host tools")
    def test_gnu_tar_repair_gives_second_ninja_no_work_without_changing_inputs_or_logs(self):
        version = subprocess.run(["tar", "--version"], capture_output=True, text=True, check=True)
        if "GNU tar" not in version.stdout:
            self.skipTest("GNU tar required")
        source_ns = (time.time_ns() // 10**9 - 60) * 10**9 + 123_456_789
        source = self.donor / "chrome/source.cc"
        os.utime(source, ns=(source_ns, source_ns))
        script = ("from pathlib import Path\nimport sys\n"
                  "source, target = map(Path, sys.argv[1:])\n"
                  "target.parent.mkdir(parents=True, exist_ok=True)\n"
                  "target.write_bytes(source.read_bytes())\n"
                  "Path(str(target) + '.d').write_text(f'{target}: {source}\\n')\n")
        self.write(self.donor / "emit.py", script)
        self.write(self.donor / "out/Default/build.ninja",
                   "rule generate\n  command = " + shlex.quote(sys.executable) + " ../../emit.py $in $out\n"
                   "  depfile = $out.d\n  deps = gcc\n"
                   "build obj/output.o: generate ../../chrome/source.cc\ndefault obj/output.o\n")
        out = self.donor / "out/Default"
        for name in (".ninja_log", ".ninja_deps", "obj/output.o"):
            (out / name).unlink()
        built = subprocess.run(["ninja", "-C", str(out)], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        output_ns = (out / "obj/output.o").stat().st_mtime_ns
        if output_ns % 10**9 == 0:
            self.skipTest("filesystem does not provide fractional output timestamps")
        archive = Path(self.tmp.name) / "fixture.tar"
        subprocess.run(["tar", "--format=gnu", "-cf", str(archive), "-C", str(self.cache / "tree"),
                        self.donor.relative_to(self.cache / "tree").as_posix()], check=True)
        shutil.rmtree(self.cache / "tree")
        (self.cache / "tree").mkdir()
        with archive.open("rb") as stream, mock.patch.object(fetcher, "require_space"):
            fetcher.extract_tar(stream, self.cache / "tree", fetcher.SourceSelection(["build/src"]))
        self.assertEqual((out / "obj/output.o").stat().st_mtime_ns, output_ns // 10**9 * 10**9)
        before = {p.relative_to(self.donor): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.donor.rglob("*") if p.is_file()}
        subprocess.run(["ninja", "-C", str(out), "-n"], capture_output=True, text=True, check=True)
        entry = self.invoke()
        self.assertEqual(entry["status"], "hit", entry)
        self.assertEqual(entry["receipt"]["ninja_mtimes"]["outputs_restored"], 1, entry)
        src = self.work / "src"
        for relative, (data, mtime) in before.items():
            path = src / relative
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mtime_ns,
                             output_ns if relative.as_posix() == "out/Default/obj/output.o" else mtime)
        out = src / "out/Default"
        second = subprocess.run(["ninja", "-C", str(out)], capture_output=True, text=True, check=True)
        self.assertIn("no work to do", second.stdout)
        self.assertEqual(restore.restore_ninja_output_mtimes(src)["outputs_restored"], 0)
        graph = out / "build.ninja"
        graph.write_text(graph.read_text().replace("../../emit.py", "-B ../../emit.py"))
        dirty = subprocess.run(["ninja", "-C", str(out), "-n"], capture_output=True, text=True, check=True)
        self.assertNotIn("no work to do", dirty.stdout)
        os.utime(src / "chrome/source.cc", ns=(output_ns + 1, output_ns + 1))
        self.assertEqual(restore.restore_ninja_output_mtimes(src)["outputs_restored"], 0)


class NinjaTimestampTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name).resolve()
        self.out = self.src / "out/Default"
        (self.out / "obj").mkdir(parents=True)
        self.output = self.out / "obj/a.o"
        self.output.write_bytes(b"object")
        self.input = self.src / "a.cc"
        self.input.write_bytes(b"source")
        self.recorded = 1_700_000_000_123_456_789
        self.floor = self.recorded // 10**9 * 10**9
        os.utime(self.input, ns=(self.floor - 10**9, self.floor - 10**9))
        os.utime(self.output, ns=(self.floor, self.floor))
        self.metadata()

    def metadata(self, output="obj/a.o", dependency="../../a.cc", logged=None, version=5):
        write_ninja_metadata(self.out, [(output, self.recorded, logged or self.recorded, [dependency])],
                             version=version)

    def shared_metadata(self, dependencies=None, logged=None):
        dependencies = dependencies or ["../../a.cc"] * 8
        recorded = self.recorded
        logs = logged or [recorded] * 3
        records = []
        for index, timestamp in enumerate(logs):
            name = f"obj/shared-{index}.o"
            path = self.out / name
            path.write_bytes(b"object")
            os.utime(path, ns=(self.floor, self.floor))
            records.append((name, recorded, timestamp, dependencies))
        write_ninja_metadata(self.out, records, version=6)
        return [self.out / name for name, _, _, _ in records]

    def test_shared_inputs_are_validated_twice_per_original_name_not_per_edge(self):
        names = ["../../a.cc", "../.././a.cc"]
        self.shared_metadata(names * 8)
        with mock.patch.object(restore.objects, "relative_path", wraps=restore.objects.relative_path) as relative, \
                mock.patch.object(restore, "safe_path", wraps=restore.safe_path) as safe:
            plan = restore.ninja_mtime_plan(self.src)
        self.assertEqual(plan["outputs_restored"], 3)
        inputs = Counter(call.args[0] for call in relative.call_args_list if not call.kwargs.get("output"))
        self.assertEqual(inputs, {name: 2 for name in names})
        self.assertEqual(sum(call.args == (self.src, Path("a.cc")) for call in safe.call_args_list), 4)

    def test_shared_input_freshness_is_checked_for_each_output_and_each_call(self):
        for logs in ([self.recorded, self.recorded - 10**9, self.recorded],
                     [self.recorded - 10**9, self.recorded, self.recorded]):
            with self.subTest(logs=logs):
                self.shared_metadata(logged=logs)
                plan = restore.ninja_mtime_plan(self.src)
                self.assertEqual(plan["outputs_restored"], 2)
                self.assertEqual(plan["skipped"], {"recorded input is newer or same-second ambiguous": 1})
        os.utime(self.input, ns=(self.floor, self.floor))
        self.assertEqual(restore.ninja_mtime_plan(self.src)["outputs_restored"], 0)
        os.utime(self.input, ns=(self.floor - 2 * 10**9, self.floor - 2 * 10**9))
        self.assertEqual(restore.ninja_mtime_plan(self.src)["outputs_restored"], 3)

    def test_missing_input_diagnostics_are_bounded_and_do_not_change_repairs(self):
        records = []
        for index in range(40):
            name = f"obj/missing-{index}.o"
            path = self.out / name
            path.write_bytes(b"object")
            os.utime(path, ns=(self.floor, self.floor))
            records.append((name, self.recorded, self.recorded,
                            ["sdk/xcode_links/MacOSX26.0.sdk/stddef.h"]))
        write_ninja_metadata(self.out, records, version=7)
        plan = restore.ninja_mtime_plan(self.src)
        self.assertEqual(plan["outputs_restored"], 0)
        self.assertEqual(plan["skipped"], {"recorded input is missing": 40})
        self.assertEqual(len(plan["skipped_samples"]), 32)
        self.assertEqual(plan["skipped_samples"][0], {
            "output": "obj/missing-0.o", "reason": "recorded input is missing",
            "input": "sdk/xcode_links/MacOSX26.0.sdk/stddef.h"})
        self.assertTrue(all((self.out / name).stat().st_mtime_ns == self.floor
                            for name, *_ in records))

    def test_output_failure_diagnostics_do_not_reuse_previous_input(self):
        write_ninja_metadata(self.out, [
            ("obj/a.o", self.recorded, self.recorded, ["../../missing.cc"]),
            ("../outside.o", self.recorded, self.recorded, ["../../a.cc"])])
        plan = restore.ninja_mtime_plan(self.src)
        self.assertEqual(len(plan["skipped_samples"]), 2)
        self.assertIn("input", plan["skipped_samples"][0])
        self.assertNotIn("input", plan["skipped_samples"][1])

    def test_failed_input_checks_are_not_cached(self):
        self.shared_metadata(["../../a.cc"])
        relative = restore.objects.relative_path
        attempts = 0

        def transient(value, *args, **kwargs):
            nonlocal attempts
            if value == "../../a.cc":
                attempts += 1
                if attempts == 1:
                    raise OSError("transient input failure")
            return relative(value, *args, **kwargs)

        with mock.patch.object(restore.objects, "relative_path", side_effect=transient):
            plan = restore.ninja_mtime_plan(self.src)
        self.assertEqual(attempts, 3)
        self.assertEqual(plan["outputs_restored"], 2)
        self.assertEqual(plan["skipped"], {"transient input failure": 1})

    def test_source_modified_during_planning_aborts_before_any_repair(self):
        outputs = self.shared_metadata()
        fresh = restore.objects.input_is_fresh
        changed = False

        def mutate(mtime, record):
            nonlocal changed
            if not changed:
                changed = True
                self.input.write_bytes(b"changed source")
                os.utime(self.input, ns=(mtime, mtime))
            return fresh(mtime, record)

        with mock.patch.object(restore.objects, "input_is_fresh", side_effect=mutate), \
                mock.patch.object(restore, "apply_mtime_plan") as apply:
            with self.assertRaisesRegex(restore.Miss, "input changed during timestamp planning"):
                restore.restore_ninja_output_mtimes(self.src)
        apply.assert_not_called()
        self.assertTrue(all(path.stat().st_mtime_ns == self.floor for path in outputs))

    def test_cached_input_disappearance_or_io_error_rejects_the_whole_plan(self):
        self.shared_metadata()
        relative = restore.objects.relative_path
        for error in (FileNotFoundError("removed input"), OSError("unreadable input"),
                      RuntimeError("cyclic input")):
            with self.subTest(error=error):
                attempts = 0

                def fail_revalidation(value, *args, **kwargs):
                    nonlocal attempts
                    if value == "../../a.cc":
                        attempts += 1
                        if attempts == 2:
                            raise error
                    return relative(value, *args, **kwargs)

                expected = type(error) if isinstance(error, OSError) else restore.Miss
                with mock.patch.object(restore.objects, "relative_path", side_effect=fail_revalidation), \
                        mock.patch.object(restore, "apply_mtime_plan") as apply:
                    with self.assertRaises(expected) as caught:
                        restore.restore_ninja_output_mtimes(self.src)
                if isinstance(error, OSError):
                    self.assertIs(caught.exception, error)
                else:
                    self.assertIn("input changed during timestamp planning", str(caught.exception))
                apply.assert_not_called()
                self.assertEqual(attempts, 2)

    def test_revalidation_compares_every_stat_identity_field(self):
        self.shared_metadata()
        fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
        original_stat = Path.stat
        fresh = restore.objects.input_is_fresh
        for field in fields:
            with self.subTest(field=field):
                revalidating = False
                changed_calls = 0

                def mark_cached(mtime, record):
                    nonlocal revalidating
                    revalidating = True
                    return fresh(mtime, record)

                def changed_stat(path, *args, **kwargs):
                    nonlocal changed_calls
                    info = original_stat(path, *args, **kwargs)
                    if revalidating and path == self.input and kwargs.get("follow_symlinks", True):
                        changed_calls += 1
                        values = {name: getattr(info, name) for name in fields}
                        values[field] += 1
                        return SimpleNamespace(**values)
                    return info

                with mock.patch.object(Path, "stat", changed_stat), \
                        mock.patch.object(restore.objects, "input_is_fresh", side_effect=mark_cached):
                    with self.assertRaisesRegex(restore.Miss, "input changed during timestamp planning"):
                        restore.ninja_mtime_plan(self.src)
                self.assertGreater(changed_calls, 0)

    def test_input_links_are_rejected_before_caching_and_on_revalidation(self):
        alias = self.src / "alias"
        for target, directory in ((self.input, False), (self.src, True),
                                  (self.src.parent, True), (self.src / "absent", False)):
            with self.subTest(target=target):
                alias.symlink_to(target, target_is_directory=directory)
                dependency = "../../alias/a.cc" if directory else "../../alias"
                self.shared_metadata([dependency])
                with mock.patch.object(restore.objects, "relative_path",
                                       wraps=restore.objects.relative_path) as relative:
                    plan = restore.ninja_mtime_plan(self.src)
                self.assertEqual(plan["outputs_restored"], 0)
                self.assertEqual(sum(call.args[0] == dependency for call in relative.call_args_list), 3)
                alias.unlink()
        saved = self.src / "saved.cc"
        self.shared_metadata()
        fresh = restore.objects.input_is_fresh
        for target in (saved, self.src.parent / "outside.cc"):
            with self.subTest(replacement=target):
                changed = False

                def replace_input(mtime, record):
                    nonlocal changed
                    if not changed:
                        changed = True
                        self.input.rename(saved)
                        self.input.symlink_to(target)
                    return fresh(mtime, record)

                with mock.patch.object(restore.objects, "input_is_fresh", side_effect=replace_input):
                    with self.assertRaisesRegex(restore.Miss, "input changed during timestamp planning"):
                        restore.ninja_mtime_plan(self.src)
                self.input.unlink()
                saved.rename(self.input)

    def test_input_junction_checks_apply_initially_and_during_revalidation(self):
        self.shared_metadata()
        for delayed in (False, True):
            with self.subTest(delayed=delayed):
                became_linked = not delayed
                fresh = restore.objects.input_is_fresh

                def junction(path):
                    return path == self.input and became_linked

                def mutate(mtime, record):
                    nonlocal became_linked
                    became_linked = True
                    return fresh(mtime, record)

                with mock.patch.object(Path, "is_junction", junction, create=True), \
                        mock.patch.object(restore.objects, "input_is_fresh", side_effect=mutate):
                    if delayed:
                        with self.assertRaisesRegex(restore.Miss, "input changed during timestamp planning"):
                            restore.ninja_mtime_plan(self.src)
                    else:
                        plan = restore.ninja_mtime_plan(self.src)
                        self.assertEqual(plan["outputs_restored"], 0)
                        self.assertEqual(sum(plan["skipped"].values()), 3)

    @unittest.skipUnless(os.name == "nt" and hasattr(Path, "is_junction"), "native Windows junction required")
    def test_native_input_directory_junction_is_rejected(self):
        target = self.src / "headers"
        target.mkdir()
        (target / "a.cc").write_bytes(b"source")
        alias = self.src / "junction"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)],
                       check=True, capture_output=True)
        try:
            self.shared_metadata(["../../junction/a.cc"])
            self.assertEqual(restore.ninja_mtime_plan(self.src)["outputs_restored"], 0)
        finally:
            alias.rmdir()

    def test_generated_input_also_output_is_revalidated_before_repairs(self):
        generated = self.out / "gen/shared.h"
        generated.parent.mkdir()
        generated.write_bytes(b"generated")
        os.utime(generated, ns=(self.floor - 10**9, self.floor - 10**9))
        os.utime(self.input, ns=(self.floor - 2 * 10**9, self.floor - 2 * 10**9))
        records = [("gen/shared.h", self.recorded - 10**9, self.recorded - 10**9, ["../../a.cc"]),
                   ("obj/a.o", self.recorded, self.recorded, ["gen/shared.h", "../../a.cc"])]
        write_ninja_metadata(self.out, records)
        before = [(path, path.read_bytes(), path.stat().st_mtime_ns)
                  for path in (self.input, self.out / ".ninja_log", self.out / ".ninja_deps")]
        plan = restore.ninja_mtime_plan(self.src)
        self.assertEqual(plan["outputs_restored"], 2)
        self.assertEqual(generated.stat().st_mtime_ns, self.floor - 10**9)
        self.assertEqual(restore.restore_ninja_output_mtimes(self.src), plan)
        self.assertEqual(generated.stat().st_mtime_ns, self.recorded - 10**9)
        self.assertEqual(self.output.stat().st_mtime_ns, self.recorded)
        for path, content, mtime in before:
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (content, mtime))

    def test_apply_failure_rolls_back_already_repaired_outputs(self):
        original_utime = os.utime
        for failure_type in (OSError, RuntimeError, KeyboardInterrupt):
            with self.subTest(failure=failure_type.__name__):
                outputs = self.shared_metadata()
                marker = self.src / restore.MARKER
                marker.write_bytes(b"existing receipt")
                paths = [*outputs, self.input, self.out / ".ninja_log", self.out / ".ninja_deps", marker]
                before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
                failure = failure_type("fixture timestamp failure")
                failed = False

                def fail_second(path, *args, **kwargs):
                    nonlocal failed
                    if Path(path) == outputs[1] and not failed:
                        failed = True
                        self.assertEqual(outputs[0].stat().st_mtime_ns, self.recorded)
                        raise failure
                    return original_utime(path, *args, **kwargs)

                with mock.patch.object(restore.os, "utime", side_effect=fail_second):
                    with self.assertRaises(failure_type) as caught:
                        restore.restore_ninja_output_mtimes(self.src)
                self.assertIs(caught.exception, failure)
                self.assertTrue(failed)
                self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}, before)

    def test_standalone_rollback_failure_preserves_source_and_interrupt(self):
        original_utime = os.utime
        for failure_type in (RuntimeError, KeyboardInterrupt):
            with self.subTest(failure=failure_type.__name__):
                outputs = self.shared_metadata()
                marker = self.src / restore.MARKER
                marker.write_bytes(b"existing receipt")
                paths = [*outputs, self.input, self.out / ".ninja_log", self.out / ".ninja_deps", marker]
                before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
                failure = failure_type("fixture timestamp interruption")
                failed = False

                def fail_apply_and_rollback(path, *args, **kwargs):
                    nonlocal failed
                    if Path(path) == outputs[1]:
                        if not failed:
                            failed = True
                            raise failure
                        raise OSError("fixture rollback utime failed")
                    return original_utime(path, *args, **kwargs)

                expected = KeyboardInterrupt if failure_type is KeyboardInterrupt else restore.LocalError
                with mock.patch.object(restore.os, "utime", side_effect=fail_apply_and_rollback):
                    with self.assertRaises(expected) as caught:
                        restore.restore_ninja_output_mtimes(self.src)
                if failure_type is KeyboardInterrupt:
                    self.assertIs(caught.exception, failure)
                self.assertEqual(caught.exception.preserved_source, str(self.src))
                self.assertEqual(outputs[0].stat().st_mtime_ns, self.recorded)
                for path in paths:
                    self.assertEqual(path.read_bytes(), before[path][0])
                    if path != outputs[0]:
                        self.assertEqual(path.stat().st_mtime_ns, before[path][1])

    def test_exact_floor_only_and_hash_deps_input_unchanged(self):
        before = [(p, p.read_bytes(), p.stat().st_mtime_ns)
                  for p in (self.input, self.out / ".ninja_log", self.out / ".ninja_deps")]
        result = restore.restore_ninja_output_mtimes(self.src)
        self.assertEqual(result["outputs_restored"], 1, result)
        self.assertEqual(self.output.stat().st_mtime_ns, self.recorded)
        for path, content, mtime in before:
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, mtime)
        for mtime in (self.floor - 10**9, self.floor + 1, self.recorded + 1, self.recorded):
            os.utime(self.output, ns=(mtime, mtime))
            self.assertEqual(restore.restore_ninja_output_mtimes(self.src)["outputs_restored"], 0)
            self.assertEqual(self.output.stat().st_mtime_ns, mtime)

    def test_v6_v7_command_start_timestamps_keep_logs_and_only_repair_proven_outputs(self):
        for version in (6, 7):
            logged = self.recorded - 2 * 10**9
            cutoff = logged // 10**9 * 10**9
            for input_mtime, repaired in ((cutoff - 1, 1), (cutoff, 0), (self.floor - 1, 0)):
                with self.subTest(version=version, input_mtime=input_mtime):
                    self.metadata(logged=logged, version=version)
                    os.utime(self.input, ns=(input_mtime, input_mtime))
                    os.utime(self.output, ns=(self.floor, self.floor))
                    before = [(p, p.read_bytes(), p.stat().st_mtime_ns)
                              for p in (self.input, self.out / ".ninja_log", self.out / ".ninja_deps")]
                    result = restore.restore_ninja_output_mtimes(self.src)
                    self.assertEqual(result["outputs_restored"], repaired, result)
                    self.assertEqual(self.output.stat().st_mtime_ns,
                                     self.recorded if repaired else self.floor)
                    for path, content, mtime in before:
                        self.assertEqual(path.read_bytes(), content)
                        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_newer_or_same_second_inputs_cannot_be_hidden(self):
        for mtime in (self.floor, self.floor + 1, self.recorded + 1):
            with self.subTest(mtime=mtime):
                os.utime(self.input, ns=(mtime, mtime))
                self.assertEqual(restore.restore_ninja_output_mtimes(self.src)["outputs_restored"], 0)
                self.assertEqual(self.output.stat().st_mtime_ns, self.floor)
                self.assertEqual(self.input.stat().st_mtime_ns, mtime)

    def test_windows_dependency_paths_remain_unchanged_and_are_reported_as_skipped(self):
        self.metadata(output=r"obj\a.obj", dependency=r"..\..\a.cc")
        before = (self.out / ".ninja_deps").read_bytes()
        result = restore.restore_ninja_output_mtimes(self.src)
        self.assertEqual(result["outputs_restored"], 0)
        self.assertEqual(result["skipped"], {"absolute or unsupported path": 1})
        self.assertEqual((self.out / ".ninja_deps").read_bytes(), before)
        self.assertEqual(self.output.stat().st_mtime_ns, self.floor)

    def test_traversal_external_inputs_log_mismatch_and_hardlinks_are_skipped(self):
        for output, dependency in (("../../a.cc", "../../a.cc"), ("/tmp/escape", "../../a.cc"),
                                   ("obj/a.o", "/tmp/external"), ("obj/a.o", "../../absent")):
            with self.subTest(output=output, dependency=dependency):
                self.metadata(output, dependency)
                self.assertEqual(restore.restore_ninja_output_mtimes(self.src)["outputs_restored"], 0)
        self.metadata(logged=self.recorded - 1)
        self.assertEqual(restore.restore_ninja_output_mtimes(self.src)["outputs_restored"], 0)
        self.metadata()
        os.link(self.output, self.src / "source-hardlink")
        self.assertEqual(restore.restore_ninja_output_mtimes(self.src)["outputs_restored"], 0)
        self.assertEqual(self.output.stat().st_mtime_ns, self.floor)


if __name__ == "__main__":
    unittest.main()
