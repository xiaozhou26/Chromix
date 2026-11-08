"""Chromium156 WebUI control source with the upstream TS6 Trusted Types DOM contract."""
import hashlib
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from tools import linux_restored_generators as generators
from tools.tests import test_linux156_typescript_deps as deps
from tools.tests.test_linux156_generators import FIXTURE, SOURCES

DOM = generators.DEVTOOLS + "/node_modules/typescript/lib/lib.dom.d.ts"
CUSTOM = "ui/webui/resources/js/custom_element.ts"


class Linux156TypeScriptDomTest(unittest.TestCase):
    def setUp(self):
        self.fixture = deps.Linux156TypeScriptDepsTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.src, self.out = self.fixture.src, self.fixture.out

    def test_checkpoint_helper_and_dom_are_exact_idempotent_transitions(self):
        helper = self.src / deps.HELPER
        helper.write_text(FIXTURE["checkpoint"]["helper"])
        identities, changes = generators.source_plan(self.src, pins=generators.PINS156)
        self.assertEqual(identities[deps.HELPER]["sha256"],
                         "f4b280e7b1776995645e9db53d331ba06cd430201dbdd717e7b64842a73c1032")
        self.assertEqual(identities[DOM]["sha256"], FIXTURE["typescript"]["dom_before_sha256"])
        for name, _, after, _ in changes:
            (self.src / name).write_bytes(after)
        self.assertEqual(generators.source_plan(self.src, pins=generators.PINS156)[1], [])
        helper.write_bytes(helper.read_bytes() + b"\nunknown")
        with self.assertRaisesRegex(ValueError, "unknown Linux esbuild source"):
            generators.source_plan(self.src, pins=generators.PINS156)
        self.assertEqual((self.src / CUSTOM).read_text(), SOURCES[CUSTOM])

    def webui(self):
        self.fixture.real_compiler()
        self.gen = self.out / "gen/ui/webui/resources/js"
        self.preprocessed = self.gen / "preprocessed"
        self.preprocessed.mkdir(parents=True)
        for name in ("custom_element.ts", "assert.ts"):
            shutil.copyfile(self.src / "ui/webui/resources/js" / name, self.preprocessed / name)
        (self.src / deps.HELPER).write_bytes(self.fixture.changes[deps.HELPER])
        self.names = ["custom_element.ts", "assert.ts"]

    def compile_webui(self):
        command = [sys.executable, str(self.src / "tools/typescript/ts_library.py"),
                   "--root_gen_dir", "../../../..", "--root_src_dir", "../../../../../../../",
                   "--root_dir", "gen/ui/webui/resources/js/preprocessed",
                   "--gen_dir", "gen/ui/webui/resources/js",
                   "--out_dir", "gen/ui/webui/resources/tsc/js", "--output_suffix", "build_ts",
                   "--tsconfig_base", "../../../../../../../ui/webui/resources/js/tsconfig_base.json",
                   "--composite", "--in_files", *self.names]
        return subprocess.run(command, cwd=self.out,
                              env=dict(os.environ, PATH="", PYTHONDONTWRITEBYTECODE="1"),
                              capture_output=True, text=True, timeout=60)

    def test_real_custom_element_target_and_negative_trusted_types(self):
        self.webui()
        dom = self.src / DOM
        repaired = dom.read_bytes()
        dom.write_text(SOURCES[DOM])
        # The declaration-only failure is reproduced by the real WebUI control source.
        broken = self.compile_webui()
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn("TS2322", broken.stdout + broken.stderr)
        self.assertIn("string | TrustedHTML", broken.stdout + broken.stderr)
        self.assertIn("custom_element.ts:27", re.sub(r"\x1b\[[0-9;]*m", "", broken.stdout + broken.stderr))
        dom.write_bytes(repaired)
        fixed = self.compile_webui()
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertEqual((self.preprocessed / "custom_element.ts").read_text(), SOURCES[CUSTOM])
        output = self.out / "gen/ui/webui/resources/tsc/js/custom_element.js"
        javascript = output.read_bytes()
        self.assertIn(b"template.innerHTML =", javascript)
        self.assertIn(b"window.trustedTypes.emptyHTML", javascript)
        self.assertNotIn(b"String(", javascript)
        self.assertNotIn(b".toString()", javascript)
        self.assertTrue((self.gen / "build_ts_manifest.json").is_file())
        probe = self.preprocessed / "trusted_contract.ts"
        self.names.append(probe.name)
        positive = '''declare const trustedHtml: TrustedHTML;
declare const trustedUrl: TrustedScriptURL;
const template = document.createElement('template');
const script = document.createElement('script');
template.innerHTML = trustedHtml;
script.src = trustedUrl;
const text: string = template.innerHTML;
export {text};
'''
        probe.write_text(positive)
        valid = self.compile_webui()
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
        for invalid in ("template.innerHTML = trustedUrl;", "template.innerHTML = 42;",
                        "script.src = trustedHtml;", "const bad: TrustedHTML = template.innerHTML;"):
            probe.write_text(positive + invalid + "\n")
            negative = self.compile_webui()
            self.assertNotEqual(negative.returncode, 0)
            self.assertIn("TS2322", negative.stdout + negative.stderr)
        probe.write_text(positive)
        self.assertEqual(self.compile_webui().returncode, 0)
        self.assertEqual(output.read_bytes(), javascript)

    def test_dom_patch_matches_official_transformation_and_no_compiler_swap(self):
        before = SOURCES[DOM].encode()
        after = self.fixture.changes[DOM]
        self.assertEqual(hashlib.sha256(after).hexdigest(), FIXTURE["typescript"]["dom_after_sha256"])
        self.assertEqual(after.count(b"set innerHTML(html: string|TrustedHTML);"), 2)
        self.assertIn(b"get innerHTML(): string;", after)
        self.assertIn(b"set src(url: string|TrustedScriptURL);", after)
        self.assertEqual(after.count(b"as any"), before.count(b"as any"))
        self.assertEqual(FIXTURE["typescript"]["dom_before_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(generators.PINS156[0], "156.0.8078.79")
        self.assertIn('"typescript": "6.0.2"', SOURCES["third_party/node/package.json"])


class Linux156DomPackageIdentityTest(unittest.TestCase):
    def test_both_hosts_repair_checkpoint_package_and_reject_mixed_inventory(self):
        from tools.tests import test_linux156_generators as tests
        from unittest import mock
        for host in ("x64", "arm64"):
            fixture = tests.InstallLinux156GeneratorsTest()
            fixture.setUp()
            try:
                fixture.populate_tools(host)
                fixture.populate_repaired_sources(host)
                fixture.write(DOM, SOURCES[DOM].encode())
                fixture.write(deps.HELPER, FIXTURE["checkpoint"]["helper"].encode())
                before = fixture.inspect(host)
                self.assertTrue(before["repair_needed"])
                with mock.patch.object(tests.prepare.subprocess, "run", side_effect=fixture.probe):
                    repaired = fixture.inspect(host, repair=True)
                self.assertFalse(repaired["repair_needed"])
                self.assertEqual(repaired, fixture.inspect(host))
                self.assertNotEqual(before["package_sha256"], repaired["package_sha256"])
                library = fixture.src / tests.prepare.TYPESCRIPT_PACKAGE / "lib/lib.es5.d.ts"
                original = library.read_bytes()
                library.write_bytes(original + b"\nchanged")
                with mock.patch.object(tests.prepare.subprocess, "run") as run:
                    with self.assertRaisesRegex(ValueError, "package bytes/inventory"):
                        fixture.inspect(host, repair=True)
                    run.assert_not_called()
            finally:
                fixture.doCleanups()
