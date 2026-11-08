"""Pinned portablelinux wrapper repair and restored-stage generator validation."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tools import prepare_restored_build as prepare


FIXTURE = json.loads((Path(__file__).parent / "fixtures/devtools_typescript153.json").read_text())
PORTABLE = FIXTURE["wrapper"].replace('def GetBinaryPath():\n',
                                       'def GetBinaryPath():\n    return "/usr/bin/tsc"\n').encode()


LINUX153_PINS = ("153.0.8010.36", "dd8fb9b5c837982faf41ba58cd30a5664e77c329",
                 "a5ffa5e4a9fb722b97a5cf7966e29450a150c3dd")


def install_linux_repo_fixture(repo, pins=LINUX153_PINS, *, historical_support=False):
    version, core, linux = pins
    values = {"ChromiumVersion": version, "UngoogledVersion": version + "-1", "UngoogledCommit": core,
              "LinuxChromiumVersion": version, "LinuxUngoogledVersion": version + "-1",
              "LinuxUngoogledCommit": core, "MacOSChromiumVersion": version,
              "MacOSUngoogledVersion": version + "-1", "MacOSUngoogledCommit": "b" * 40,
              "WindowsChromiumVersion": version, "WindowsUngoogledVersion": version + "-1",
              "WindowsUngoogledCommit": "c" * 40, "UngoogledLinuxVersion": version + "-1",
              "UngoogledLinuxCommit": linux, "UngoogledMacOSVersion": version + "-1",
              "UngoogledMacOSCommit": "b" * 40, "UngoogledWindowsVersion": version + "-1",
              "UngoogledWindowsCommit": "c" * 40}
    sources = {}
    for index, (platform, head, repository, roots) in enumerate((
            ("linux", linux, "portablelinux", ["build/src"]),
            ("macos", "b" * 40, "macos", ["src"]),
            ("windows", "c" * 40, "windows", ["src", "build/src"])), 1):
        sources[platform] = {
            "chromium_version": version, "ungoogled_commit": {"linux": core, "macos": "b" * 40,
                "windows": "c" * 40}[platform],
            "repository": "ungoogled-software/ungoogled-chromium-" + repository,
            "repository_id": index, "head_sha": head, "head_branch": version + "-1",
            "event": "push", "workflow_path": ".github/workflows/" + (
                "build-x64.yml" if platform == "windows" else "build.yml"),
            "run_id": index, "source_roots": roots,
            "artifacts": {arch: {"id": index * 10 + number, "name": "fixture-" + arch,
                "size_in_bytes": 1, "digest": "sha256:" + str(index) * 64,
                "expires_at": "2099-01-01T00:00:00Z",
                "inner_archive": "fixture.zip" if platform == "windows" else "fixture.tar.zst"}
                for number, arch in enumerate(("x64",) if platform == "windows" else ("x64", "arm64"), 1)}}
    (repo / "build").mkdir(parents=True, exist_ok=True)
    for name in ("CHROMIUM_VERSION", "CHROMIUM_LINUX_VERSION", "CHROMIUM_MACOS_VERSION",
                 "CHROMIUM_WINDOWS_VERSION"):
        (repo / name).write_text(version + "\n")
    (repo / "build/ungoogled-revisions.psd1").write_text(
        "@{\n" + "".join(f'  {key} = "{value}"\n' for key, value in values.items()) + "}\n")
    (repo / "build/upstream-cache.json").write_text(json.dumps({
        "schema_version": 1, "chromium_version": version, "ungoogled_commit": core, "sources": sources}))
    if historical_support:
        historical = json.loads((Path(__file__).parent / "fixtures/windows155_repository.json").read_text())
        for relative, content in historical["files"].items():
            destination = repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        revisions = repo / "build/ungoogled-revisions.psd1"
        lines = revisions.read_text().splitlines(keepends=True)
        for index, line in enumerate(lines):
            key = line.strip().split(" = ", 1)[0]
            if key in ("LinuxChromiumVersion", "LinuxUngoogledVersion", "LinuxUngoogledCommit",
                       "UngoogledLinuxVersion", "UngoogledLinuxCommit"):
                lines[index] = f'  {key} = "{values[key]}"\n'
        revisions.write_text("".join(lines))
        (repo / "CHROMIUM_LINUX_VERSION").write_text(version + "\n")
        manifest = json.loads(historical["files"]["build/upstream-cache.json"])
        manifest["sources"]["linux"] = sources["linux"]
        (repo / "build/upstream-cache.json").write_text(json.dumps(manifest))
    return repo


def install_typescript_fixture(src):
    files = {prepare.TYPESCRIPT_WRAPPER: PORTABLE,
             prepare.TYPESCRIPT_PACKAGE + "/package.json": FIXTURE["package"],
             prepare.TYPESCRIPT_PACKAGE + "/bin/tsc": "#!/usr/bin/env node\nrequire('../lib/tsc.js');\n",
             prepare.TYPESCRIPT_PACKAGE + "/lib/tsc.js": FIXTURE["shim"],
             prepare.TYPESCRIPT_PACKAGE + "/lib/_tsc.js": "console.log('Version 6.0.2');\n"}
    for name in FIXTURE["libraries"]:
        references = FIXTURE["library_references"][name]
        files[prepare.TYPESCRIPT_PACKAGE + "/lib/" + name] = "// fixture declaration\n" + "".join(
            f'/// <reference lib="{reference}" />\n' for reference in references)
    for relative, content in files.items():
        path = src / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        if relative.endswith("/bin/tsc"):
            path.chmod(0o755)


class LinuxTypeScriptTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="typescript restored ")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.src = self.work / "src"
        self.repo = install_linux_repo_fixture(self.work / "repo")
        install_typescript_fixture(self.src)
        self.wrapper = self.src / prepare.TYPESCRIPT_WRAPPER
        self.package = self.src / prepare.TYPESCRIPT_PACKAGE

    def repair(self, host="x64", **kwargs):
        with mock.patch.object(prepare, "binary_architectures", return_value={host}), \
                mock.patch.object(prepare.os, "access", return_value=True), \
                mock.patch.object(prepare.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, "Version 6.0.2\n")) as run:
            result = prepare.prepare_linux_typescript(self.src, repo=self.repo, host_arch=host, repair=True, **kwargs)
        return result, run

    def load_wrapper(self):
        spec = importlib.util.spec_from_file_location("fixture_typescript", self.wrapper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_exact_upstream_fixture_and_read_only_inspection(self):
        self.assertEqual(hashlib.sha256(FIXTURE["wrapper"].encode()).hexdigest(),
                         "6f1e5c0c002be9756e9cb0e3b4700aa255f5fd235b7fe4c9ca0a259a340968d4")
        self.assertEqual(hashlib.sha256(PORTABLE).hexdigest(), prepare.TYPESCRIPT_PORTABLE)
        before = self.wrapper.stat()
        with mock.patch.object(prepare.subprocess, "run") as run:
            result = prepare.prepare_linux_typescript(self.src, repo=self.repo, host_arch="x64")
        run.assert_not_called()
        self.assertTrue(result["repair_needed"])
        self.assertEqual(result["version"], "6.0.2")
        self.assertEqual(self.wrapper.read_bytes(), PORTABLE)
        self.assertEqual(self.wrapper.stat().st_mtime_ns, before.st_mtime_ns)

    def test_repair_uses_host_node_and_local_pinned_package_and_is_idempotent(self):
        for host in ("x64", "arm64", "x64"):
            with self.subTest(host=host):
                result, run = self.repair(host)
                self.assertFalse(result["repair_needed"])
                self.assertEqual(hashlib.sha256(self.wrapper.read_bytes()).hexdigest(),
                                 prepare.TYPESCRIPT_REPAIRED[host])
                module = self.load_wrapper()
                self.assertEqual(Path(module.GetBinaryPath()), self.package / "lib/tsc.js")
                node = self.src / prepare.tool_paths("linux", host)["node"]
                self.assertEqual(Path(module.GetNodePath()), node)
                self.assertEqual(run.call_args.args[0], [str(node), str(self.package / "lib/tsc.js"), "--version"])
                before = self.wrapper.stat().st_mtime_ns
                self.repair(host)
                self.assertEqual(self.wrapper.stat().st_mtime_ns, before)
                self.assertNotIn(b"/usr/bin/tsc", self.wrapper.read_bytes())

    def test_unknown_canonical_partial_and_duplicate_wrappers_fail_without_writes_or_execution(self):
        for content in (FIXTURE["wrapper"].encode(), PORTABLE + b"# unexpected\n",
                        PORTABLE.replace(b'/usr/bin/tsc', b'/usr/local/bin/tsc'),
                        PORTABLE.replace(b'    return "/usr/bin/tsc"\n', b'    return "/usr/bin/tsc"\n' * 2),
                        b"def GetBinaryPath():\n    return '/usr/bin/tsc'\n"):
            with self.subTest(content=hashlib.sha256(content).hexdigest()):
                self.wrapper.write_bytes(content)
                with mock.patch.object(prepare.subprocess, "run") as run, \
                        self.assertRaisesRegex(ValueError, "unknown restored"):
                    prepare.prepare_linux_typescript(self.src, repo=self.repo, host_arch="x64", repair=True)
                run.assert_not_called()
                self.assertEqual(self.wrapper.read_bytes(), content)

    def test_wrong_pins_fail_closed(self):
        for pins in (("153.0.8010.37", *LINUX153_PINS[1:]),
                     (LINUX153_PINS[0], "d" * 40, LINUX153_PINS[2]),
                     (*LINUX153_PINS[:2], "e" * 40)):
            install_linux_repo_fixture(self.repo, pins)
            with self.subTest(pins=pins), mock.patch.object(prepare.subprocess, "run") as run, \
                    self.assertRaisesRegex(ValueError, "pins/host"):
                prepare.prepare_linux_typescript(self.src, repo=self.repo, host_arch="x64", repair=True)
            run.assert_not_called()
        self.assertEqual(self.wrapper.read_bytes(), PORTABLE)

    def test_linux154_pins_reject_legacy_and_repaired153_wrappers(self):
        from tools import linux_restored_generators as linux154
        current = install_linux_repo_fixture(self.work / "repo154", linux154.PINS)
        wrappers = [PORTABLE]
        for host in ("x64", "arm64"):
            self.repair(host)
            wrappers.append(self.wrapper.read_bytes())
        for payload in wrappers:
            self.wrapper.write_bytes(payload)
            with mock.patch.object(prepare.subprocess, "run") as run, \
                    self.assertRaisesRegex(ValueError, "unknown restored"):
                prepare.prepare_linux_typescript(self.src, repo=current, host_arch="x64", repair=True)
            run.assert_not_called()
            self.assertEqual(self.wrapper.read_bytes(), payload)

    def test_missing_empty_package_inputs_and_wrong_version_fail_before_repair(self):
        paths = [self.wrapper, *(self.package / name for name in (
            "package.json", "lib/tsc.js", "lib/_tsc.js", "lib/lib.d.ts", "lib/lib.es5.d.ts", "lib/lib.dom.d.ts"))]
        for path in paths:
            original = path.read_bytes()
            for empty in (False, True):
                with self.subTest(path=path, empty=empty):
                    path.write_bytes(b"") if empty else path.unlink()
                    with mock.patch.object(prepare.subprocess, "run") as run, self.assertRaises((ValueError, OSError)):
                        prepare.prepare_linux_typescript(self.src, repo=self.repo, host_arch="x64", repair=True)
                    run.assert_not_called()
                    path.write_bytes(original)
                    self.assertEqual(self.wrapper.read_bytes(), PORTABLE)
        metadata = self.package / "package.json"
        metadata.write_text(FIXTURE["package"].replace('"6.0.2"', '"6.0.3"'))
        with self.assertRaisesRegex(ValueError, "package metadata"):
            self.repair()
        self.assertEqual(self.wrapper.read_bytes(), PORTABLE)

    def test_complete_pinned_library_inventory_and_reference_graph(self):
        libraries = FIXTURE["libraries"]
        self.assertEqual(len(libraries), 108)
        self.assertEqual(len(set(libraries)), 108)
        self.assertEqual(hashlib.sha256("\n".join(sorted(libraries)).encode()).hexdigest(),
                         "8689edceb234a3584fe0fc46458e4278fbe3fa9fee93a8f17001a8b4ed6f394f")
        self.assertEqual(sorted(prepare.TYPESCRIPT_LIBRARIES), libraries)
        self.assertIn("decorators", FIXTURE["library_references"]["lib.es5.d.ts"])
        for name, references in FIXTURE["library_references"].items():
            self.assertIn(name, libraries)
            for reference in references:
                self.assertIn(f"lib.{reference}.d.ts", libraries)

    def test_each_missing_or_empty_standard_library_fails_without_probe_or_repair(self):
        for name in FIXTURE["libraries"]:
            path = self.package / "lib" / name
            original = path.read_bytes()
            for empty in (False, True):
                with self.subTest(name=name, empty=empty):
                    path.write_bytes(b"") if empty else path.unlink()
                    try:
                        with mock.patch.object(prepare.subprocess, "run") as run, \
                                self.assertRaises((ValueError, OSError)) as error:
                            prepare.prepare_linux_typescript(self.src, repo=self.repo, host_arch="x64", repair=True)
                        self.assertIn(name, str(error.exception))
                        run.assert_not_called()
                        self.assertEqual(self.wrapper.read_bytes(), PORTABLE)
                    finally:
                        path.write_bytes(original)

    def test_symlink_hardlink_and_linked_parent_are_rejected(self):
        for relative in (prepare.TYPESCRIPT_WRAPPER, prepare.TYPESCRIPT_PACKAGE + "/lib/_tsc.js",
                         prepare.TYPESCRIPT_PACKAGE + "/lib/lib.decorators.d.ts",
                         prepare.TYPESCRIPT_PACKAGE + "/lib/lib.es2023.d.ts"):
            path = self.src / relative
            original = path.read_bytes()
            target = self.work / "outside"
            target.write_bytes(original)
            for hard in (False, True):
                with self.subTest(relative=relative, hard=hard):
                    path.unlink()
                    os.link(target, path) if hard else path.symlink_to(target)
                    with self.assertRaises(ValueError):
                        prepare.prepare_linux_typescript(self.src, repo=self.repo, host_arch="x64", repair=True)
                    self.assertEqual(target.read_bytes(), original)
                    path.unlink()
                    path.write_bytes(original)
        lib = self.package / "lib"
        lib.rename(self.work / "outside-lib")
        lib.symlink_to(self.work / "outside-lib", target_is_directory=True)
        with self.assertRaises(ValueError):
            self.repair()
        self.assertEqual(self.wrapper.read_bytes(), PORTABLE)

    def test_wrong_host_node_and_failed_probes_do_not_publish_wrapper(self):
        with mock.patch.object(prepare, "binary_architectures", return_value={"arm64"}), \
                mock.patch.object(prepare.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "native host"):
            prepare.prepare_linux_typescript(self.src, repo=self.repo, host_arch="x64", repair=True)
        run.assert_not_called()
        for result in (subprocess.CompletedProcess([], 1, "missing module"),
                       subprocess.CompletedProcess([], 0, "Version 7.0.2"),
                       FileNotFoundError("node missing"), subprocess.TimeoutExpired("node", 30)):
            with self.subTest(result=result), mock.patch.object(prepare, "binary_architectures", return_value={"x64"}), \
                    mock.patch.object(prepare.os, "access", return_value=True), \
                    mock.patch.object(prepare.subprocess, "run", side_effect=(
                        result if isinstance(result, Exception) else lambda *args, **kwargs: result)), \
                    self.assertRaises((ValueError, OSError, subprocess.SubprocessError)):
                prepare.prepare_linux_typescript(self.src, repo=self.repo, host_arch="x64", repair=True)
            self.assertEqual(self.wrapper.read_bytes(), PORTABLE)

    def test_raw_and_checked_calls_preserve_argv_streams_exit_and_cwd(self):
        self.repair()
        module = self.load_wrapper()
        arguments = ["--project", "directory with spaces/配置.json", "--pretty", "false"]
        process = mock.Mock(returncode=2)
        process.communicate.return_value = ("stdout diagnostic", "stderr diagnostic")
        with mock.patch.object(module.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(module.RunTypeScriptRaw(arguments), (2, "stdout diagnostic", "stderr diagnostic"))
            command = [module.GetNodePath(), module.GetBinaryPath(), *arguments]
            self.assertEqual(popen.call_args.args[0], command)
            self.assertEqual(popen.call_args.kwargs["cwd"], os.getcwd())
            self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
            with self.assertRaisesRegex(RuntimeError, "exit=2") as error:
                module.RunTypeScript(arguments)
            self.assertIn(module.GetNodePath(), str(error.exception))
            self.assertIn("stderr diagnostic", str(error.exception))
            self.assertIn("stdout diagnostic", str(error.exception))
            process.returncode = 0
            self.assertEqual(module.RunTypeScript(arguments), "stdout diagnostic")
        self.assertEqual(arguments, ["--project", "directory with spaces/配置.json", "--pretty", "false"])

    @unittest.skipUnless(os.environ.get("CHROMIX_TEST_NODE"), "set CHROMIX_TEST_NODE for a real Node subprocess")
    def test_real_node_subprocess_uses_pinned_entry_not_path_tsc(self):
        host = prepare.host_identity()[1]
        node = self.src / prepare.tool_paths("linux", host)["node"]
        node.parent.mkdir(parents=True)
        node.symlink_to(Path(os.environ["CHROMIX_TEST_NODE"]).resolve())
        (self.package / "lib/_tsc.js").write_text('''const args = process.argv.slice(2);
if (args[0] === '--version') { console.log('Version 6.0.2'); }
else { console.log(JSON.stringify({args, cwd: process.cwd()})); console.error('diagnostic'); process.exit(2); }
''')
        prepare.prepare_linux_typescript(self.src, repo=self.repo, host_arch=host, repair=True)
        module = self.load_wrapper()
        with mock.patch.dict(os.environ, {"PATH": ""}):
            code, stdout, stderr = module.RunTypeScriptRaw(["--project", "space dir/配置.json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout), {"args": ["--project", "space dir/配置.json"], "cwd": os.getcwd()})
        self.assertEqual(stderr.strip(), "diagnostic")

    @unittest.skipUnless(os.environ.get("CHROMIX_TEST_NODE") and os.environ.get("CHROMIX_TEST_TYPESCRIPT"),
                         "set real Node and local TypeScript package paths for a compile smoke")
    def test_real_typescript_compile_and_type_error(self):
        shutil.rmtree(self.package)
        shutil.copytree(os.environ["CHROMIX_TEST_TYPESCRIPT"], self.package)
        host = prepare.host_identity()[1]
        node = self.src / prepare.tool_paths("linux", host)["node"]
        node.parent.mkdir(parents=True)
        node.symlink_to(Path(os.environ["CHROMIX_TEST_NODE"]).resolve())
        prepare.prepare_linux_typescript(self.src, repo=self.repo, host_arch=host, repair=True)
        module = self.load_wrapper()
        source = self.work / "with spaces.ts"
        source.write_text("export const value: number = 42;\n")
        # Avoid ambient @types from the surrounding checkout.
        args = [str(source), "--outDir", str(self.work / "compiled"), "--ignoreConfig",
                "--typeRoots", str(self.work / "empty-types")]
        with mock.patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(module.RunTypeScriptRaw(args), (0, "", ""))
            self.assertIn("42", (self.work / "compiled/with spaces.js").read_text())
            source.write_text("export const value: number = 'not a number';\n")
            code, stdout, stderr = module.RunTypeScriptRaw(args)
        self.assertNotEqual(code, 0)
        self.assertIn("TS2322", stdout)
        self.assertEqual(stderr, "")


class LinuxTypeScriptRestoredChainTest(unittest.TestCase):
    def setUp(self):
        from tools.tests.test_prepare_restored_build import PrepareRestoredBuildTest
        self.fixture = PrepareRestoredBuildTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.fixture("linux", "arm64", host_arch="x64", linux_pins=LINUX153_PINS)

    def test_restore_inspect_repair_resume_and_compiler_drift_invalidate_only_generators(self):
        fixture = self.fixture
        wrapper = fixture.src / prepare.TYPESCRIPT_WRAPPER
        protected = {path: path.read_bytes() for path in (
            fixture.src / ".chromix-upstream-restored.json", *(fixture.out / name for name in prepare.METADATA))}
        with fixture.native_context("linux", "x64"):
            inspection = fixture.prepare(fixture.work, "linux", "arm64", phase="inspect")
            self.assertTrue(inspection["generator_fingerprint"]["typescript"]["repair_needed"])
            self.assertEqual(wrapper.read_bytes(), PORTABLE)
            first = fixture.prepare(fixture.work, "linux", "arm64")
            self.assertTrue(first["ready_for_gn"])
            independent = fixture.object("independent.o")
            dependent = fixture.object("generated.o")
            generated = fixture.write(fixture.out / "gen/ts-output.h", "generated")
            fixture.deps({"obj/independent.o": ["../../include/a.h"], "obj/generated.o": ["gen/ts-output.h"]})
            fixture.write(fixture.out / ".ninja_log", "# ninja log v5\n0\t1\t1\tgen/ts-output.h\tabc\n")
            resumed = fixture.prepare(fixture.work, "linux", "arm64")
            self.assertEqual(resumed["counters"]["generator_rechecks"], 0)
            self.assertTrue(generated.exists())
            for changed in ("lib/_tsc.js", "lib/lib.es5.d.ts"):
                with self.subTest(changed=changed):
                    path = fixture.src / prepare.TYPESCRIPT_PACKAGE / changed
                    path.write_bytes(path.read_bytes() + b"// changed compiler input\n")
                    result = fixture.prepare(fixture.work, "linux", "arm64")
                    self.assertEqual(result["counters"]["generator_rechecks"], 1)
                    self.assertEqual(result["counters"]["toolchain_invalidated_outputs"], 0)
                    self.assertTrue(independent.exists())
                    self.assertFalse(generated.exists())
                    self.assertFalse(dependent.exists())
                    fixture.write(generated, "generated")
                    fixture.write(dependent, "object")
            wrapper.write_bytes(PORTABLE)
            repaired_again = fixture.prepare(fixture.work, "linux", "arm64")
            self.assertEqual(repaired_again["counters"]["generator_rechecks"], 1)
            self.assertEqual(hashlib.sha256(wrapper.read_bytes()).hexdigest(), prepare.TYPESCRIPT_REPAIRED["x64"])
        for path, payload in protected.items():
            if path.name not in (".ninja_log", ".ninja_deps"):
                self.assertEqual(path.read_bytes(), payload)

    def test_legacy_stage_marker_without_typescript_fingerprint_rechecks_generators(self):
        fixture = self.fixture
        with fixture.native_context("linux", "x64"):
            fixture.prepare(fixture.work, "linux", "arm64")
            marker = fixture.src / prepare.MARKER
            old = json.loads(marker.read_text())
            del old["generator_fingerprint"]["typescript"]
            marker.write_text(json.dumps(old))
            generated = fixture.write(fixture.out / "gen/legacy.js", "legacy output")
            fixture.write(fixture.out / ".ninja_log", "# ninja log v5\n0\t1\t1\tgen/legacy.js\tabc\n")
            independent = fixture.object("keep.o")
            fixture.deps({"obj/keep.o": ["../../include/a.h"]})
            result = fixture.prepare(fixture.work, "linux", "arm64")
        self.assertEqual(result["counters"]["generator_rechecks"], 1)
        self.assertEqual(result["counters"]["toolchain_invalidated_outputs"], 0)
        self.assertFalse(generated.exists())
        self.assertTrue(independent.exists())

    def test_invalid_restore_receipt_blocks_wrapper_repair_before_probes(self):
        fixture = self.fixture
        receipt = fixture.src / ".chromix-upstream-restored.json"
        data = json.loads(receipt.read_text())
        data["identity"]["chromium_version"] = "152.0.7977.82"
        receipt.write_text(json.dumps(data))
        with fixture.native_context("linux", "x64"), mock.patch.object(prepare.subprocess, "run") as run, \
                self.assertRaises(prepare.Miss):
            fixture.prepare(fixture.work, "linux", "arm64")
        run.assert_not_called()
        self.assertEqual((fixture.src / prepare.TYPESCRIPT_WRAPPER).read_bytes(), PORTABLE)
        report = json.loads((fixture.work / "upstream-cache-preparation.json").read_text())
        self.assertFalse(report["ready_for_gn"])
        self.assertEqual(report["operation"], "verify_restored")

    def test_later_stage_probe_failure_does_not_publish_ready_marker(self):
        fixture = self.fixture
        with fixture.native_context("linux", "x64"):
            fixture.prepare(fixture.work, "linux", "arm64")
            marker = (fixture.src / prepare.MARKER).read_bytes()
            obj = fixture.object("keep.o")
            run = prepare.subprocess.run
            def fail_tsc(command, **kwargs):
                if str(command[1]).endswith("/lib/tsc.js"):
                    return subprocess.CompletedProcess(command, 1, "missing compiler module")
                return run(command, **kwargs)
            with mock.patch.object(prepare.subprocess, "run", side_effect=fail_tsc), \
                    self.assertRaisesRegex(ValueError, "TypeScript probe failed"):
                fixture.prepare(fixture.work, "linux", "arm64")
        report = json.loads((fixture.work / "upstream-cache-preparation.json").read_text())
        self.assertFalse(report["ready_for_gn"])
        self.assertEqual(report["operation"], "prepare_linux_typescript")
        self.assertEqual((fixture.src / prepare.MARKER).read_bytes(), marker)
        self.assertTrue(obj.exists())

    def test_missing_libraries_block_first_finish_and_later_stage_before_invalidation(self):
        fixture = self.fixture
        marker = fixture.src / prepare.MARKER
        wrapper = fixture.src / prepare.TYPESCRIPT_WRAPPER
        with fixture.native_context("linux", "x64"):
            for first in (True, False):
                if not first:
                    self.assertTrue(fixture.prepare(fixture.work, "linux", "arm64")["ready_for_gn"])
                for name in ("lib.decorators.d.ts", "lib.es2023.d.ts"):
                    with self.subTest(first=first, name=name):
                        obj = fixture.object("keep.o")
                        generated = fixture.write(fixture.out / "gen/keep.js", "retained generated")
                        fixture.write(fixture.out / ".ninja_log", "# ninja log v5\n0\t1\t1\tgen/keep.js\tabc\n")
                        protected = {path: path.read_bytes() for path in (
                            wrapper, obj, generated, fixture.src / ".chromix-upstream-restored.json",
                            *(fixture.out / value for value in prepare.METADATA))}
                        old = marker.read_bytes() if marker.exists() else None
                        path = fixture.src / prepare.TYPESCRIPT_PACKAGE / "lib" / name
                        content = path.read_bytes()
                        path.unlink()
                        try:
                            with mock.patch.object(prepare.subprocess, "run", wraps=prepare.subprocess.run) as run, \
                                    self.assertRaises(FileNotFoundError) as error:
                                fixture.prepare(fixture.work, "linux", "arm64")
                            self.assertFalse(any(str(call.args[0][1]).endswith("/lib/tsc.js")
                                                 for call in run.call_args_list))
                            self.assertIn(name, str(error.exception))
                            report = json.loads((fixture.work / "upstream-cache-preparation.json").read_text())
                            self.assertFalse(report["ready_for_gn"])
                            self.assertEqual(report["operation"], "inspect_linux_typescript")
                            self.assertEqual(marker.read_bytes() if marker.exists() else None, old)
                            for target, original in protected.items():
                                self.assertEqual(target.read_bytes(), original)
                        finally:
                            path.write_bytes(content)

    @unittest.skipUnless(os.environ.get("CHROMIX_TEST_NODE") and os.environ.get("CHROMIX_TEST_TYPESCRIPT"),
                         "set real Node and local TypeScript package paths for completeness smoke")
    def test_real_missing_libraries_fail_first_finish_and_later_stage_despite_version_success(self):
        fixture = self.fixture
        package = fixture.src / prepare.TYPESCRIPT_PACKAGE
        shutil.rmtree(package)
        shutil.copytree(os.environ["CHROMIX_TEST_TYPESCRIPT"], package)
        self.assertEqual(sorted(path.name for path in (package / "lib").glob("lib*.d.ts")), FIXTURE["libraries"])
        host = prepare.host_identity()[1]
        self.assertEqual(host, "x64")
        node = fixture.src / prepare.tool_paths("linux", host)["node"]
        node.unlink()
        node.symlink_to(Path(os.environ["CHROMIX_TEST_NODE"]).resolve())
        source = fixture.write(fixture.work / "check.ts", "export const value: number = 42;\n")
        command = [str(node), str(package / "lib/tsc.js"), "--ignoreConfig", "--noEmit",
                   "--target", "es2023", "--lib", "es2023,dom", "--typeRoots",
                   str(fixture.work / "empty-types"), str(source)]
        env = dict(os.environ, PATH="", NODE_DISABLE_COMPILE_CACHE="1")
        real_run = subprocess.run
        marker = fixture.src / prepare.MARKER
        with fixture.native_context("linux", host):
            fake_run = prepare.subprocess.run
            def run(command, **kwargs):
                if Path(command[0]) == node:
                    return real_run(command, **kwargs)
                return fake_run(command, **kwargs)
            with mock.patch.object(prepare.subprocess, "run", side_effect=run):
                self.assertEqual(real_run(command, env=env, capture_output=True, text=True, timeout=30).returncode, 0)
                for first in (True, False):
                    if not first:
                        self.assertTrue(fixture.prepare(fixture.work, "linux", "arm64")["ready_for_gn"])
                    for name in ("lib.decorators.d.ts", "lib.es2023.d.ts"):
                        with self.subTest(first=first, name=name):
                            path = package / "lib" / name
                            content = path.read_bytes()
                            old = marker.read_bytes() if marker.exists() else None
                            before = (fixture.src / prepare.TYPESCRIPT_WRAPPER).read_bytes()
                            path.unlink()
                            try:
                                version = real_run([str(node), str(package / "lib/tsc.js"), "--version"],
                                                   env=env, capture_output=True, text=True, timeout=30)
                                self.assertEqual((version.returncode, version.stdout.strip()), (0, "Version 6.0.2"))
                                with self.assertRaises(FileNotFoundError) as error:
                                    fixture.prepare(fixture.work, "linux", "arm64")
                                self.assertIn(name, str(error.exception))
                                report = json.loads((fixture.work / "upstream-cache-preparation.json").read_text())
                                self.assertFalse(report["ready_for_gn"])
                                self.assertEqual(report["operation"], "inspect_linux_typescript")
                                self.assertEqual(marker.read_bytes() if marker.exists() else None, old)
                                self.assertEqual((fixture.src / prepare.TYPESCRIPT_WRAPPER).read_bytes(), before)
                                compiled = real_run(command, env=env, capture_output=True, text=True, timeout=30)
                                self.assertNotEqual(compiled.returncode, 0)
                                self.assertIn("TS6053", compiled.stdout)
                                self.assertIn(name, compiled.stdout)
                            finally:
                                path.write_bytes(content)
                # Exact endpoint changes made by the pinned ungoogled domain rules remain valid inputs.
                before = fixture.prepare(fixture.work, "linux", "arm64")
                library = package / "lib/lib.dom.d.ts"
                canonical = library.read_bytes().replace(b"m0z111a.qjz9zk", b"mozilla.org")
                substituted = canonical.replace(b"mozilla.org", b"m0z111a.qjz9zk")
                self.assertNotEqual(canonical, substituted)
                library.write_bytes(canonical)
                normal = fixture.prepare(fixture.work, "linux", "arm64")
                library.write_bytes(substituted)
                transformed = fixture.prepare(fixture.work, "linux", "arm64")
                self.assertTrue(before["ready_for_gn"] and normal["ready_for_gn"] and transformed["ready_for_gn"])
                self.assertNotEqual(normal["generator_fingerprint"]["typescript"]["package_sha256"],
                                    transformed["generator_fingerprint"]["typescript"]["package_sha256"])
                self.assertEqual(transformed["counters"]["generator_rechecks"], 1)
                self.assertEqual(real_run(command, env=env, capture_output=True, text=True, timeout=30).returncode, 0)

    def test_later_stage_tampering_fails_before_invalidation_and_records_failure(self):
        fixture = self.fixture
        with fixture.native_context("linux", "x64"):
            fixture.prepare(fixture.work, "linux", "arm64")
            marker = (fixture.src / prepare.MARKER).read_bytes()
            obj = fixture.object("keep.o")
            wrapper = fixture.src / prepare.TYPESCRIPT_WRAPPER
            wrapper.write_bytes(wrapper.read_bytes() + b"# unknown stage wrapper\n")
            with self.assertRaisesRegex(ValueError, "unknown restored"):
                fixture.prepare(fixture.work, "linux", "arm64")
        report = json.loads((fixture.work / "upstream-cache-preparation.json").read_text())
        self.assertFalse(report["ready_for_gn"])
        self.assertEqual(report["operation"], "inspect_linux_typescript")
        self.assertEqual((fixture.src / prepare.MARKER).read_bytes(), marker)
        self.assertTrue(obj.exists())


if __name__ == "__main__":
    unittest.main()
