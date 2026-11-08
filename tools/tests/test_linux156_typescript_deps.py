"""Real Chromium156 declaration target and strict relocated standard-library identity."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import linux_restored_generators as generators
from tools.tests.test_linux156_generators import FIXTURE, SOURCES
from tools.tests import test_linux156_generators as generator_tests

HELPER = "tools/typescript/validate_tsconfig.py"
LIBROOT = generators.DEVTOOLS + "/node_modules/typescript/lib"


class Linux156TypeScriptDepsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chromium156 ts deps ")
        self.addCleanup(temporary.cleanup)
        self.src = Path(temporary.name).resolve()
        for name, text in SOURCES.items():
            path = self.src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.out = self.src / "out/Default"
        self.out.mkdir(parents=True)
        self.changes = {name: after for name, _, after, _ in
                        generators.source_plan(self.src, pins=generators.PINS156)[1]}

    def helper(self):
        (self.src / HELPER).write_bytes(self.changes[HELPER])
        spec = importlib.util.spec_from_file_location("linux156_validate_tsconfig", self.src / HELPER)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "path", [str(self.src / "tools/typescript"), *sys.path]):
            spec.loader.exec_module(module)
        return module

    def test_standard_library_inventory_and_original_assertion_are_preserved(self):
        self.assertEqual(generators.LINUX156_TYPESCRIPT_LIBRARIES,
                         FIXTURE["typescript"]["standard_libraries_sha256"])
        self.assertEqual(len(generators.LINUX156_TYPESCRIPT_LIBRARIES), 108)
        self.assertEqual(hashlib.sha256(SOURCES[HELPER].encode()).hexdigest(),
                         "a898d158049f59bc18e84355bfeb54b11c1ab00781cc1939ed5b7b9736c14ff1")
        self.assertEqual(hashlib.sha256(self.changes[HELPER]).hexdigest(),
                         "f4b280e7b1776995645e9db53d331ba06cd430201dbdd717e7b64842a73c1032")
        original = SOURCES[HELPER].split("  exceptions_list = [", 1)[1].split("  ]", 1)[0]
        repaired = self.changes[HELPER].decode().split("  exceptions_list = [", 1)[1].split("  ]", 1)[0]
        self.assertEqual(original, repaired)
        self.assertIn("if not list_valid:\n    raise AssertionError(error_msg)", SOURCES["tools/typescript/ts_library.py"])

    def test_only_exact_standard_library_name_root_and_bytes_are_accepted(self):
        module = self.helper()
        library = self.src / LIBROOT / "lib.es5.d.ts"
        library.parent.mkdir(parents=True)
        library.write_text("independent standard library test bytes\n")
        digest = hashlib.sha256(library.read_bytes()).hexdigest()
        with mock.patch.dict(module._CHROMIX_TYPESCRIPT_LIBRARIES, {library.name: digest}, clear=True):
            self.assertTrue(module._is_chromix_typescript_library(str(library)))
            for relative in ("app.d.ts", "lib.not-a-standard-library.d.ts", "nested/lib.es5.d.ts"):
                path = library.parent / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(library.read_bytes())
                self.assertFalse(module._is_chromix_typescript_library(str(path)))
            outside = self.src / "app/lib.es5.d.ts"
            outside.parent.mkdir()
            outside.write_bytes(library.read_bytes())
            self.assertFalse(module._is_chromix_typescript_library(str(outside)))
            library.write_text("tampered standard library\n")
            self.assertFalse(module._is_chromix_typescript_library(str(library)))
            library.unlink()
            library.symlink_to(outside)
            self.assertFalse(module._is_chromix_typescript_library(str(library)))
            library.unlink()
            library.write_bytes(outside.read_bytes())
            displaced = library.parent.with_name("external-lib")
            library.parent.rename(displaced)
            library.parent.symlink_to(displaced, target_is_directory=True)
            self.assertFalse(module._is_chromix_typescript_library(str(library)))

    def real_compiler(self):
        node = os.environ.get("CHROMIX_TEST_NODE")
        package = os.environ.get("CHROMIX_TEST_TYPESCRIPT")
        if not node or not package:
            self.skipTest("set real Node and independently verified TypeScript package for declaration target")
        package = Path(package).resolve()
        observed = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (package / "lib").glob("lib*.d.ts")}
        self.assertEqual(observed, FIXTURE["typescript"]["standard_libraries_sha256"])
        shutil.copytree(package, self.src / generators.DEVTOOLS / "node_modules/typescript")
        # Chromium's unchanged node.py uses the x64-named host-tool link on Linux.
        executable = self.src / "third_party/node/linux/node-linux-x64/bin/node"
        executable.parent.mkdir(parents=True)
        executable.symlink_to(Path(node).resolve())
        for name, after in self.changes.items():
            if name != HELPER:
                (self.src / name).write_bytes(after)
        self.gen = self.out / "gen/tools/typescript/definitions"
        self.gen.mkdir(parents=True)
        self.definitions = list(FIXTURE["dependency_validation"]["linux_definitions"])

    def run_target(self):
        relative = lambda p: os.path.relpath(p, self.out)
        command = [sys.executable, str(self.src / "tools/typescript/ts_library.py"),
                   "--root_gen_dir", "../../..", "--root_src_dir", "../../../../../../",
                   "--root_dir", "../../tools/typescript/definitions",
                   "--gen_dir", relative(self.gen), "--out_dir", relative(self.gen),
                   "--output_suffix", "build_ts", "--composite", "--tsconfig_base",
                   "../../../../../../tools/typescript/definitions/tsconfig_base.json",
                   "--definitions", *[os.path.relpath(self.src / n, self.gen) for n in self.definitions]]
        return subprocess.run(command, cwd=self.out,
                              env=dict(os.environ, PATH="", PYTHONDONTWRITEBYTECODE="1"),
                              capture_output=True, text=True, timeout=60)

    def test_real_cloud_failure_then_full_declared_target_and_unknown_dts(self):
        self.real_compiler()
        self.assertEqual(len(self.definitions), 49)
        broken = self.run_target()
        self.assertNotEqual(broken.returncode, 0, broken.stdout + broken.stderr)
        self.assertIn("AssertionError: Undeclared dependencies to definition files", broken.stderr)
        self.assertIn("//" + LIBROOT + "/lib.es5.d.ts", broken.stderr)
        (self.src / HELPER).write_bytes(self.changes[HELPER])
        fixed = self.run_target()
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertTrue((self.gen / "tsconfig_build_ts.json").is_file())
        self.assertFalse((self.gen / "tsconfig_build_ts.tsbuildinfo").exists())
        declaration = self.src / "tools/typescript/definitions/chrome_send.d.ts"
        original = declaration.read_text()
        for relative in ("app/unknown.d.ts", LIBROOT + "/lib.unknown.d.ts",
                         generators.DEVTOOLS + "/node_modules/other/unknown.d.ts"):
            unknown = self.src / relative
            unknown.parent.mkdir(parents=True, exist_ok=True)
            unknown.write_text("declare interface UnlistedApplicationType { value: string; }\n")
            reference = os.path.relpath(unknown, declaration.parent).replace(os.sep, "/")
            declaration.write_text(f'/// <reference path="{reference}" />\n' + original)
            negative = self.run_target()
            self.assertNotEqual(negative.returncode, 0)
            self.assertIn("AssertionError: Undeclared dependencies to definition files", negative.stderr)
            self.assertIn("//" + relative, negative.stderr)
            self.definitions.append(relative)
            declared = self.run_target()
            self.assertEqual(declared.returncode, 0, declared.stdout + declared.stderr)
            self.definitions.pop()
            declaration.write_text(original)


class Linux156DependencyFingerprintTest(unittest.TestCase):
    def test_helper_identity_is_in_both_host_fingerprints(self):
        fixture = generator_tests.InstallLinux156GeneratorsTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for host in ("x64", "arm64"):
            fixture.populate_tools(host)
            fixture.populate_repaired_sources(host)
            path = fixture.src / HELPER
            repaired = path.read_bytes()
            path.write_text(SOURCES[HELPER])
            original_report = fixture.inspect(host)
            path.write_bytes(repaired)
            report = fixture.inspect(host)
            self.assertNotEqual(original_report, report)
            self.assertTrue(original_report["repair_needed"])
            self.assertFalse(report["repair_needed"])
            identity = report["esbuild"]["sources"][HELPER]
            self.assertEqual(identity["sha256"], generators.ROOT156_REPAIRS[HELPER][1])
            self.assertEqual(identity["original_sha256"], generators.ROOT156_REPAIRS[HELPER][0])
            path = fixture.src / HELPER
            before = path.read_bytes()
            path.write_bytes(before + b"\n# unexpected\n")
            with self.assertRaisesRegex(ValueError, "unknown Linux esbuild source"):
                fixture.inspect(host)
            path.write_bytes(before)
