"""mac154 generators reject unknown bytes and publish only pinned local inputs."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
from unittest import mock

if os.name != "posix":
    raise unittest.SkipTest("macOS generator file operations require POSIX directory descriptors")

from tools import macos_restored_generators as gen
from tools import prepare_restored_build as prepare
from tools import platform_pins


class MacGeneratorsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.src = self.work / "src"
        self.src.mkdir()
        self.patch(platform_pins, "load_pins", return_value=dict(zip(
            ("ChromiumVersion", "UngoogledCommit", "UngoogledMacOSCommit"), gen.PINS)))
        self.archives, self.files = {}, {}
        manifest = {}
        for key, entry in gen.ARCHIVES.items():
            files = {"package.json": (json.dumps({"name": entry["package"], "version": entry["version"]}).encode(), 0o644),
                     "lib/input.js": (b"fixture package, never executed", 0o644)}
            if key.endswith(("arm64", "x64")):
                host = key.rsplit("-", 1)[1]
                binary = bytearray(64)
                binary[:4] = b"\xcf\xfa\xed\xfe"
                struct.pack_into("<I", binary, 4, {"arm64": 0x100000C, "x64": 0x1000007}[host])
                files["bin/esbuild" if key.startswith("esbuild") else "lib/tsc"] = (bytes(binary), 0o755)
            self.files[key] = files
            data = self.tar(files)
            self.archives[key] = data
            manifest[key] = dict(entry, sha256=gen.sha256(data), integrity=self.integrity(data),
                                 tree_sha256=gen.tree_digest([[n, m, gen.sha256(d)] for n, (d, m) in files.items()]),
                                 files=len(files))
        self.patch(gen, "ARCHIVES", manifest)
        repairs = {}
        for name, (_, _, replacements) in gen.SOURCE_REPAIRS.items():
            original = b"fixture source, never executed\n" + b"\n".join(old for old, _ in replacements)
            repaired = original
            for old, new in replacements:
                repaired = repaired.replace(old, new)
            repairs[name] = gen.sha256(original), gen.sha256(repaired), replacements
            self.write(gen.DEVTOOLS + "/" + name, original)
        self.patch(gen, "SOURCE_REPAIRS", repairs)

    def patch(self, obj, name, *args, **kwargs):
        patcher = mock.patch.object(obj, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def write(self, name, data):
        path = self.src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    @staticmethod
    def integrity(data):
        return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()

    @staticmethod
    def tar(files):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, (data, mode) in files.items():
                member = tarfile.TarInfo("package/" + name)
                member.size, member.mode = len(data), mode
                archive.addfile(member, io.BytesIO(data))
        return stream.getvalue()

    def install(self, host="arm64"):
        for key in gen.package_keys(host):
            gen.install_package(self.src, key, gen.archive_files(self.archives[key], key))

    def inspect(self, **kwargs):
        return gen.prepare(self.src, host_arch="arm64", repo=self.work, **kwargs)

    def test_inspect_install_repair_and_resume(self):
        before = self.inspect()
        self.assertTrue(before["install_needed"])
        self.install()
        installed = self.inspect()
        self.assertFalse(installed["install_needed"])
        self.assertTrue(installed["repair_needed"])
        self.assertNotEqual(before, installed)
        with mock.patch.object(gen, "smoke", return_value={}) as smoke:
            repaired = self.inspect(repair=True)
            self.assertFalse(repaired["repair_needed"])
            self.assertEqual(self.inspect(repair=True), repaired)
            self.assertEqual(smoke.call_count, 2)
            fingerprint = {key: value for key, value in repaired.items() if key != "probe"}
            self.assertEqual(self.inspect(), fingerprint)
            self.assertEqual(smoke.call_count, 2)
            self.assertEqual(repaired["probe"]["generator_fingerprint_sha256"],
                             gen.sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()))
        for entry in repaired["sources"].values():
            self.assertEqual(entry["sha256"], entry["repaired_sha256"])
            self.assertNotEqual(entry["sha256"], entry["original_sha256"])

    def test_failed_native_probe_never_repairs_sources(self):
        self.install()
        before = gen.source_plan(self.src)[0]
        with mock.patch.object(gen, "smoke", side_effect=ValueError("native compile failed")), \
                self.assertRaisesRegex(ValueError, "native compile failed"):
            self.inspect(repair=True)
        self.assertEqual(gen.source_plan(self.src)[0], before)

    def test_unknown_source_and_pins_fail_before_probes(self):
        name = next(iter(gen.SOURCE_REPAIRS))
        path = self.src / gen.DEVTOOLS / name
        path.write_bytes(path.read_bytes() + b"unknown")
        with mock.patch.object(gen, "smoke") as smoke, self.assertRaisesRegex(ValueError, "unknown macOS"):
            self.inspect(repair=True)
        smoke.assert_not_called()
        with mock.patch.object(platform_pins, "load_pins", return_value={
                "ChromiumVersion": gen.PINS[0], "UngoogledCommit": "unknown", "UngoogledMacOSCommit": gen.PINS[2]}), \
                self.assertRaisesRegex(ValueError, "exact reviewed pins"):
            self.inspect()

    def test_archives_require_both_integrity_and_full_inventory(self):
        key = "esbuild"
        data = self.archives[key]
        for field, bad in (("sha256", "0" * 64), ("integrity", "sha512-invalid"),
                           ("tree_sha256", "0" * 64), ("files", 999)):
            with self.subTest(field=field), mock.patch.dict(gen.ARCHIVES[key], {field: bad}), self.assertRaises(ValueError):
                gen.archive_files(data, key)
        data = self.tar({"../escape": (b"bad", 0o644)})
        with mock.patch.dict(gen.ARCHIVES[key], {"sha256": gen.sha256(data), "integrity": self.integrity(data)}), \
                self.assertRaisesRegex(ValueError, "unsafe"):
            gen.archive_files(data, key)

    def test_package_unknown_extra_mode_link_and_empty_directory_are_rejected(self):
        self.install()
        root = self.src / gen.package_path("esbuild")
        for kind in ("extra", "mode", "hardlink", "symlink", "directory"):
            path = root / "lib/input.js"
            original = path.read_bytes()
            with self.subTest(kind=kind):
                if kind == "extra": (root / "extra").write_bytes(b"extra")
                elif kind == "mode": path.chmod(0o755)
                elif kind == "hardlink": os.link(path, self.work / "outside")
                elif kind == "symlink":
                    path.unlink()
                    path.symlink_to(self.work / "outside")
                else: (root / "empty").mkdir()
                with self.assertRaises((ValueError, OSError)):
                    self.inspect()
                if kind == "extra": (root / "extra").unlink()
                elif kind == "mode": path.chmod(0o644)
                elif kind == "hardlink": (self.work / "outside").unlink()
                elif kind == "symlink":
                    path.unlink()
                    path.write_bytes(original)
                else: (root / "empty").rmdir()

    def test_atomic_repair_rejects_destination_and_parent_races(self):
        for parent_race in (False, True):
            with self.subTest(parent_race=parent_race):
                relative = "race/file"
                path = self.write(relative, b"original")
                def race(_):
                    if parent_race:
                        path.parent.rename(self.src / "moved")
                        path.parent.mkdir()
                        path.write_bytes(b"replacement parent")
                    else:
                        path.write_bytes(b"concurrent writer")
                with self.assertRaises(ValueError):
                    gen.atomic_replace(self.src, relative, b"original", b"repaired", before_publish=race)
                self.assertNotEqual(path.read_bytes(), b"repaired")
                if parent_race:
                    self.assertEqual((self.src / "moved/file").read_bytes(), b"original")

    def test_linked_source_and_package_parents_fail_closed(self):
        name = next(iter(gen.SOURCE_REPAIRS))
        path = self.src / gen.DEVTOOLS / name
        external = self.work / "source-copy"
        external.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(external)
        with self.assertRaises(ValueError):
            self.inspect()
        path.unlink()
        path.write_bytes(external.read_bytes())
        parent = self.src / gen.PACKAGES
        parent.parent.mkdir(parents=True, exist_ok=True)
        outside = self.work / "outside-packages"
        outside.mkdir()
        parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            gen.install_package(self.src, "esbuild", self.files["esbuild"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_package_publish_never_overwrites_racing_destination(self):
        def race(path):
            path.mkdir()
            (path / "racer").write_bytes(b"preserved")
        with self.assertRaises(OSError):
            gen.install_package(self.src, "esbuild", self.files["esbuild"], before_publish=race)
        self.assertEqual((self.src / gen.package_path("esbuild") / "racer").read_bytes(), b"preserved")

    def test_wrong_host_and_binary_architecture_fail_without_execution(self):
        self.install()
        node = self.write(gen.native_paths("arm64")["node"], self.files["esbuild-x64"]["bin/esbuild"][0])
        node.chmod(0o755)
        with mock.patch.object(prepare, "host_identity", return_value=("macos", "arm64")), \
                mock.patch.object(gen.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "header/mode"):
            gen.smoke(self.src, "arm64")
        run.assert_not_called()
        with mock.patch.object(prepare, "host_identity", return_value=("linux", "arm64")), \
                self.assertRaisesRegex(ValueError, "matching native host"):
            gen.smoke(self.src, "arm64")

    def test_smoke_executes_native_tsc_with_empty_path_and_records_tool_hashes(self):
        self.install()
        paths = gen.native_paths("arm64")
        node = self.write(paths["node"], self.files["esbuild-arm64"]["bin/esbuild"][0])
        node.chmod(0o755)
        commands = []
        def run(command, **kwargs):
            self.assertEqual(kwargs["env"]["PATH"], "")
            commands.append(command)
            if command[-1] == "--version":
                output = ("v24.12.0" if command == [str(node), "--version"] else
                          "0.28.2" if command[0].endswith("/bin/esbuild") else "Version 7.0.2")
            else:
                output = "42" if command[-1].endswith(".js") else ""
            return mock.Mock(returncode=0, stdout=output)
        with mock.patch.object(prepare, "host_identity", return_value=("macos", "arm64")), \
                mock.patch.object(gen.subprocess, "run", side_effect=run):
            probe = gen.smoke(self.src, "arm64")
        self.assertIn([str(self.src / paths["tsc"]), "sample.ts", "--outDir", "native-compiled",
                       "--target", "es2023"], commands)
        self.assertTrue(probe["typescript_native_compile"])
        self.assertTrue(probe["path_search_disabled"])
        for name, relative in paths.items():
            self.assertEqual(probe["tools"][name]["sha256"], gen.sha256((self.src / relative).read_bytes()))
            self.assertTrue(probe["tools"][name]["version"])

    def test_host_package_selection_and_manifest_fingerprint(self):
        self.install("x64")
        self.assertTrue(self.inspect()["install_needed"])
        digest = gen.manifest_digest()
        with mock.patch.dict(gen.ARCHIVES["esbuild"], {"tree_sha256": "0" * 64}):
            self.assertNotEqual(gen.manifest_digest(), digest)
        with self.assertRaises(ValueError):
            gen.package_keys("amd64")


class MacGeneratorsRealSourceTest(unittest.TestCase):
    def test_pinned_fixture_derives_all_repaired_bytes(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/macos154_generators.json").read_text())
        self.assertEqual((fixture["chromium_version"], fixture["core"], fixture["mac"]), gen.PINS)
        for name, (before, after, replacements) in gen.SOURCE_REPAIRS.items():
            with self.subTest(name=name):
                data = fixture["sources"][name].encode()
                self.assertEqual(gen.sha256(data), before)
                for old, new in replacements:
                    self.assertIn(old, data)
                    data = data.replace(old, new)
                self.assertEqual(gen.sha256(data), after)
                self.assertEqual(fixture["repaired_sha256"][name], after)
                if name == "scripts/build/typescript/typescript_vars.gni":
                    self.assertIn(b'@typescript/typescript-darwin-${host_cpu}/lib/tsc"', data)
                    self.assertNotIn(b"node_modules/typescript/bin/tsc", data)
                self.assertNotIn(b"/opt/homebrew", data)
                if name.endswith(".py"):
                    compile(data, name, "exec")

    def test_repaired_wrapper_resolves_local_node_and_package(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/macos154_generators.json").read_text())
        name = "third_party/typescript/typescript.py"
        data = fixture["sources"][name].encode()
        for old, new in gen.SOURCE_REPAIRS[name][2]:
            data = data.replace(old, new)
        namespace = {"__name__": "fixture", "__file__": "/checkout/src/" + gen.DEVTOOLS + "/" + name}
        exec(compile(data, name, "exec"), namespace)
        for arch, machine in (("x64", "x86_64"), ("arm64", "arm64")):
            with mock.patch("platform.machine", return_value=machine):
                self.assertEqual(namespace["GetNodePath"](), "/checkout/src/" + gen.native_paths(arch)["node"])
                self.assertEqual(namespace["GetBinaryPath"](), "/checkout/src/" + gen.package_path("typescript") + "/bin/tsc")


if __name__ == "__main__":
    unittest.main()
