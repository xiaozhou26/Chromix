"""Linux155 consumers select exact profiles without weakening receipt or byte checks."""
import io
import json
import os
import shutil
import struct
import subprocess
import tarfile
import unittest
from unittest import mock

from tools import install_linux_generators as installer
from tools import linux_restored_generators as generators
from tools import prepare_restored_build as prepare
from tools import restore_upstream_cache as restore
from tools.tests import test_prepare_restored_build as prepare_tests
from tools.tests.test_linux154_generators import PIN_KEYS, tree_hash
from tools.tests.test_linux155_generators import SCRIPTS


URLS = {
    "module": "https://registry.npmjs.org/esbuild/-/esbuild-0.28.2.tgz",
    "x64": "https://registry.npmjs.org/@esbuild/linux-x64/-/linux-x64-0.28.2.tgz",
    "arm64": "https://registry.npmjs.org/@esbuild/linux-arm64/-/linux-arm64-0.28.2.tgz",
}


class InstallLinux155GeneratorsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = prepare_tests.PrepareRestoredBuildTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.fixture("linux", "arm64", host_arch="x64", linux_pins=generators.PINS155)
        self.src, self.work, self.repo = self.fixture.src, self.fixture.work, self.fixture.fixture_repo
        self.downloads = self.work / "offline downloads"
        self.downloads.mkdir()
        for name, text in SCRIPTS.items():
            self.write(generators.DEVTOOLS + "/" + name, text.encode())
        self.write(generators.LOCK, b"independent Linux155 lock fixture\n")
        self.patch(generators, "LOCK155_SHA256", generators.sha256((self.src / generators.LOCK).read_bytes()))
        self.patch(generators, "TYPESCRIPT155_TREE", tree_hash(self.src / prepare.TYPESCRIPT_PACKAGE))
        self.modules = {name: b"independent Linux155 module: " + name.encode()
                        for name in generators.MODULE155_FILES}
        self.patch(generators, "MODULE155_FILES", {name: generators.sha256(data)
                                                   for name, data in self.modules.items()})
        self.binaries = {}
        for host, cpu in (("x64", 62), ("arm64", 183)):
            data = bytearray(64)
            data[:6] = b"\x7fELF\x02\x01"
            struct.pack_into("<H", data, 18, cpu)
            self.binaries[host] = bytes(data) + b"Linux155 fixture"
            self.fixture.binary(self.src / prepare.tool_paths("linux", host)["node"], "linux", host)
        self.patch(generators, "BINARY155_HASHES", {host: generators.sha256(data)
                                                    for host, data in self.binaries.items()})
        self.archives = {"module": self.archive(self.modules)}
        self.archives.update({host: self.archive({"bin/esbuild": data, "package.json": b"{}",
                                                  "README.md": b"independent fixture"})
                              for host, data in self.binaries.items()})
        self.assertEqual({key: value[0] for key, value in generators.ARCHIVES155.items()}, URLS)
        self.patch(generators, "ARCHIVES155", {key: (URLS[key], generators.sha256(data))
                                               for key, data in self.archives.items()})
        for key, data in self.archives.items():
            self.cached(key).write_bytes(data)
        self.patch(prepare, "host_identity", return_value=("linux", "x64"))
        self.network = self.patch(installer, "fetch_archive", side_effect=AssertionError("unexpected download"))

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def write(self, relative, data):
        return self.fixture.write(self.src / relative, data)

    def archive(self, files):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as stream:
            for name, data in files.items():
                entry = tarfile.TarInfo("package/" + name)
                entry.size = len(data)
                stream.addfile(entry, io.BytesIO(data))
        return output.getvalue()

    def cached(self, key):
        return self.downloads / URLS[key].rsplit("/", 1)[-1]

    def install(self, **kwargs):
        return installer.install(self.work, "arm64", self.downloads, repo=self.repo, **kwargs)

    def inspect(self, host="x64", **kwargs):
        return prepare.prepare_linux_typescript(self.src, host_arch=host, repo=self.repo, **kwargs)

    def populate_tools(self, host="x64"):
        for name, data in self.modules.items():
            self.write(generators.ESBUILD_MODULE + "/" + name, data)
        self.write(generators.ESBUILD_BINARY, self.binaries[host]).chmod(0o755)

    def populate_repaired_sources(self, host="x64"):
        for name, before, after, metadata in generators.source_plan(self.src, pins=generators.PINS155)[1]:
            self.write(name, after)
        path = self.src / prepare.TYPESCRIPT_WRAPPER
        original = path.read_bytes()
        start = original.index(b"def GetBinaryPath():\n")
        end = original.index(b"def RunTypeScriptRaw(cmd_parts, stdout=None):\n")
        getter = f'''def GetBinaryPath():
    return os_path.normpath(os_path.join(os_path.dirname(__file__), '..', '..',
                                        'node_modules', 'typescript', 'lib', 'tsc.js'))


def GetNodePath():
    return os_path.normpath(os_path.join(os_path.dirname(__file__), '..', '..',
                                        '..', '..', 'node', 'linux',
                                        'node-linux-{host}', 'bin', 'node'))


'''.encode()
        repaired = original[:start] + getter + original[end:].replace(
            b"cmd = [GetBinaryPath()] + cmd_parts", b"cmd = [GetNodePath(), GetBinaryPath()] + cmd_parts")
        self.assertEqual(generators.sha256(repaired), generators.TYPESCRIPT155_REPAIRED[host])
        path.write_bytes(repaired)

    def snapshot(self):
        return {path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.src.rglob("*") if path.is_file()}

    def probe(self, command, **kwargs):
        output = "Version 6.0.2\n" if str(command[1]).endswith("/lib/tsc.js") else (
            "0.28.2\n" if command[1] == "--version" else "")
        return subprocess.CompletedProcess(command, 0, output)

    def test_ci_install_downloads_exact_host_urls_and_forwards_full_identity(self):
        shutil.rmtree(self.downloads)
        for host in ("x64", "arm64"):
            with self.subTest(host=host), \
                    mock.patch.object(prepare, "host_identity", return_value=("linux", host)), \
                    mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True), \
                    mock.patch.object(installer, "fetch_archive", side_effect=lambda url, digest:
                                      self.archives[next(key for key in URLS if URLS[key] == url)]) as fetch, \
                    mock.patch.object(generators, "install_esbuild", return_value={"fixture": host}) as provision, \
                    mock.patch.object(generators, "prepare_esbuild", wraps=generators.prepare_esbuild) as inspect, \
                    mock.patch.object(prepare.subprocess, "run") as run:
                before = self.snapshot()
                result = self.install(download=True)
                self.assertEqual(result["status"], "installed")
                self.assertEqual(fetch.call_args_list, [mock.call(*generators.ARCHIVES155[key])
                                                        for key in ("module", host)])
                provision.assert_called_once_with(self.src, host_arch=host,
                    module_archive=self.cached("module"), native_archive=self.cached(host), pins=generators.PINS155)
                self.assertEqual(len(inspect.call_args_list), 2)
                for call in inspect.call_args_list:
                    self.assertEqual(call.kwargs["pins"], generators.PINS155)
                self.assertEqual(result["source_identity"], json.loads((self.src / restore.MARKER).read_text())["identity"])
                self.assertEqual(before, self.snapshot())
                run.assert_not_called()
                for key in ("module", host):
                    self.assertEqual(self.cached(key).read_bytes(), self.archives[key])
                    self.assertEqual(self.cached(key).stat().st_nlink, 1)
            shutil.rmtree(self.downloads)

    def test_offline_install_and_repair_with_real_publication(self):
        protected = self.snapshot()
        with mock.patch.object(prepare.subprocess, "run") as run:
            result = self.install()
        self.assertEqual(result["esbuild"]["version"], "0.28.2")
        run.assert_not_called()
        for path, identity in protected.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), identity)
        with mock.patch.object(prepare.subprocess, "run", side_effect=self.probe) as run:
            result = self.inspect(repair=True)
        self.assertEqual(run.call_count, 3)
        self.assertFalse(result["repair_needed"])
        self.assertEqual(len(result["esbuild"]["sources"]), 7)
        self.assertEqual(result["sha256"], generators.TYPESCRIPT155_REPAIRED["x64"])

    def test_prepare_finish_uses155_profile_for_both_hosts_and_all_probes(self):
        for host in ("x64", "arm64"):
            self.populate_tools(host)
            self.populate_repaired_sources(host)
            before = self.snapshot()
            with self.subTest(host=host), \
                    mock.patch.object(generators, "prepare_esbuild", wraps=generators.prepare_esbuild) as esbuild, \
                    mock.patch.object(prepare.subprocess, "run", side_effect=self.probe) as run:
                result = self.inspect(host, repair=True)
            self.assertFalse(result["repair_needed"])
            self.assertEqual(result["esbuild"]["binary"]["host_arch"], host)
            self.assertEqual(len(result["esbuild"]["sources"]), 7)
            self.assertEqual([call.kwargs["pins"] for call in esbuild.call_args_list], [generators.PINS155] * 2)
            self.assertTrue(esbuild.call_args_list[1].kwargs["repair"])
            self.assertEqual(run.call_count, 3)
            self.assertIn('e.version!=="0.28.2"', run.call_args_list[-1].args[0][2])
            self.assertEqual(run.call_args_list[-1].args[0][0],
                             str(self.src / prepare.tool_paths("linux", host)["node"]))
            self.assertEqual(before, self.snapshot())

    def test_prepare_wrapper_repair_passes_exact155_bytes_and_publication_hook(self):
        for host in ("x64", "arm64"):
            self.populate_tools(host)
            self.populate_repaired_sources(host)
            wrapper = self.src / prepare.TYPESCRIPT_WRAPPER
            repaired = wrapper.read_bytes()
            portable = SCRIPTS["third_party/typescript/typescript.py"].encode()
            wrapper.write_bytes(portable)
            hook = mock.Mock()
            before = self.snapshot()
            with self.subTest(host=host), \
                    mock.patch.object(generators, "atomic_wrapper_replace") as publish, \
                    mock.patch.object(prepare.subprocess, "run", side_effect=self.probe):
                result = self.inspect(host, repair=True, before_publish=hook)
            publish.assert_called_once_with(self.src, portable, repaired, before_publish=hook)
            self.assertEqual(result["sha256"], generators.TYPESCRIPT155_REPAIRED[host])
            self.assertEqual(before, self.snapshot())

    def test_resume_is_read_only_without_cache_or_network(self):
        self.populate_tools()
        before = self.snapshot()
        shutil.rmtree(self.downloads)
        with mock.patch.object(prepare.subprocess, "run") as run:
            result = self.install(download=True)
        self.assertEqual(result["status"], "already_installed")
        self.assertEqual(before, self.snapshot())
        self.network.assert_not_called()
        run.assert_not_called()
        self.assertFalse(self.downloads.exists())

    def test_unknown_identity_keys_fail_before_archive_access(self):
        for key in PIN_KEYS:
            pins = dict(zip(PIN_KEYS, generators.PINS155))
            pins[key] = "unknown"
            with self.subTest(key=key), mock.patch.object(prepare, "load_pins", return_value=pins), \
                    mock.patch.object(installer, "ensure_archive") as archive, self.assertRaisesRegex(ValueError, "pins"):
                self.install(download=True)
            archive.assert_not_called()

    def test_receipt_fields_are_validated_before_archive_access(self):
        path = self.src / restore.MARKER
        original = path.read_bytes()
        receipt = json.loads(original)
        changed_identity = dict(receipt["identity"], chromium_version=generators.PINS[0])
        for key, value in (("identity", changed_identity), ("manifest", {}), ("owner", "unknown"), ("arch", "x64")):
            path.write_text(json.dumps(dict(receipt, **{key: value})))
            with self.subTest(key=key), mock.patch.object(installer, "ensure_archive") as archive, \
                    self.assertRaises(restore.Miss):
                self.install(download=True)
            archive.assert_not_called()
        path.write_bytes(original)

    def test_changed_receipt_or_source_during_download_blocks_publication(self):
        self.cached("x64").unlink()
        for relative in (restore.MARKER, generators.DEVTOOLS + "/scripts/build/ninja/bundle.gni"):
            path = self.src / relative
            original = path.read_bytes()
            def fetch(url, digest):
                path.write_bytes(original + (b" " if relative == restore.MARKER else b"unknown"))
                return self.archives["x64"]
            with self.subTest(relative=relative), mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), \
                    mock.patch.object(installer, "fetch_archive", side_effect=fetch), \
                    mock.patch.object(generators, "install_esbuild") as provision, self.assertRaises(ValueError):
                self.install(download=True)
            provision.assert_not_called()
            self.assertFalse((self.src / generators.ESBUILD_MODULE).exists())
            path.write_bytes(original)
            self.cached("x64").unlink()

    def test_changed_wrapper_tree_lock_sources_module_and_binary_are_not_repaired(self):
        self.populate_tools()
        paths = [prepare.TYPESCRIPT_WRAPPER, prepare.TYPESCRIPT_PACKAGE + "/lib/_tsc.js", generators.LOCK,
                 generators.ESBUILD_MODULE + "/lib/main.js", generators.ESBUILD_BINARY,
                 *(generators.DEVTOOLS + "/" + name for name in generators.SOURCE155_REPAIRS)]
        for relative in paths:
            path = self.src / relative
            original = path.read_bytes()
            path.write_bytes(original + b"unknown")
            with self.subTest(relative=relative), mock.patch.object(prepare.subprocess, "run") as run, \
                    self.assertRaises(ValueError):
                self.inspect(repair=True)
            run.assert_not_called()
            self.assertEqual(path.read_bytes(), original + b"unknown")
            path.write_bytes(original)
        self.write(prepare.TYPESCRIPT_PACKAGE + "/extra.js", b"unknown")
        with self.assertRaisesRegex(ValueError, "bytes/inventory"):
            self.inspect()

    def test_native_elf_architecture_is_checked_even_with_matching_digest(self):
        self.populate_tools()
        for host in ("x64", "arm64"):
            wrong = "arm64" if host == "x64" else "x64"
            self.write(generators.ESBUILD_BINARY, self.binaries[wrong]).chmod(0o755)
            with self.subTest(host=host), \
                    mock.patch.object(generators, "BINARY155_HASHES", {host: generators.sha256(self.binaries[wrong])}), \
                    mock.patch.object(prepare.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "header/mode"):
                self.inspect(host, repair=True)
            run.assert_not_called()

    def test_native_node_architecture_and_esbuild_version_probes_remain_strict(self):
        self.populate_tools()
        self.populate_repaired_sources()
        node = self.src / prepare.tool_paths("linux", "x64")["node"]
        self.fixture.binary(node, "linux", "arm64")
        with mock.patch.object(prepare.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "native host"):
            self.inspect(repair=True)
        run.assert_not_called()
        self.fixture.binary(node, "linux", "x64")
        for failure in ("version", "module"):
            def probe(command, **kwargs):
                if failure == "version" and command[1] == "--version":
                    return subprocess.CompletedProcess(command, 0, "0.25.1\n")
                if failure == "module" and command[1] == "-e":
                    return subprocess.CompletedProcess(command, 1, "module mismatch")
                return self.probe(command, **kwargs)
            before = self.snapshot()
            with self.subTest(failure=failure), mock.patch.object(prepare.subprocess, "run", side_effect=probe), \
                    self.assertRaisesRegex(ValueError, "esbuild .*probe failed"):
                self.inspect(repair=True)
            self.assertEqual(before, self.snapshot())

    def test_unknown_or_154_archives_are_rejected_before_publication(self):
        for key in ("module", "x64"):
            path = self.cached(key)
            original = path.read_bytes()
            path.write_bytes(b"unverified archive")
            with self.subTest(key=key), mock.patch.object(generators, "install_esbuild") as provision, \
                    self.assertRaisesRegex(ValueError, "SHA256"):
                self.install()
            provision.assert_not_called()
            path.write_bytes(original)
        for key in self.archives:
            with self.assertRaisesRegex(ValueError, "SHA256"):
                generators.archive_files(self.cached(key), key)
            files = generators.archive_files(self.cached(key), key, pins=generators.PINS155)
            self.assertEqual(files, self.modules if key == "module" else {
                "bin/esbuild": self.binaries[key], "package.json": b"{}", "README.md": b"independent fixture"})
        self.network.assert_not_called()

    def test_archive_selection_defaults_to154_and_rejects_unverified_pins_digest_and_url(self):
        with mock.patch.object(installer, "archive_identity", return_value=True) as identity:
            path = installer.ensure_archive(self.downloads, "module", download=False)
        self.assertEqual(path.name, "esbuild-0.25.1.tgz")
        identity.assert_called_once_with(path, generators.ARCHIVES["module"][1])
        for pins in ((), list(generators.PINS155), ("155.0.8059.40", *generators.PINS155[1:]),
                     (generators.PINS155[0], generators.PINS[1], generators.PINS155[2])):
            with self.subTest(pins=pins), self.assertRaisesRegex(ValueError, "pins"):
                installer.ensure_archive(self.downloads, "module", download=True, pins=pins)
        for digest in (None, "", "0" * 63, "g" * 64):
            with mock.patch.dict(generators.ARCHIVES155, module=(URLS["module"], digest)), \
                    self.assertRaisesRegex(ValueError, "SHA256"):
                installer.ensure_archive(self.downloads, "module", download=True, pins=generators.PINS155)
        for url in (URLS["module"].replace("0.28.2", "0.25.1"), URLS["module"] + "?token=bad",
                    URLS["module"].replace("https:", "http:"),
                    URLS["module"].replace("registry.npmjs.org", "user:password@registry.npmjs.org")):
            with mock.patch.dict(generators.ARCHIVES155, module=(url, generators.ARCHIVES155["module"][1])), \
                    self.assertRaisesRegex(ValueError, "endpoint"):
                installer.ensure_archive(self.downloads, "module", download=True, pins=generators.PINS155)
        self.network.assert_not_called()


class InstallLinux15579GeneratorsTest(InstallLinux155GeneratorsTest):
    def setUp(self):
        self.patch(generators, "PINS155", generators.PINS15579)
        super().setUp()


if __name__ == "__main__":
    unittest.main()
