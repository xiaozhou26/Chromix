"""Real stage/archive/source checks with a tiny native Ninja object failure."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import unittest
from unittest import mock

import yaml

from tools.tests.test_posix_ci_stage import FullCacheFixture, REPO
from tools.tests.test_verify_patch_stack import source_tree
from tools import verify_macos_checkpoint as checkpoint


@unittest.skipUnless(sys.platform == "darwin" and all(shutil.which(name) for name in
                    ("ninja", "clang", "zstd", "gsplit", "timeout")), "native macOS build tools required")
class MacFailedCheckpointTest(FullCacheFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.repo, _ = source_tree(self.root)
        self.work = self.root / "work"
        self.cache = self.root / "chromix-upstream"
        self.arch = checkpoint.restore_ninja.host_identity()[1]
        for name in ("CHROMIUM_VERSION", "CHROMIUM_MACOS_VERSION", "UNGOOGLED_VERSION",
                     "CHROMIUM_LINUX_VERSION", "CHROMIUM_WINDOWS_VERSION", "UNGOOGLED_WINDOWS_VERSION",
                     "build/ungoogled-revisions.psd1", "build/upstream-cache.json",
                     "build/linux/157.0.8103.78/source.json",
                     "build/posix/ci-stage.sh", "build/posix/ci-parts.sh",
                     "build/prepare-ungoogled.sh", "build/apply-patches.sh"):
            self.put(self.repo / name, (REPO / name).read_bytes())
        shutil.copytree(REPO / "build/linux/157.0.8103.78/patches",
                        self.repo / "build/linux/157.0.8103.78/patches")
        tools = self.repo / "tools"
        tools.mkdir()
        for path in (REPO / "tools").glob("*.py"):
            (tools / path.name).symlink_to(path)
        (tools / "verify_macos_checkpoint.py").unlink()
        shutil.copy2(REPO / "tools/verify_macos_checkpoint.py", tools / "verify_macos_checkpoint.py")
        patch_root = self.root / "patch-fixture"
        patch_root.mkdir()
        patch_repo, _ = source_tree(patch_root)
        selector = tools / "patch_selection.py"
        selector.unlink()
        selector.write_text((REPO / "tools/patch_selection.py").read_text() +
                            f'\n_fixture_select = select\ndef select(repo, platform=None, **kwargs):\n'
                            f'    return _fixture_select(Path({str(patch_repo)!r}), platform)\n')
        real_select = checkpoint.patch_selection.select
        selection = mock.patch.object(checkpoint.patch_selection, "select",
                                      side_effect=lambda repo, platform=None, **kwargs:
                                      real_select(patch_repo, platform))
        selection.start()
        self.addCleanup(selection.stop)
        self.pin_repo = self.repo
        self.seed("macos", self.arch)
        self.assertEqual(checkpoint.restore.restore(self.work, "macos", self.arch,
                                                    self.cache, repo=self.repo)["status"], "hit")
        self.src = self.work / "src"
        self.out = self.src / "out/Default"
        self.put(self.src / "file.cc", (self.root / "source/file.cc").read_bytes())
        self.pins = checkpoint.platform_pins.load_pins(self.repo, "macos")
        key = "|".join(("macos", self.arch, self.pins["ChromiumVersion"], self.pins["UngoogledCommit"],
                        self.pins["UngoogledMacOSCommit"],
                        checkpoint.patch_selection.preparation_key(self.repo, "macos", "posix")))
        self.put(self.src / ".chromix-source-ready", key + "\n")
        self.put(self.work / "fingerprint-diagnostics/source-final.json", json.dumps(
            checkpoint.verify_patch_stack.verify(self.src, self.repo, platform="macos")))
        for name in ("ungoogled-chromium", "ungoogled-chromium-macos"):
            (self.work / "tooling" / name).mkdir(parents=True)
        ninja = Path(shutil.which("ninja")).resolve()
        version = subprocess.check_output([str(ninja), "--version"], text=True).strip()
        log_format = checkpoint.restore_ninja.version_format(version)
        self.put(self.out / ".ninja_log", f"# ninja log v{log_format}\n")
        self.put(self.work / checkpoint.restore_ninja.REPORT, json.dumps({
            "schema_version": 1, "status": "selected", "platform": "macos", "arch": self.arch,
            "host": ["macos", self.arch], "log": {"format": log_format},
            "selected": {"path": str(ninja), "version": version}}))
        self.put(self.out / "good.c", "int retained(void) { return 1033; }\n")
        self.put(self.out / "bad.c", '#error simulated ICU compile failure\n')
        self.put(self.out / "build.ninja", "rule cc\n"
                 "  command = clang -MMD -MF $out.d -c $in -o $out\n"
                 "  depfile = $out.d\n  deps = gcc\n"
                 "build obj/current.o: cc good.c\n"
                 "build obj/failure.o: cc bad.c || obj/current.o\n"
                 "default obj/failure.o\n")
        real_git = shutil.which("git")
        self.put(self.root / "bin/git", f"#!{sys.executable}\n"
                 "import os,sys\nfrom pathlib import Path\na=sys.argv[1:]\n"
                 "if len(a)==4 and a[0]=='-C' and a[2:]==['rev-parse','HEAD']:\n"
                 f"    print({self.pins['UngoogledCommit']!r} if Path(a[1]).name == 'ungoogled-chromium' "
                 f"else {self.pins['UngoogledMacOSCommit']!r})\n"
                 f"else: os.execv({real_git!r}, [{real_git!r}, *a])\n", True)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("CHROMIX_")}
        self.env.update(PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"],
                        GITHUB_OUTPUT=str(self.root / "outputs"), PYTHONDONTWRITEBYTECODE="1")
        self.ninja = ninja
        self.record_ready()

    def record_ready(self):
        self.put(self.work / checkpoint.READY, json.dumps(checkpoint.ready_identity(self.work, self.arch, 6)))

    def verify(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return checkpoint.verify(self.work, self.arch, 6, repo=self.repo)

    def stage(self, failure="object"):
        build = self.repo / "build/macos/build.sh"
        script = '#!/bin/bash\nset -euo pipefail\n'
        if failure in ("source", "partial"):
            if failure == "source":
                script += 'printf \'{"status":"failed"}\\n\' > "$1/fingerprint-diagnostics/source-final.json"\n'
            script += 'exit 17\n'
        else:
            script += (f'python3 "{self.repo}/tools/verify_macos_checkpoint.py" --record-ready '
                       '--workdir "$1" --arch "$2" --stage "$CHROMIX_MAC157_CHECKPOINT_STAGE"\n')
            if failure == "blocked":
                script += 'touch "$1/src/.chromix-domain-substitution-in-progress"\n'
            if failure == "patch":
                script += 'printf "stale source\\n" > "$1/src/file.cc"\n'
            script += f'"{self.ninja}" -C "$1/src/out/Default" -j1\n'
        self.put(build, script, True)
        result = subprocess.run(["/bin/bash", str(self.repo / "build/posix/ci-stage.sh"),
                                 "--platform", "macos", "--arch", self.arch,
                                 "--workdir", str(self.work), "--stage-index", "6"],
                                env=self.env, capture_output=True, text=True, timeout=45)
        outputs = dict(line.split("=", 1) for line in (self.root / "outputs").read_text().splitlines())
        return result, outputs

    def test_real_object_failure_saves_latest_tree_and_preserves_failure(self):
        result, outputs = self.stage()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("simulated ICU compile failure", result.stdout)
        self.assertEqual(outputs["status"], "failed")
        self.assertIn("exit 1", outputs["last_error"])
        self.assertEqual(outputs["finished"], "false")
        self.assertEqual(outputs["mac157_failed_checkpoint"], "true")
        self.assertEqual(outputs["upload_snapshot"], "true")
        self.assertNotIn("compiled_ready", outputs)
        self.assertEqual(self.verify()["status"], "verified")
        volumes = sorted((self.work / ".snapshot-stage-6").glob("p*/tree.tar.zst.*"))
        self.assertTrue(volumes)
        archive = self.root / "snapshot.tar"
        with archive.open("wb") as output:
            subprocess.run(["zstd", "-dc", *map(str, volumes)], stdout=output, check=True)
        with archive.open("rb") as stream:
            from tools.verify_snapshot_tar import verify
            verify(stream)
        with tarfile.open(archive) as stream:
            self.assertEqual(stream.extractfile("./src/out/Default/obj/current.o").read(),
                             (self.out / "obj/current.o").read_bytes())
            self.assertEqual(stream.extractfile("./src/out/Default/.ninja_log").read(),
                             (self.out / ".ninja_log").read_bytes())
        # Exercise the generated failure-path verifier, not just ci-stage.sh.
        workflow = yaml.safe_load((REPO / ".github/workflows/build-posix-github.yml").read_text())
        steps = workflow["jobs"]["posix-6"]["steps"]
        step = next(item for item in steps if item.get("id") == "checkpoint")
        for item in [step, *(s for s in steps if s.get("name", "").startswith("Upload tree part"))]:
            self.assertIn("always() && inputs.platform == 'macos'", item["if"])
            self.assertIn("steps.stage.outputs.mac157_failed_checkpoint == 'true'", item["if"])
        runner = self.root / "runner"
        runner.mkdir()
        (runner / "chromix-build").symlink_to(self.work)
        (runner / "chromix-logs").mkdir()
        script = step["run"]
        for expression, value in (("inputs.platform", "macos"), ("inputs.arch", self.arch),
                                  ("steps.stage.outputs.mac157_failed_checkpoint", "true"),
                                  ("steps.stage.outputs.status", "failed")):
            script = script.replace("${{ " + expression + " }}", value)
        # The production work root is resolved, not a runner symlink.
        script = script.replace('${RUNNER_TEMP}/chromix-build', str(self.work))
        verified = subprocess.run(["/bin/bash", "-c", script], cwd=self.repo,
                                  env={**self.env, "RUNNER_TEMP": str(runner)},
                                  text=True, capture_output=True, timeout=45)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        with volumes[0].open("r+b") as stream:
            stream.write(b"corrupt zstd stream")
        rejected = subprocess.run(["/bin/bash", "-c", script], cwd=self.repo,
                                  env={**self.env, "RUNNER_TEMP": str(runner)},
                                  text=True, capture_output=True, timeout=45)
        self.assertNotEqual(rejected.returncode, 0)
        for item in steps:
            if item.get("name", "").startswith("Upload tree part"):
                self.assertIn("steps.checkpoint.outcome == 'success'", item["if"])

    def test_snapshot_failure_does_not_hide_native_failure_or_enable_upload(self):
        self.put(self.repo / "build/posix/ci-parts.sh", "#!/bin/bash\nexit 29\n")
        result, outputs = self.stage()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(outputs["status"], "failed")
        self.assertNotIn("upload_snapshot", outputs)
        self.assertNotIn("mac157_failed_checkpoint", outputs)
        self.assertTrue((self.out / "obj/current.o").is_file())

    def test_source_failure_cannot_archive_an_old_ready_receipt(self):
        result, outputs = self.stage("source")
        self.assertEqual(result.returncode, 17)
        self.assertEqual(outputs["status"], "failed")
        self.assertNotIn("upload_snapshot", outputs)
        self.assertFalse((self.work / ".snapshot-stage-6").exists())

    def test_partial_preparation_cannot_reuse_prior_stage_readiness(self):
        result, outputs = self.stage("partial")
        self.assertEqual(result.returncode, 17)
        self.assertNotIn("upload_snapshot", outputs)
        self.assertFalse((self.work / checkpoint.READY).exists())

    def test_blocked_migration_or_changed_source_never_archives(self):
        for failure in ("blocked", "patch"):
            with self.subTest(failure=failure):
                result, outputs = self.stage(failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("upload_snapshot", outputs)
                self.assertFalse((self.work / ".snapshot-stage-6").exists())
                (self.src / ".chromix-domain-substitution-in-progress").unlink(missing_ok=True)

    def test_verifier_rejects_bad_provenance_without_mutating_tree(self):
        self.assertEqual(self.verify()["status"], "verified")
        cases = {"src/.chromix-source-ready": "unprepared",
                 "src/out/Default/args.gn": 'target_cpu = "wrong"\n',
                 "src/out/Default/build.ninja": "wrong graph",
                 "src/out/Default/.ninja_log": "# ninja log v99\n",
                 "src/out/Default/.ninja_deps": "bad deps",
                 "upstream-cache-ninja.json": "{}",
                 "src/.chromix-upstream-restored.json": "{}",
                 "fingerprint-diagnostics/source-final.json": '{"status":"failed"}',
                 "src/file.cc": "stale source"}
        for name, content in cases.items():
            with self.subTest(name=name):
                path = self.work / name
                before = path.read_bytes()
                path.write_text(content)
                # GN is bound to the successful generation, not a later replacement.
                if name != "src/out/Default/build.ninja":
                    self.record_ready()
                with self.assertRaises((ValueError, OSError, checkpoint.restore.Miss, KeyError)):
                    self.verify()
                self.assertEqual(path.read_text(), content)
                path.write_bytes(before)
        self.record_ready()
        with self.assertRaises(ValueError):
            checkpoint.verify(self.work, self.arch, 5, repo=self.repo)

    def test_architecture_tooling_and_ninja_provenance_are_mandatory(self):
        report = self.work / checkpoint.restore_ninja.REPORT
        original = json.loads(report.read_text())
        for field, value in (("arch", "wrong"), ("host", ["linux", self.arch]),
                             ("selected", {**original["selected"], "version": "1.11.0"})):
            with self.subTest(field=field):
                report.write_text(json.dumps({**original, field: value}))
                self.record_ready()
                with self.assertRaises(ValueError):
                    self.verify()
        report.write_text(json.dumps(original))
        self.record_ready()
        with mock.patch.object(checkpoint.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "wrong commit\n")), \
                mock.patch.object(checkpoint.restore_ninja, "probe_version",
                                  return_value=original["selected"]["version"]):
            with self.assertRaisesRegex(ValueError, "tooling provenance"):
                self.verify()
        marker = self.work / checkpoint.READY
        marker.unlink()
        marker.symlink_to(self.work / "upstream-cache-ninja.json")
        with self.assertRaises((ValueError, checkpoint.restore.Miss)):
            self.verify()

    def test_native_ready_is_recorded_only_after_source_gn_and_plan(self):
        source = (REPO / "build/macos/build.sh").read_text()
        start = source.index('cp "$SOURCE_REPORT"')
        gn = source.index('"$OUT/gn" gen')
        plan = source.index('if chromix_report_upstream_plan')
        ready = source.index('verify_macos_checkpoint.py" --record-ready')
        compile_start = source.index('chromix_build_restored_target macos')
        self.assertLess(start, gn)
        self.assertLess(gn, plan)
        self.assertLess(plan, ready)
        self.assertLess(ready, compile_start)


if __name__ == "__main__":
    unittest.main()
