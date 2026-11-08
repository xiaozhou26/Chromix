"""Fail-closed checkpoint identity checks never repair source or Ninja state."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import restore_upstream_cache as restore
from tools import verify_linux_checkpoint as checkpoint
from tools.tests.test_posix_ci_stage import FullCacheFixture, REPO
from tools.tests.test_restore_ninja import binary_header
from tools.tests.test_verify_patch_stack import source_tree


class LinuxCheckpointIdentityTest(FullCacheFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.assertEqual(restore.restore(self.work, "linux", "x64", self.cache)["status"], "hit")
        self.src = self.work / "src"
        self.out = self.src / "out/Default"
        self.pins = checkpoint.platform_pins.load_pins(REPO, "linux")
        self.key = "|".join(("linux", "x64", self.pins["ChromiumVersion"], self.pins["UngoogledCommit"],
                            self.pins["UngoogledLinuxCommit"],
                            checkpoint.patch_selection.preparation_key(REPO, "linux", "posix")))
        self.put(self.src / ".chromix-source-ready", self.key + "\n")
        self.ninja = self.root / "ninja"
        self.put(self.ninja, binary_header("linux", "x64"), executable=True)
        self.selection = {"schema_version": 1, "status": "selected", "platform": "linux", "arch": "x64",
                          "host": ["linux", "x64"], "log": {"format": 5},
                          "selected": {"path": str(self.ninja), "version": "1.11.1"}}
        self.put(self.work / checkpoint.restore_ninja.REPORT, json.dumps(self.selection))
        self.core = self.work / "tooling/ungoogled-chromium"
        self.tooling = self.work / "tooling/ungoogled-chromium-portablelinux"
        self.core.mkdir(parents=True)
        self.tooling.mkdir()
        self.host = mock.patch.object(checkpoint.restore_ninja, "host_identity", return_value=("linux", "x64"))
        self.probe = mock.patch.object(checkpoint.restore_ninja, "probe_version", return_value="1.11.1")
        self.git = mock.patch.object(checkpoint.subprocess, "run", side_effect=self.git_head)
        self.source = mock.patch.object(checkpoint.verify_patch_stack, "verify", return_value={"status": "verified"})
        for patch in (self.host, self.probe, self.git, self.source):
            patch.start()
            self.addCleanup(patch.stop)

    def git_head(self, args, **kwargs):
        self.assertEqual(args[:2], ["git", "-C"])
        self.assertEqual(args[3:], ["rev-parse", "HEAD"])
        self.assertTrue(kwargs["check"])
        commit = self.pins["UngoogledCommit" if Path(args[2]) == self.core else "UngoogledLinuxCommit"]
        return subprocess.CompletedProcess(args, 0, commit + "\n", "")

    def verify(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.src.rglob("*") if path.is_file() and not path.is_symlink()}
        try:
            return checkpoint.verify(self.work, "x64")
        finally:
            self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before})

    def test_verified_latest_tree_is_not_a_success_or_reuse_claim(self):
        result = self.verify()
        self.assertTrue(result["source_verified"])
        self.assertEqual(result["output"], "src/out/Default")
        self.assertEqual(result["arch"], "x64")
        self.assertEqual(result["provenance"]["identity"]["arch"], "x64")
        self.assertEqual(result["ninja"]["version"], "1.11.1")
        for claim in ("compiled_ready", "finished", "runtime_verified", "retention_proven"):
            self.assertNotIn(claim, result)

    def test_cold_tree_requires_matching_arch_and_compatible_native_ninja(self):
        (self.src / restore.MARKER).unlink()
        (self.work / checkpoint.restore_ninja.REPORT).unlink()
        self.out.rename(self.src / 'out/Chromix')
        with mock.patch.object(checkpoint.restore_ninja, 'candidates', return_value=[('PATH', self.ninja)]):
            result = self.verify()
            self.assertEqual(result['output'], 'src/out/Chromix')
            self.assertEqual(result['provenance'], {'preparation_key': self.key})
            self.assertTrue(result['source_verified'])
            with mock.patch.object(checkpoint.restore_ninja, 'probe_version', return_value='1.13.2'):
                with self.assertRaisesRegex(ValueError, 'Ninja executable/version'):
                    self.verify()

    def test_linux_arm64_checkpoint_accepts_native_or_x64_host_not_wrong_target(self):
        receipt_path = self.src / restore.MARKER
        receipt = json.loads(receipt_path.read_text())
        identity, _, manifest = restore.identities(REPO, 'linux', 'arm64')
        args = 'target_cpu = "arm64"\n'
        receipt.update(identity=identity, manifest=manifest, arch='arm64')
        receipt['original_args'] = {'path': 'out/Default/args.gn', 'text': args,
                                    'bytes': len(args.encode()), 'sha256': hashlib.sha256(args.encode()).hexdigest(),
                                    'assignments': restore.parse_gn_assignments(args)}
        self.put(receipt_path, json.dumps(receipt))
        self.put(self.out / 'args.gn', args)
        self.put(self.src / '.chromix-source-ready', self.key.replace('linux|x64|', 'linux|arm64|'))
        for host in ('x64', 'arm64'):
            with self.subTest(host=host):
                self.put(self.ninja, binary_header('linux', host), executable=True)
                self.put(self.work / checkpoint.restore_ninja.REPORT,
                         json.dumps({**self.selection, 'arch': 'arm64', 'host': ['linux', host]}))
                with mock.patch.object(checkpoint.restore_ninja, 'host_identity', return_value=('linux', host)):
                    result = checkpoint.verify(self.work, 'arm64')
                    self.assertEqual(result['arch'], 'arm64')
                    self.assertEqual(result['ninja']['host'], ['linux', host])
                    with self.assertRaises(ValueError):
                        checkpoint.verify(self.work, 'x64')

    def test_missing_identity_inputs_are_rejected_without_mutating_objects(self):
        for relative in ("src/.chromix-source-ready", "src/.chromix-upstream-restored.json",
                         "src/chrome/VERSION", "src/out/Default/args.gn", "src/out/Default/build.ninja",
                         "src/out/Default/.ninja_log", "src/out/Default/.ninja_deps", checkpoint.restore_ninja.REPORT):
            with self.subTest(relative=relative):
                path = self.work / relative
                content = path.read_bytes()
                path.unlink()
                with self.assertRaises((OSError, ValueError, restore.Miss)):
                    self.verify()
                path.write_bytes(content)

    def test_wrong_preparation_arch_provenance_and_ninja_identity_are_rejected(self):
        cases = {"src/.chromix-source-ready": "unverified",
                 "src/out/Default/args.gn": 'target_cpu = "arm64"\n',
                 "src/out/Default/.ninja_log": "# ninja log v99\n",
                 "src/out/Default/.ninja_deps": "bad dependencies",
                 "src/.chromix-upstream-restored.json": "{}"}
        for relative, content in cases.items():
            with self.subTest(relative=relative):
                path = self.work / relative
                original = path.read_bytes()
                path.write_text(content)
                with self.assertRaises((ValueError, restore.Miss)):
                    self.verify()
                path.write_bytes(original)
        for key, value in (("arch", "arm64"), ("platform", "macos"), ("status", "failed"),
                           ("host", ["linux", "arm64"]), ("log", {"format": 7}),
                           ("selected", {"path": str(self.ninja), "version": "1.13.2"})):
            with self.subTest(key=key):
                self.put(self.work / checkpoint.restore_ninja.REPORT, json.dumps({**self.selection, key: value}))
                with self.assertRaises(ValueError):
                    self.verify()
        self.put(self.work / checkpoint.restore_ninja.REPORT, json.dumps(self.selection))
        self.put(self.ninja, binary_header("linux", "arm64"), executable=True)
        with self.assertRaisesRegex(ValueError, "Ninja executable/version"):
            self.verify()

    def test_unverified_source_or_tooling_never_passes(self):
        for source in ({"status": "failed"}, {"status": "incomplete"}):
            with mock.patch.object(checkpoint.verify_patch_stack, "verify", return_value=source):
                with self.assertRaisesRegex(ValueError, "source is not verified"):
                    self.verify()
        with mock.patch.object(checkpoint.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "wrong")):
            with self.assertRaisesRegex(ValueError, "tooling provenance"):
                self.verify()

    def test_real_source_roundtrip_rejects_stale_bytes_and_interrupted_preparation(self):
        self.source.stop()
        self.git.stop()
        scratch = self.root / "patch-fixture"
        scratch.mkdir()
        repo, source = source_tree(scratch)
        self.put(self.src / "file.cc", (source / "file.cc").read_bytes())
        real_verify = checkpoint.verify_patch_stack.verify
        # Small patch stack, but real reverse/forward git verification on current source bytes.
        with mock.patch.object(checkpoint.verify_patch_stack, "verify",
                               side_effect=lambda src, *_a, **_kw: real_verify(src, repo)), \
                mock.patch.object(checkpoint.subprocess, "run", wraps=subprocess.run) as runner:
            runner.side_effect = lambda args, **kw: (self.git_head(args, **kw) if args[:2] == ["git", "-C"]
                                                     else runner._mock_wraps(args, **kw))
            self.assertTrue(self.verify()["source_verified"])
            self.put(self.src / "file.cc", "int version = 1;\n// https://example.com\n")
            with self.assertRaisesRegex(ValueError, "stale/incompatible"):
                self.verify()
            self.put(self.src / "file.cc", (source / "file.cc").read_bytes())
            self.put(self.src / ".chromix-domain-substitution-in-progress", "")
            with self.assertRaisesRegex(ValueError, "unfinished preparation"):
                self.verify()

    def test_linked_inputs_are_rejected(self):
        for relative in ("src/.chromix-source-ready", "src/.chromix-upstream-restored.json",
                         "src/out/Default/args.gn", checkpoint.restore_ninja.REPORT):
            with self.subTest(relative=relative):
                path = self.work / relative
                content = path.read_bytes()
                outside = self.root / "outside"
                outside.write_bytes(content)
                path.unlink()
                path.symlink_to(outside)
                with self.assertRaises((ValueError, restore.Miss)):
                    self.verify()
                self.assertEqual(outside.read_bytes(), content)
                path.unlink()
                path.write_bytes(content)


if __name__ == "__main__":
    unittest.main()
