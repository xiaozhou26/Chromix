import copy
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import import_upstream_cache as importer
from tools.tests.test_restore_upstream_cache import windows_source
from tools.tests.test_fetch_upstream_cache import unavailable_source, windows153_source


class ImportUpstreamCacheTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.work = self.root / "work"
        self.src = self.work / "src"
        self.cache = self.work / "upstream-cache"
        self.donor = self.cache / "tree/build/src"
        self.platform = "linux"
        self.arch = "x64"
        for relative in ("CHROMIUM_VERSION", "CHROMIUM_LINUX_VERSION", "CHROMIUM_MACOS_VERSION", "CHROMIUM_WINDOWS_VERSION",
                         "build/ungoogled-revisions.psd1", "build/upstream-cache.json"):
            destination = self.repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if (importer.REPO / relative).is_file():
                shutil.copyfile(importer.REPO / relative, destination)
        path = self.repo / "build/upstream-cache.json"
        manifest = importer.read_json(path)
        manifest["sources"]["windows"] = windows_source(self.repo)
        self.write(path, json.dumps(manifest))
        self.prepare()

    def write(self, path, data, mode=0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        path.chmod(mode)

    def binary(self, path, platform=None, arch=None):
        platform, arch = platform or self.platform, arch or self.arch
        data = bytearray(128)
        if platform == "linux":
            data[:6] = b"\x7fELF\x02\x01"
            struct.pack_into("<H", data, 18, {"x64": 62, "arm64": 183}[arch])
        elif platform == "macos":
            data[:4] = b"\xcf\xfa\xed\xfe"
            struct.pack_into("<I", data, 4, {"x64": 0x1000007, "arm64": 0x100000C}[arch])
        else:
            data[:2] = b"MZ"
            struct.pack_into("<I", data, 60, 64)
            data[64:68] = b"PE\0\0"
            struct.pack_into("<H", data, 68, {"x64": 0x8664, "arm64": 0xAA64}[arch])
        self.write(path, data, 0o755)

    def prepare(self, platform="linux", arch="x64"):
        self.platform, self.arch = platform, arch
        for path in (self.src, self.cache):
            if path.exists():
                shutil.rmtree(path)
        self.identity, _ = importer.repository_identity(self.repo, platform, arch)
        version = self.identity["chromium_version"]
        version_text = "\n".join(f"{key}={value}" for key, value in zip(
            ("MAJOR", "MINOR", "BUILD", "PATCH"), version.split("."))) + "\n"
        for root in (self.src, self.donor):
            self.write(root / "chrome/VERSION", version_text)
            self.write(root / "tools/clang/scripts/update.py",
                       "CLANG_REVISION = 'llvmorg-23-init-19482-g53d18800'\n"
                       "CLANG_SUB_REVISION = 1\nRELEASE_VERSION = '23'\n"
                       "PACKAGE_VERSION = '%s-%s' % (CLANG_REVISION, CLANG_SUB_REVISION)\n")
            self.write(root / "tools/rust/update_rust.py",
                       "RUST_REVISION = 'b998449636a48e2c4a362809085b600a0174e1f2'\nRUST_SUB_REVISION = 5\n")
            for relative in ("tools/clang/scripts/build.py", "tools/rust/build_rust.py", "tools/rust/build_bindgen.py"):
                self.write(root / relative, "raise RuntimeError('must never execute cached scripts')\n")
        ready = [self.identity[key] for key in ("chromium_version", "ungoogled_commit", "head_sha")]
        if platform != "windows":
            ready = [platform, arch] + ready
        self.write(self.src / ".chromix-source-ready", "|".join(ready + ["a" * 64]))
        self.write(self.src / "chrome/canonical.cc", "canonical source only\n")
        self.write(self.donor / "chrome/canonical.cc", "different upstream source\n")
        self.write(self.donor / ".chromix-toolchain-ready", "donor marker must not be copied")
        self.versions = importer.expected_versions(self.src)
        self.toolchains(self.donor)
        if platform != "linux":
            for relative in (importer.CLANG, importer.RUST):
                (self.src / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(self.donor / relative, self.src / relative)
            suffix = ".exe" if platform == "windows" else ""
            (self.src / importer.RUST / "bin" / ("bindgen" + suffix)).unlink()
            (self.src / importer.RUST / {"macos": "lib/libclang.dylib", "windows": "bin/libclang.dll"}[platform]).unlink()
        self.receipt()

    def toolchains(self, root):
        clang, rust = root / importer.CLANG, root / importer.RUST
        suffix = ".exe" if self.platform == "windows" else ""
        binaries = {
            "linux": ["clang", "clang++", "llvm-ar", "llvm-nm", "llvm-readobj", "llvm-objcopy", "ld.lld"],
            "macos": ["clang", "clang++", "llvm-ar", "llvm-readobj", "llvm-objcopy", "ld64.lld"],
            "windows": ["clang-cl", "lld-link", "llvm-ml"],
        }[self.platform]
        host_arch = "x64" if self.platform == "windows" else self.arch
        for name in binaries:
            self.binary(clang / "bin" / (name + suffix), arch=host_arch)
        self.write(clang / "cr_build_revision", self.versions["clang"])
        resource = clang / "lib/clang" / self.versions["clang_release"]
        for name in ("stddef.h", "stdarg.h"):
            self.write(resource / "include" / name, "/* complete fixture header */\n")
        runtime_arch = {"x64": "x86_64", "arm64": "aarch64"}[self.arch]
        self.write(resource / {"linux": f"lib/linux/libclang_rt.builtins-{runtime_arch}.a",
                               "macos": "lib/darwin/libclang_rt.osx.a",
                               "windows": f"lib/windows/clang_rt.builtins-{runtime_arch}.lib"}[self.platform], b"!<arch>\nfixture")
        self.write(rust / "VERSION", f"rustc 1.96.0 deadbeef ({self.versions['rust']} chromium)\n")
        for name in ("rustc", "cargo", "rustfmt", "bindgen"):
            self.binary(rust / "bin" / (name + suffix), arch=host_arch)
        for cpu in {self.arch, host_arch}:
            rustlib = rust / "lib/rustlib" / importer.TRIPLES[self.platform, cpu] / "lib"
            for name in ("std", "core", "alloc", "compiler_builtins"):
                self.write(rustlib / f"lib{name}-abc123.rlib", b"!<arch>\nfixture")
        self.write(rust / ("bin/rustc_driver-abc.dll" if self.platform == "windows" else "lib/librustc_driver-abc.so"), b"shared runtime")
        self.write(rust / {"linux": "lib/libclang.so.23", "macos": "lib/libclang.dylib",
                           "windows": "bin/libclang.dll"}[self.platform], b"bindgen runtime")

    def receipt(self):
        manifest = {key: self.identity[key] for key in (
            "chromium_version", "repository", "head_sha", "run_id", "artifact_id", "artifact_digest")}
        manifest.update(schema_version=1, target=f"{self.platform}-{self.arch}",
                        path=str(self.repo / "build/upstream-cache.json"),
                        sha256=importer.digest_file(self.repo / "build/upstream-cache.json"))
        self.result = {"owner": "chromix-upstream-cache-v1", "status": "hit", "source": str(self.donor),
                       "destination": str(self.cache), "platform": self.platform, "arch": self.arch,
                       "manifest": manifest, "skipped_external_symlinks": 0}
        self.save_receipt()

    def save_receipt(self):
        self.write(self.cache / "result.json", json.dumps(self.result))

    def run_phase(self, phase="toolchain"):
        return importer.run_import(phase, self.platform, self.arch, self.work, self.cache, repo=self.repo)

    def assert_miss(self, entry, reason):
        self.assertEqual(entry["status"], "miss", entry)
        self.assertIn(reason, " ".join(entry["reasons"]))
        self.assertEqual(entry["counts"]["objects_copied"], 0)

    def object_args(self, canonical='target_cpu = "x64"\nis_debug = false\n', donor=None):
        self.write(self.src / ".chromix-domain-substituted", "canonical")
        self.write(self.src / "out/Chromix/args.gn", canonical)
        self.write(self.donor / "out/Default/args.gn", canonical if donor is None else donor)
        self.write(self.src / "out/Chromix/gn", b"canonical GN", 0o755)
        self.write(self.donor / "out/Default/.ninja_log", "# ninja log v5\n")
        self.write(self.donor / "out/Default/.ninja_deps", b"donor dependency database")
        self.write(self.donor / "out/Default/obj/object.o", b"upstream object")

    def linux_overrides(self):
        pins = importer.load_shared_pins(self.repo)
        pins.update(LinuxChromiumVersion="153.0.8010.36",
                    LinuxUngoogledVersion="153.0.8010.36-1", LinuxUngoogledCommit="e" * 40,
                    UngoogledLinuxVersion="153.0.8010.36-1", UngoogledLinuxCommit="f" * 40)
        self.write(self.repo / "build/ungoogled-revisions.psd1",
                   "@{\n" + "".join(f'  {key} = "{value}"\n' for key, value in pins.items()) + "}\n")
        self.write(self.repo / "CHROMIUM_LINUX_VERSION", "153.0.8010.36\n")
        path = self.repo / "build/upstream-cache.json"
        manifest = importer.read_json(path)
        manifest["sources"]["linux"].update(
            chromium_version="153.0.8010.36", ungoogled_commit="e" * 40,
            head_sha="f" * 40, head_branch="153.0.8010.36-1")
        self.write(path, json.dumps(manifest))

    def test_linux_override_identity_and_nonlinux_identity_are_independent(self):
        before = {platform: importer.repository_identity(self.repo, platform, "x64")
                  for platform in ("macos", "windows")}
        self.linux_overrides()
        identity, _ = importer.repository_identity(self.repo, "linux", "x64")
        self.assertEqual(identity["chromium_version"], "153.0.8010.36")
        self.assertEqual(identity["ungoogled_commit"], "e" * 40)
        self.assertEqual(identity["head_sha"], "f" * 40)
        for platform, identity in before.items():
            self.assertEqual(importer.repository_identity(self.repo, platform, "x64"), identity)
        self.write(self.repo / "CHROMIUM_LINUX_VERSION", "unrelated invalid Linux pin\n")
        for platform in ("linux", "macos", "windows"):
            with self.subTest(platform=platform), self.assertRaises(importer.Miss):
                importer.repository_identity(self.repo, platform, "x64")

    def test_disabled_windows_fixture_refuses_import_before_donor_validation(self):
        path = self.repo / "build/upstream-cache.json"
        original = importer.read_json(path)
        for arch in ("x64", "arm64"):
            for phase in ("toolchain", "objects"):
                with self.subTest(arch=arch, phase=phase):
                    self.write(path, json.dumps(original))
                    self.prepare("windows")
                    disabled = copy.deepcopy(original)
                    disabled["sources"]["windows"] = unavailable_source(
                        self.repo, "windows", disabled["sources"]["windows"])
                    self.write(path, json.dumps(disabled))
                    self.write(self.src / importer.MARKER, "stale authorization")
                    with mock.patch.object(importer, "validate_source") as canonical, \
                            mock.patch.object(importer, "donor_source") as donor, \
                            mock.patch.object(importer, "install_transaction") as install:
                        entry = importer.run_import(phase, "windows", arch, self.work, self.cache, repo=self.repo)
                    self.assert_miss(entry, "source_unavailable")
                    for key in ("files_copied", "bytes_copied", "directories_reused", "objects_copied"):
                        self.assertEqual(entry["counts"][key], 0)
                    canonical.assert_not_called()
                    donor.assert_not_called()
                    install.assert_not_called()
                    if phase == "toolchain":
                        self.assertFalse((self.src / importer.MARKER).exists())
                    self.assertFalse((self.src / importer.RUST / "bin/bindgen.exe").exists())

    def test_disabled_sources_allow_other_platform_imports(self):
        path = self.repo / "build/upstream-cache.json"
        original = importer.read_json(path)
        for disabled in ("windows", "linux", "macos"):
            manifest = copy.deepcopy(original)
            manifest["sources"][disabled] = unavailable_source(
                self.repo, disabled, manifest["sources"][disabled])
            self.write(path, json.dumps(manifest))
            for platform in ("linux", "macos", "windows"):
                with self.subTest(disabled=disabled, platform=platform):
                    if platform == disabled:
                        for arch in ("x64", "arm64"):
                            with self.assertRaisesRegex(importer.Miss, "source_unavailable"):
                                importer.repository_identity(self.repo, platform, arch)
                    else:
                        self.prepare(platform)
                        self.assertEqual(self.run_phase()["status"], "hit")

    def test_disabled_manifest_missing_extra_or_cross_platform_fields_fail_closed(self):
        path = self.repo / "build/upstream-cache.json"
        original = importer.read_json(path)
        for disabled in ("windows", "linux", "macos"):
            source = unavailable_source(self.repo, disabled, original["sources"][disabled])
            variants = [{key: value for key, value in source.items() if key != missing}
                        for missing in source]
            variants += [dict(source, **{key: value}) for key, value in (
                ("run_id", 101), ("run_id", None), ("artifacts", {}), ("artifacts", None),
                ("available", None), ("available", "false"), ("available", 0), ("available", 1),
                ("chromium_version", "151.0.0.0"), ("ungoogled_commit", "0" * 40),
                ("head_sha", "0" * 40), ("head_branch", "151.0.0.0-1"),
                ("event", "pull_request"), ("workflow_path", "wrong.yml"))]
            variants += [unavailable_source(self.repo, other, original["sources"][other])
                         for other in ("linux", "macos", "windows") if other != disabled]
            for index, changed in enumerate(variants):
                manifest = copy.deepcopy(original)
                manifest["sources"][disabled] = changed
                self.write(path, json.dumps(manifest))
                for platform in ("linux", "macos", "windows"):
                    with self.subTest(disabled=disabled, variant=index, platform=platform):
                        with self.assertRaises(importer.Miss) as caught:
                            importer.repository_identity(self.repo, platform, "x64")
                        self.assertNotIn("source_unavailable", str(caught.exception))

    def test_windows_153_import_rejects_old_152_donor_and_receipt(self):
        path = self.repo / "build/upstream-cache.json"
        manifest = importer.read_json(path)
        manifest["sources"]["windows"] = windows153_source(self.repo)
        self.write(path, json.dumps(manifest))
        self.prepare("windows")
        self.result["manifest"]["chromium_version"] = "152.0.7977.82"
        self.save_receipt()
        self.assert_miss(self.run_phase(), "pinned identity mismatch: chromium_version")
        self.receipt()
        self.write(self.donor / "chrome/VERSION", "MAJOR=152\nMINOR=0\nBUILD=7977\nPATCH=82\n")
        self.assert_miss(self.run_phase(), "donor chrome/VERSION")
        self.assertFalse((self.src / importer.RUST / "bin/bindgen.exe").exists())

    def test_legacy_available_flag_absent_matches_explicit_true(self):
        path = self.repo / "build/upstream-cache.json"
        manifest = importer.read_json(path)
        for platform in ("linux", "macos", "windows"):
            identity, _ = importer.repository_identity(self.repo, platform, "x64")
            self.assertNotIn("available", manifest["sources"][platform])
            manifest["sources"][platform]["available"] = True
            self.write(path, json.dumps(manifest))
            self.assertEqual(importer.repository_identity(self.repo, platform, "x64")[0], identity)

    def test_linux_153_import_rejects_old_152_donor_and_receipt(self):
        self.linux_overrides()
        self.prepare()
        self.result["manifest"]["chromium_version"] = "152.0.7977.82"
        self.save_receipt()
        self.assert_miss(self.run_phase(), "pinned identity mismatch: chromium_version")
        self.receipt()
        self.write(self.donor / "chrome/VERSION", "MAJOR=152\nMINOR=0\nBUILD=7977\nPATCH=82\n")
        self.assert_miss(self.run_phase(), "donor chrome/VERSION")
        self.assertFalse((self.src / importer.MARKER).exists())
        self.assertFalse((self.src / importer.CLANG).exists())

    def test_linux_override_matches_fetcher_and_imports(self):
        from tools.fetch_upstream_cache import load_manifest
        self.linux_overrides()
        self.prepare()
        _, self.result["manifest"] = load_manifest("linux", "x64", root=self.repo)
        self.save_receipt()
        self.assertEqual(self.run_phase()["status"], "hit")

    def test_manifest_fallback_or_global_override_requires_matching_identity(self):
        self.linux_overrides()
        path = self.repo / "build/upstream-cache.json"
        original = path.read_text()
        for field in ("chromium_version", "ungoogled_commit"):
            for target in ("global", "linux"):
                with self.subTest(field=field, target=target):
                    manifest = json.loads(original)
                    if target == "linux":
                        del manifest["sources"]["linux"][field]
                    else:
                        manifest[field] = manifest["sources"]["linux"][field]
                    self.write(path, json.dumps(manifest))
                    baseline = json.loads(original)
                    if baseline["sources"]["linux"][field] == baseline[field]:
                        importer.repository_identity(self.repo, "linux", "x64")
                    else:
                        with self.assertRaisesRegex(importer.Miss, "does not match repository pins"):
                            importer.repository_identity(self.repo, "linux", "x64")
        self.write(path, original)

    def test_linux_hit_copies_only_complete_allowlist_and_preserves_source(self):
        original = importer.inventory(self.src)
        donor_tree = importer.inventory(self.donor)
        with mock.patch("subprocess.run", side_effect=AssertionError("no donor execution")):
            entry = self.run_phase()
        self.assertEqual(entry["status"], "hit", entry)
        self.assertGreater(entry["counts"]["files_copied"], 15)
        self.assertGreater(entry["counts"]["bytes_copied"], 1000)
        self.assertEqual(entry["counts"]["directories_reused"], 2)
        for relative in (importer.CLANG, importer.RUST):
            self.assertEqual(importer.inventory(self.src / relative), importer.inventory(self.donor / relative))
        for relative, value in original.items():
            self.assertEqual(importer.inventory(self.src)[relative], value)
        self.assertEqual(importer.inventory(self.donor), donor_tree)
        self.assertFalse((self.src / ".chromix-toolchain-ready").exists())
        marker = importer.read_json(self.src / importer.MARKER)
        self.assertEqual(marker["reused"], {"clang": True, "rust": True, "bindgen": True})
        self.assertIs(marker["sysroot_reused"], False)
        self.assertFalse((self.src / "build/linux").exists())

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("clang"), "native Linux clang required")
    def test_imported_native_clang_can_execute(self):
        host = os.uname().machine
        if host not in ("x86_64", "aarch64"):
            self.skipTest("unsupported native test host")
        self.prepare(arch="x64" if host == "x86_64" else "arm64")
        clang = Path(shutil.which("clang")).resolve()
        shutil.copy2(clang, self.donor / importer.CLANG / "bin/clang")
        entry = self.run_phase()
        self.assertEqual(entry["status"], "hit", entry)
        installed = self.src / importer.CLANG / "bin/clang"
        result = subprocess.run([str(installed), "--version"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("clang version", result.stdout)
        self.assertEqual(importer.digest_file(clang), importer.digest_file(installed))

    def test_linux_arm64_hit_checks_machine_and_rust_target(self):
        self.prepare(arch="arm64")
        self.assertEqual(self.run_phase()["status"], "hit")
        self.assertTrue((self.src / importer.RUST / "lib/rustlib/aarch64-unknown-linux-gnu/lib").is_dir())

    def test_windows_arm64_identity_and_bindgen_use_selected_pin_and_x64_executables(self):
        self.prepare("windows", "arm64")
        self.assertEqual(self.identity["chromium_version"], "153.0.8010.47")
        self.assertEqual(self.identity["run_id"], 103)
        self.assertEqual(self.identity["workflow_path"], ".github/workflows/build-arm.yml")
        self.assertEqual(self.identity["artifact_id"], 104)
        before = importer.inventory(self.src / importer.CLANG)
        source = (self.src / "chrome/canonical.cc").read_bytes()
        with mock.patch.object(importer, "require_binary", wraps=importer.require_binary) as check:
            entry = self.run_phase()
        self.assertEqual(entry["status"], "hit", entry)
        self.assertTrue(all(call.args[1:] == ("windows", "x64") for call in check.call_args_list))
        self.assertEqual(entry["reused"], {"clang": False, "rust": False, "bindgen": True})
        self.assertEqual(entry["counts"]["objects_copied"], 0)
        self.assertEqual(importer.inventory(self.src / importer.CLANG), before)
        self.assertEqual((self.src / "chrome/canonical.cc").read_bytes(), source)
        self.assertFalse((self.src / importer.MARKER).exists())

    def test_windows_arm64_import_rejects_wrong_host_binaries_target_libraries_and_x64_receipt(self):
        for relative in (importer.CLANG / "bin/clang-cl.exe", importer.RUST / "bin/rustc.exe",
                         importer.RUST / "bin/bindgen.exe"):
            with self.subTest(binary=relative):
                self.prepare("windows", "arm64")
                self.binary(self.donor / relative, arch="arm64")
                self.assert_miss(self.run_phase(), "host architecture")
                self.assertFalse((self.src / importer.RUST / "bin/bindgen.exe").exists())
        for cpu in ("x64", "arm64"):
            with self.subTest(libraries=cpu):
                self.prepare("windows", "arm64")
                shutil.rmtree(self.donor / importer.RUST / "lib/rustlib" / importer.TRIPLES["windows", cpu])
                self.assert_miss(self.run_phase(), "required toolchain libraries")
        self.prepare("windows", "arm64")
        self.result["manifest"]["run_id"] = 101
        self.save_receipt()
        self.assert_miss(self.run_phase(), "pinned identity mismatch: run_id")
        self.prepare("windows", "arm64")
        (self.donor / importer.CLANG / "lib/clang/23/lib/windows/clang_rt.builtins-aarch64.lib").unlink()
        self.assert_miss(self.run_phase(), "required toolchain libraries")

    def test_windows_arm64_does_not_enable_relocated_object_import(self):
        self.prepare("windows", "arm64")
        self.assertEqual(self.run_phase()["status"], "hit")
        self.object_args('target_cpu = "arm64"\nhost_cpu = "x64"\n')
        entry = self.run_phase("objects")
        self.assert_miss(entry, "external SDK identity and environment are unavailable")
        self.assertFalse((self.src / "out/Chromix/obj/object.o").exists())

    def test_current_clang_target_triple_runtime_layout_hits(self):
        resource = self.donor / importer.CLANG / "lib/clang/23"
        (resource / "lib/linux/libclang_rt.builtins-x86_64.a").unlink()
        self.write(resource / "lib/x86_64-unknown-linux-gnu/libclang_rt.builtins.a", b"!<arch>\nfixture")
        self.assertEqual(self.run_phase()["status"], "hit")

    def test_downloader_manifest_identity_matches_importer_contract(self):
        from tools.fetch_upstream_cache import load_manifest
        _, manifest = load_manifest(self.platform, self.arch, root=self.repo)
        self.result["manifest"] = manifest
        self.save_receipt()
        self.assertEqual(self.run_phase()["status"], "hit")

    def test_linux_idempotent_hit_does_not_recopy(self):
        self.assertEqual(self.run_phase()["status"], "hit")
        with mock.patch.object(importer.shutil, "copytree", side_effect=AssertionError("must not recopy")):
            entry = self.run_phase()
        self.assertEqual(entry["status"], "hit")
        self.assertEqual(entry["counts"]["files_copied"], 0)
        report = importer.read_json(self.work / importer.REPORT)
        self.assertEqual(list(report["phases"]), ["toolchain"])

    def test_changed_installed_content_rejects_existing_marker(self):
        self.run_phase()
        self.write(self.src / importer.CLANG / "bin/clang", "broken", 0o755)
        self.assert_miss(self.run_phase(), "binary format")
        self.assertFalse((self.src / importer.MARKER).exists())

    def test_changed_scripts_invalidate_previous_skip_marker(self):
        self.run_phase()
        self.write(self.src / "tools/rust/build_bindgen.py", "changed canonical script\n")
        self.assert_miss(self.run_phase(), "scripts changed after import")
        self.assertFalse((self.src / importer.MARKER).exists())

    def test_platform_bindgen_hit_requires_identical_compiler_and_rust(self):
        for platform in ("macos", "windows"):
            with self.subTest(platform=platform):
                self.prepare(platform)
                before = importer.inventory(self.src / importer.CLANG)
                entry = self.run_phase()
                self.assertEqual(entry["status"], "hit", entry)
                self.assertEqual(importer.inventory(self.src / importer.CLANG), before)
                self.assertEqual(entry["reused"], {"clang": False, "rust": False, "bindgen": True})
                self.assertFalse((self.src / importer.MARKER).exists())

    def test_same_stamp_but_changed_canonical_compiler_rejects_bindgen(self):
        self.prepare("macos")
        path = self.src / importer.CLANG / "bin/clang"
        path.write_bytes(path.read_bytes() + b"different build options")
        self.assert_miss(self.run_phase(), "canonical compiler content differs")
        self.assertFalse((self.src / importer.RUST / "bin/bindgen").exists())

    def test_same_stamp_but_changed_canonical_rust_rejects_bindgen(self):
        self.prepare("windows")
        path = self.src / importer.RUST / "bin/cargo.exe"
        path.write_bytes(path.read_bytes() + b"different Rust build")
        self.assert_miss(self.run_phase(), "canonical Rust content differs")

    def test_manifest_receipt_pin_rejections(self):
        for field in ("sha256", "head_sha", "run_id", "artifact_id", "artifact_digest", "target", "repository"):
            with self.subTest(field=field):
                self.receipt()
                self.result["manifest"][field] = "incorrect"
                self.save_receipt()
                self.assert_miss(self.run_phase(), "pinned identity mismatch")
                self.assertFalse((self.src / importer.CLANG).exists())

    def test_platform_arch_receipt_rejection(self):
        self.result["arch"] = "arm64"
        self.save_receipt()
        self.assert_miss(self.run_phase(), "platform/architecture mismatch")

    def test_changed_repository_manifest_pin_rejection(self):
        manifest_path = self.repo / "build/upstream-cache.json"
        manifest = importer.read_json(manifest_path)
        manifest["sources"]["linux"]["head_sha"] = "0" * 40
        manifest_path.write_text(json.dumps(manifest))
        self.assert_miss(self.run_phase(), "platform commit does not match")

    def test_canonical_final_version_required(self):
        self.write(self.src / "chrome/VERSION", "MAJOR=1\nMINOR=0\nBUILD=0\nPATCH=0\n")
        self.assert_miss(self.run_phase(), "canonical final chrome/VERSION")

    def test_canonical_preparation_marker_required_not_manufactured(self):
        (self.src / ".chromix-source-ready").unlink()
        self.assertEqual(self.run_phase()["status"], "miss")
        self.assertFalse((self.src / ".chromix-source-ready").exists())

    def test_recursive_script_changes_rejected_and_pycache_ignored(self):
        self.write(self.donor / "tools/clang/scripts/nested/helper.py", "changed")
        self.assert_miss(self.run_phase(), "tools/clang scripts differ")
        shutil.rmtree(self.donor / "tools/clang/scripts/nested")
        self.write(self.donor / "tools/clang/scripts/__pycache__/update.pyc", b"irrelevant")
        self.write(self.src / "tools/rust/__pycache__/unrelated.pyc", b"irrelevant")
        self.assertEqual(self.run_phase()["status"], "hit")

    def test_static_constant_expression_is_not_executed(self):
        for root in (self.src, self.donor):
            self.write(root / "tools/rust/update_rust.py", "RUST_REVISION = __import__('os').getcwd()\nRUST_SUB_REVISION = 5\n")
        self.assert_miss(self.run_phase(), "nonliteral update constant")

    def test_stale_stamps_rejected(self):
        for relative in (importer.CLANG / "cr_build_revision", importer.RUST / "VERSION"):
            with self.subTest(relative=relative):
                self.prepare()
                self.write(self.donor / relative, "incorrect")
                self.assert_miss(self.run_phase(), "does not match static source constants")

    def test_empty_missing_or_wrong_arch_binaries_rejected(self):
        path = self.donor / importer.CLANG / "bin/clang"
        self.write(path, b"", 0o755)
        self.assert_miss(self.run_phase(), "empty required")
        self.binary(path, arch="arm64")
        self.assert_miss(self.run_phase(), "host architecture")
        path.unlink()
        self.assert_miss(self.run_phase(), "missing or empty required")

    def test_missing_rust_and_clang_libraries_rejected(self):
        for relative in (importer.RUST / "lib/libclang.so.23", importer.RUST / "lib/rustlib/x86_64-unknown-linux-gnu/lib/libstd-abc123.rlib",
                         importer.CLANG / "lib/clang/23/lib/linux/libclang_rt.builtins-x86_64.a"):
            with self.subTest(relative=relative):
                self.prepare()
                (self.donor / relative).unlink()
                self.assert_miss(self.run_phase(), "missing required toolchain libraries")

    def test_nonexecutable_binary_rejected(self):
        (self.donor / importer.RUST / "bin/bindgen").chmod(0o644)
        self.assert_miss(self.run_phase(), "not executable")

    def test_external_symlink_rejected_internal_symlink_preserved(self):
        path = self.donor / importer.CLANG / "bin/clang++"
        path.unlink()
        path.symlink_to(self.root / "external")
        self.write(self.root / "external", "outside")
        self.assert_miss(self.run_phase(), "external toolchain symlink")
        path.unlink()
        path.symlink_to("clang")
        self.assertEqual(self.run_phase()["status"], "hit")
        self.assertEqual(os.readlink(self.src / importer.CLANG / "bin/clang++"), "clang")

    def test_internal_directory_aliases_preserve_toolchain_identity(self):
        resource = self.donor / importer.CLANG / "lib/clang/23/lib"
        (resource / "alias").symlink_to("linux", target_is_directory=True)
        entry = self.run_phase()
        self.assertEqual(entry["status"], "hit", entry)
        alias = self.src / importer.CLANG / "lib/clang/23/lib/alias"
        self.assertTrue(alias.is_symlink())
        self.assertEqual(os.readlink(alias), "linux")
        self.assertEqual(importer.inventory(self.src / importer.CLANG), importer.inventory(self.donor / importer.CLANG))

    def test_symlinked_canonical_parent_is_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.src / "third_party").symlink_to(outside, target_is_directory=True)
        self.assert_miss(self.run_phase(), "symlinked directory")
        self.assertEqual(list(outside.iterdir()), [])

    def test_source_path_escape_does_not_consume_outside_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.write(outside / "keep", "outside")
        self.result["source"] = str(outside)
        self.save_receipt()
        self.assert_miss(self.run_phase(), "not inside cache")
        self.assertTrue((outside / "keep").exists())

    def test_copy_failure_keeps_both_canonical_toolchains(self):
        for relative in (importer.CLANG, importer.RUST):
            self.write(self.src / relative / "keep", "canonical existing download")
        before = importer.inventory(self.src)
        real_copy = shutil.copytree
        def fail_rust(source, destination, *args, **kwargs):
            if Path(source) == self.donor / importer.RUST:
                raise OSError("simulated disk full")
            return real_copy(source, destination, *args, **kwargs)
        with mock.patch.object(importer.shutil, "copytree", side_effect=fail_rust):
            self.assert_miss(self.run_phase(), "simulated disk full")
        self.assertEqual(importer.inventory(self.src), before)

    def test_second_directory_install_failure_rolls_back_first(self):
        for relative in (importer.CLANG, importer.RUST):
            self.write(self.src / relative / "keep", "canonical existing download")
        before = importer.inventory(self.src)
        real_replace = os.replace
        def fail_rust(source, destination):
            if Path(source).name == "rust" and Path(destination) == self.src / importer.RUST:
                raise OSError("simulated rename failure")
            return real_replace(source, destination)
        with mock.patch.object(importer.os, "replace", side_effect=fail_rust):
            self.assert_miss(self.run_phase(), "simulated rename failure")
        self.assertEqual(importer.inventory(self.src), before)
        self.assertFalse((self.src / importer.MARKER).exists())

    def test_marker_failure_rolls_back_both_toolchains(self):
        for relative in (importer.CLANG, importer.RUST):
            self.write(self.src / relative / "keep", "canonical existing download")
        before = importer.inventory(self.src)
        real_write = importer.write_json
        def fail_marker(path, value):
            if path.name == importer.MARKER:
                raise OSError("simulated marker failure")
            return real_write(path, value)
        with mock.patch.object(importer, "write_json", side_effect=fail_marker):
            self.assert_miss(self.run_phase(), "simulated marker failure")
        self.assertEqual(importer.inventory(self.src), before)

    def test_resume_marker_prevents_read_and_consumption(self):
        for phase in ("toolchain", "objects"):
            with self.subTest(phase=phase):
                self.write(self.work / ".chromix-resumed", "restored")
                before = importer.inventory(self.cache)
                real_read = importer.read_json
                def no_receipt(path):
                    self.assertNotEqual(path, self.cache / "result.json")
                    return real_read(path)
                with mock.patch.object(importer, "read_json", side_effect=no_receipt):
                    self.assert_miss(self.run_phase(phase), "resume marker exists")
                self.assertEqual(importer.inventory(self.cache), before)

    def test_objects_gn_mismatch_preserves_canonical_output_and_cleans_donor(self):
        self.prepare("macos")
        self.assertEqual(self.run_phase()["status"], "hit")
        self.object_args(donor='target_cpu = "x64"\nis_debug = true\n')
        before = importer.inventory(self.src)
        entry = self.run_phase("objects")
        self.assert_miss(entry, "GN assignments differ: is_debug")
        self.assertEqual(importer.inventory(self.src), before)
        self.assertFalse((self.cache / "tree").exists())
        self.assertTrue((self.cache / "result.json").exists())
        report = importer.read_json(self.work / importer.REPORT)
        self.assertEqual(set(report["phases"]), {"toolchain", "objects"})
        self.assertEqual(report["phases"]["toolchain"]["status"], "hit")

    def test_objects_require_target_arch(self):
        self.prepare("macos")
        self.object_args(canonical="is_debug = false\n")
        self.assert_miss(self.run_phase("objects"), "mandatory GN target_cpu")

    def test_linux_objects_require_canonical_graph(self):
        self.run_phase()
        self.object_args()
        self.assert_miss(self.run_phase("objects"), "canonical GN generation required")
        self.assertFalse((self.src / "out/Chromix/obj").exists())
        self.assertFalse((self.src / "out/Chromix/.ninja_log").exists())

    def test_linux_ready_candidates_keep_donor_until_stage_finalization(self):
        from tools import upstream_object_cache
        self.run_phase()
        self.object_args()
        with mock.patch.object(upstream_object_cache, "prepare", return_value={
                "status": "ready", "counts": {"prepared": 1}, "reasons": []}) as prepare:
            entry = self.run_phase("objects")
        self.assertEqual(entry["status"], "ready")
        prepare.assert_called_once_with(self.src, self.donor, "linux", "x64", self.work, allow_truncated_mtimes=False)
        self.assertTrue(self.donor.exists())
        objects = self.work / ".upstream-objects"
        self.write(objects / "receipts/hit.json", json.dumps({"status": "hit", "returncode": 0, "bytes": 128}))
        self.write(objects / "receipts/miss.json", json.dumps({"status": "miss", "reason": "changed command"}))
        self.write(objects / "generation-test-ready/object.o", b"unused object")
        self.write(self.src / "out/Chromix/obj/accepted.o", b"verified object")
        self.write(self.src / "out/Chromix/.ninja_log", "canonical Ninja log")
        report = importer.finalize_objects(self.work, self.cache)
        self.assertEqual((report["hits"], report["misses"], report["bytes_reused"]), (1, 1, 128))
        self.assertEqual(report["cleanup_errors"], [])
        self.assertFalse(self.donor.exists())
        self.assertFalse(objects.exists())
        self.assertEqual((self.src / "out/Chromix/obj/accepted.o").read_bytes(), b"verified object")
        self.assertEqual((self.src / "out/Chromix/.ninja_log").read_text(), "canonical Ninja log")

    def test_future_donor_mtime_never_changes_canonical_input_mtime(self):
        self.run_phase()
        self.object_args()
        canonical = self.src / "chrome/canonical.cc"
        before = (canonical.read_bytes(), canonical.stat().st_mtime_ns, canonical.stat().st_mode)
        future = 4_000_000_000_000_000_000
        os.utime(self.donor / "chrome/canonical.cc", ns=(future, future))
        self.assertEqual(self.run_phase("objects")["status"], "miss")
        self.assertEqual((canonical.read_bytes(), canonical.stat().st_mtime_ns, canonical.stat().st_mode), before)
        self.assertFalse((self.src / "out/Chromix/.ninja_deps").exists())

    def test_cleanup_does_not_delete_unowned_or_locked_cache(self):
        self.result["owner"] = "other-tool"
        self.save_receipt()
        self.assert_miss(self.run_phase("objects"), "owned verified hit")
        self.assertTrue(self.donor.exists())
        self.receipt()
        self.write(self.cache / ".lock", "downloader is writing")
        self.assert_miss(self.run_phase("objects"), "still writing")
        self.assertTrue(self.donor.exists())

    def test_external_tool_links_preserve_existence_without_execution(self):
        path = self.donor / "third_party/node/linux/node-linux-x64/bin/node"
        name = path.relative_to(self.cache).as_posix()
        self.result.update(skipped_external_symlinks=1, external_symlink_paths=[name])
        importer.preserve_external_tool_lookups(self.cache, self.donor, self.result)
        self.assertTrue(path.is_file())
        self.assertIn("#error", path.read_text())
        self.assertFalse(os.access(path, os.X_OK))
        self.result["external_symlink_paths"] = ["tree/build/src/include/external.h"]
        with self.assertRaisesRegex(importer.Miss, "unknown external symlink"):
            importer.preserve_external_tool_lookups(self.cache, self.donor, self.result)

    def test_external_links_without_paths_reject_objects(self):
        self.object_args()
        self.result["skipped_external_symlinks"] = 1
        self.save_receipt()
        self.assert_miss(self.run_phase("objects"), "omitted links were not recorded")

    def test_linux_missed_candidates_release_donor(self):
        from tools import upstream_object_cache
        self.object_args()
        with mock.patch.object(upstream_object_cache, "prepare", return_value={
                "status": "miss", "counts": {}, "reasons": ["different sysroot"]}):
            self.assert_miss(self.run_phase("objects"), "different sysroot")
        self.assertFalse(self.donor.exists())

    def test_objects_toolchain_mismatch_rejected(self):
        self.prepare("macos")
        self.run_phase()
        self.object_args()
        self.write(self.donor / importer.CLANG / "bin/clang", "other bytes", 0o755)
        self.assert_miss(self.run_phase("objects"), "object toolchain content differs")

    def test_strict_gn_parser_rejects_expressions_duplicates_and_missing_types(self):
        args = self.root / "args.gn"
        for content in ('target_cpu = getenv("ARCH")', 'target_cpu = "x64"\ntarget_cpu = "x64"',
                        'target_cpu = "$ARCH"', 'a = null', 'a = 1.5', 'a = {"x": 1}', 'a = true || false'):
            with self.subTest(content=content):
                self.write(args, content)
                with self.assertRaises(importer.Miss):
                    importer.gn_assignments(args)
        self.write(args, '# comment\na = ["x", 1, false] # inline\nb = true\n')
        self.assertEqual(importer.gn_assignments(args), {"a": '["x",1,false]', "b": 'true'})

    def test_cli_disabled_windows_fixture_returns_miss_with_no_import(self):
        path = self.repo / "build/upstream-cache.json"
        manifest = importer.read_json(path)
        manifest["sources"]["windows"] = unavailable_source(
            self.repo, "windows", manifest["sources"]["windows"])
        self.write(path, json.dumps(manifest))
        for name in ("import_upstream_cache.py", "fetch_upstream_cache.py", "platform_pins.py",
                     "upstream_script_identity.py"):
            self.write(self.repo / "tools" / name, (importer.REPO / "tools" / name).read_bytes())
        for arch in ("x64", "arm64"):
            result = subprocess.run([sys.executable, str(self.repo / "tools/import_upstream_cache.py"),
                                     "--phase", "toolchain", "--platform", "windows", "--arch", arch,
                                     "--workdir", str(self.work), "--cache-dir", str(self.cache)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("source_unavailable", result.stdout)
            report = importer.read_json(self.work / importer.REPORT)["phases"]["toolchain"]
            self.assert_miss(report, "source_unavailable")
            self.assertEqual(report["counts"]["bytes_copied"], 0)
            self.assertFalse((self.src / importer.CLANG).exists())

    def test_cli_missing_cache_reports_miss_and_exits_zero(self):
        result = subprocess.run([sys.executable, str(importer.REPO / "tools/import_upstream_cache.py"),
                                 "--phase", "toolchain", "--platform", "linux", "--arch", "x64",
                                 "--workdir", str(self.work), "--cache-dir", str(self.work / "absent-cache")],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("toolchain: miss", result.stdout)
        self.assertEqual(importer.read_json(self.work / importer.REPORT)["phases"]["toolchain"]["status"], "miss")


if __name__ == "__main__":
    unittest.main()
