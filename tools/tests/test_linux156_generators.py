"""Independent Chromium156 donor bytes, exact tools, and real GN launch contracts."""
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools import linux_restored_generators as generators
from tools import prepare_restored_build as prepare
from tools.tests import test_linux155_generators as previous
from tools.tests import test_install_linux155_generators as install_previous

RAW = gzip.decompress((Path(__file__).parent / "fixtures/linux156_generators.json.gz").read_bytes())
FIXTURE = json.loads(RAW)
SOURCES = FIXTURE["sources"]
DEVTOOLS_SOURCES = {name.removeprefix(generators.DEVTOOLS + "/"): text
                   for name, text in SOURCES.items() if name.startswith(generators.DEVTOOLS + "/")}


class Linux156GeneratorsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.src = Path(temporary.name).resolve()
        for name, text in SOURCES.items():
            path = self.src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_independent_source_identity_and_all_repairs(self):
        self.assertEqual(hashlib.sha256(RAW).hexdigest(),
                         "35279f781fbf4d4e0655b4dd6f75c36a02815862c15446dc687c9d35238d8716")
        self.assertEqual(FIXTURE["devtools_commit"], "8c62f844ad8e0664ff41a633bfc17c472f1c5fd6")
        self.assertEqual(FIXTURE["core_commit"], generators.PINS156[1])
        self.assertEqual(FIXTURE["platform_commit"], generators.PINS156[2])
        identities, changes = generators.source_plan(self.src, pins=generators.PINS156)
        self.assertEqual(len(identities), 14)
        self.assertEqual(len(changes), 8)
        for name, before, after, _ in changes:
            self.assertEqual(before.decode(), SOURCES[name])
            self.assertEqual(generators.sha256(after), identities[name]["repaired_sha256"])
            (self.src / name).write_bytes(after)
        self.assertEqual(generators.source_plan(self.src, pins=generators.PINS156)[1], [])
        for pins in (generators.PINS, generators.PINS155, generators.PINS15579):
            with self.assertRaisesRegex(ValueError, "package lock"):
                generators.source_plan(self.src, pins=pins)
            for mixed in ((generators.PINS156[0], *pins[1:]), (pins[0], *generators.PINS156[1:])):
                with self.assertRaisesRegex(ValueError, "pins"):
                    generators.generator_profile(mixed)

    def test_unknown_source_and_rollback_inputs_fail_without_execution(self):
        for name in [generators.LOCK, *generators.ROOT156_REPAIRS,
                     *(generators.DEVTOOLS + "/" + n for n in generators.SOURCE156_REPAIRS)]:
            path = self.src / name
            before = path.read_bytes()
            path.write_bytes(before + b"\nchanged")
            with self.subTest(name=name), mock.patch.object(subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "unknown"):
                    generators.prepare_esbuild(self.src, host_arch="x64", pins=generators.PINS156)
                run.assert_not_called()
            path.write_bytes(before)

    def test_typescript_go_fallback_uses_verified_local_compiler(self):
        for name, _, after, _ in generators.source_plan(self.src, pins=generators.PINS156)[1]:
            (self.src / name).write_bytes(after)
        self.assertIn("use_typescript_go = false", (self.src / "ui/webui/webui_features.gni").read_text())
        for suffix in ("ts_library", "ts_definitions"):
            script = (self.src / f"tools/typescript/{suffix}.py").read_text()
            self.assertIn("if args.use_typescript_go:", script)
            self.assertIn("node.RunNode(", script)
            self.assertIn("node_modules.PathToTypescript()", script)
            gn = (self.src / f"tools/typescript/{suffix}.gni").read_text()
            self.assertIn("if (use_typescript_go)", gn)
        module_path = self.src / "third_party/node/node_modules.py"
        spec = importlib.util.spec_from_file_location("linux156_node_modules", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(Path(module.PathToTypescript()), self.src / prepare.TYPESCRIPT_PACKAGE / "lib/tsc.js")

    def test_download_identities_and_typescript_tree_are_independent(self):
        profile = generators.generator_profile(generators.PINS156)
        self.assertEqual(profile["typescript_tree"], FIXTURE["typescript"]["tree_sha256"])
        self.assertEqual(profile["version"], "0.28.2")
        for name, (url, digest) in profile["archives"].items():
            self.assertEqual(FIXTURE["esbuild"][name], {"url": url, "sha256": digest})
        self.assertEqual(generators.sha256(SOURCES[prepare.TYPESCRIPT_WRAPPER].encode()),
                         profile["typescript_portable"])

    def test_real_typescript_declarations_preserve_javascript(self):
        node, tsc = os.environ.get("CHROMIX_TEST_NODE"), os.environ.get("CHROMIX_TEST_TSC")
        if not node or not tsc:
            self.skipTest("set CHROMIX_TEST_NODE and CHROMIX_TEST_TSC for TypeScript execution")
        version = subprocess.run([node, tsc, "--version"], capture_output=True, text=True, check=True)
        self.assertEqual(version.stdout.strip(), "Version 6.0.2")
        names = ["front_end/ui/legacy/ARIAUtils.ts", "front_end/ui/legacy/Widget.ts"]
        changes = {name: after for name, _, after, _ in
                   generators.source_plan(self.src, pins=generators.PINS156)[1]}
        flags = ["--ignoreConfig", "--declaration", "--isolatedDeclarations", "--noCheck", "--noResolve",
                 "--target", "esnext", "--module", "esnext"]
        for fixed in (False, True):
            if fixed:
                for name in names:
                    (self.src / generators.DEVTOOLS / name).write_bytes(changes[generators.DEVTOOLS + "/" + name])
            paths = [self.src / generators.DEVTOOLS / name for name in names]
            result = subprocess.run([node, tsc, *flags, "--outDir", str(self.src / str(fixed)),
                                     "--rootDir", str(paths[0].parent), *map(str, paths)],
                                    cwd=self.src, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0 if fixed else 1, result.stdout + result.stderr)
            if not fixed:
                self.assertEqual(result.stdout.count("error TS9025:"), 2, result.stdout)
        for name in names:
            stem = Path(name).stem
            self.assertEqual((self.src / "False" / (stem + ".js")).read_bytes(),
                             (self.src / "True" / (stem + ".js")).read_bytes())

    def _gn_fixture(self):
        self.patch(previous, "GN_SOURCES", DEVTOOLS_SOURCES)
        self.patch(generators, "PINS155", generators.PINS156)
        # Only the test's old profile dispatch is isolated; production pins stay exact.
        original = generators.generator_profile
        def profile(pins=generators.PINS):
            if pins == generators.PINS156:
                return {"version": "0.28.2", "lock": generators.LOCK156_SHA256,
                        "sources": generators.SOURCE156_REPAIRS, "root_sources": generators.ROOT156_REPAIRS}
            return original(pins)
        self.patch(generators, "generator_profile", side_effect=profile)
        previous.Linux155GeneratorsTest._gn_fixture(self)
        build = self.src / "BUILD.gn"
        text = build.read_text().replace('"' + previous.API + ':api"', '":api_node_typecheck"')
        text += f'''\nimport("//{generators.DEVTOOLS}/scripts/build/typescript/ts_type_check.gni")
ts_type_check("api_node_typecheck") {{
  sources = [ "probe.ts" ]
  runs_in = "node"
}}
'''
        build.write_text(text)

    _run_gn = previous.Linux155GeneratorsTest._run_gn
    _gn_host = previous.Linux155GeneratorsTest._gn_host

    def test_real_gn_all_hosts_and_remote_inputs(self):
        self._gn_fixture()
        for host, cpu in (("linux", "x64"), ("linux", "arm64"), ("mac", "x64"),
                          ("mac", "arm64"), ("win", "x64"), ("win", "arm64")):
            for remote in (False, True):
                with self.subTest(host=host, cpu=cpu, remote=remote):
                    self._gn_host(host, cpu)
                    out = f"out/{host}-{cpu}-{remote}"
                    result = self._run_gn("gen", out, "--fail-on-unused-args",
                                          f"--args=use_remoteexec={str(remote).lower()}")
                    self.assertEqual(result.returncode, 0, result.stdout)
                    proof = json.loads((self.src / out / "vars.json").read_text())
                    result = self._run_gn("desc", out, "*", "--format=json")
                    self.assertEqual(result.returncode, 0, result.stdout)
                    actions = [a for a in json.loads(result.stdout).values() if a["type"] == "action"]
                    self.assertEqual(len(actions), 5)
                    for action in actions:
                        response = action["response_file_contents"]
                        start = response.index("--") + 1
                        self.assertEqual(response[start:start + len(proof["command"])], proof["command"])
                        self.assertTrue(set(proof["inputs"]).issubset(action["inputs"]))
                    if host == "linux":
                        self.assertEqual(proof["command"], [f"../../third_party/node/linux/node-linux-{cpu}/bin/node",
                            f"../../{generators.DEVTOOLS}/node_modules/typescript/lib/tsc.js"])
                        self.assertIn("//" + prepare.TYPESCRIPT_PACKAGE + "/lib/_tsc.js", proof["inputs"])
                        self.assertTrue(all("/node_modules/typescript/lib/" in n for n in proof["standard_inputs"]))
                        self.assertNotIn("//third_party/typescript/linux-amd64/src/lib/tsc", proof["inputs"])
                    else:
                        self.assertEqual(len(proof["command"]), 1)
                        self.assertFalse(any("node-linux-" in n for n in proof["inputs"]))

    def test_real_ninja_node_launch_and_failure(self):
        with mock.patch.object(previous, "API", "//"):
            previous.Linux155GeneratorsTest.test_real_ninja_api_node_typecheck_launch_and_failure(self)


class InstallLinux156GeneratorsTest(install_previous.InstallLinux155GeneratorsTest):
    def setUp(self):
        self.patch(generators, "PINS155", generators.PINS156)
        self.patch(generators, "SOURCE155_REPAIRS", generators.SOURCE156_REPAIRS)
        self.patch(install_previous, "SCRIPTS", DEVTOOLS_SOURCES)
        super().setUp()
        for name in generators.ROOT156_REPAIRS:
            self.write(name, SOURCES[name].encode())
        original = generators.generator_profile
        def profile(pins=generators.PINS):
            result = original(pins)
            if pins == generators.PINS156:
                result["root_sources"] = generators.ROOT156_REPAIRS
            return result
        self.patch(generators, "generator_profile", side_effect=profile)

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
            self.assertEqual(len(result["esbuild"]["sources"]), 14)
            self.assertEqual([call.kwargs["pins"] for call in esbuild.call_args_list], [generators.PINS156] * 2)
            self.assertTrue(esbuild.call_args_list[1].kwargs["repair"])
            self.assertEqual(run.call_count, 3)
            self.assertIn('e.version!=="0.28.2"', run.call_args_list[-1].args[0][2])
            self.assertEqual(run.call_args_list[-1].args[0][0],
                             str(self.src / prepare.tool_paths("linux", host)["node"]))
            self.assertEqual(self.snapshot(), before)

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
        self.assertEqual(len(result["esbuild"]["sources"]), 14)
        self.assertEqual(result["sha256"], generators.TYPESCRIPT155_REPAIRED["x64"])
        self.assertEqual(self.install()["status"], "already_installed")
