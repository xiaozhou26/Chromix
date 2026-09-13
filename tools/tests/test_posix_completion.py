"""Exercise stage completion against small ZIP fixtures, not Chromium builds."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("timeout") and shutil.which("unzip"), "GNU timeout and unzip required")
class PosixCompletionTest(unittest.TestCase):
    def complete_fixture(self, platform, corrupt, *, arch="x64", host_arch="x64",
                         verifier_failure=False, unsafe_member=None, smoke_failure=None):
        temp = tempfile.TemporaryDirectory(prefix="chromix completion ")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        repo = root / "repo"
        work = root / "work"
        out = work / "src/out/Chromix"
        out.mkdir(parents=True)
        (work / "src/.chromix-source-ready").touch()
        (repo / "build/posix").mkdir(parents=True)
        shutil.copy2(REPO / "build/posix/ci-stage.sh", repo / "build/posix/ci-stage.sh")
        if platform == "macos":
            (repo / "tools").mkdir()
            shutil.copy2(REPO / "tools/macos_browser_smoke.py", repo / "tools/macos_browser_smoke.py")
        extractor = repo / "sdk/python/chromix/_binary.py"
        extractor.parent.mkdir(parents=True)
        shutil.copy2(REPO / "sdk/python/chromix/_binary.py", extractor)
        version = (REPO / "CHROMIUM_VERSION").read_text().strip()
        (repo / "CHROMIUM_VERSION").write_text(version + "\n")
        if platform == "linux":
            (out / "chrome").touch()
            (out / "chrome").chmod(0o755)
            build = repo / "build/build.sh"
            package = repo / "build/linux/package-linux.sh"
            asset = f"chromix-linux-{arch}.zip"
        else:
            (out / "Chromium.app").mkdir()
            build = repo / "build/macos/build.sh"
            package = repo / "build/macos/package-macos.sh"
            asset = "chromix-mac-x64.zip"
        build.parent.mkdir(parents=True, exist_ok=True)
        build.write_text("#!/bin/sh\nexit 0\n")
        build.chmod(0o755)
        seed = root / "seed"
        seed.mkdir()
        launch_log = root / "launcher-log"
        marker = "<p>missing</p>" if smoke_failure == "marker" else "<p>chromix-smoke-ok</p>"
        exit_code = 7 if smoke_failure == "nonzero" else 0
        launcher = ('#!/bin/sh\nprintf "%s\\n" "$*" >> "$LAUNCH_TEST_LOG"\n'
                    f'case "$1" in --version) printf "Chromix {version}\\n";; '
                    f'*) printf "{marker}\\n"; printf "diagnostic\\n" >&2; exit {exit_code};; esac\n')
        info = zipfile.ZipInfo("chromix/chromix")
        info.create_system = 3
        info.external_attr = 0o100755 << 16
        with zipfile.ZipFile(seed / asset, "w") as archive:
            archive.writestr(info, launcher)
            if unsafe_member:
                archive.writestr(unsafe_member, "rejected")
        checksum = hashlib.sha256((seed / asset).read_bytes()).hexdigest()
        if corrupt:
            checksum = "0" * 64
        (seed / "SHA256SUMS").write_text(f"{checksum}  {asset}\n")
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_text('#!/bin/sh\ncp "$TEST_SEED"/* "$2/"\n')
        package.chmod(0o755)
        if platform == "linux":
            (repo / "build/linux/prepare-ci-sandbox.sh").write_text(
                '#!/bin/sh\nprintf "sandbox-preflight\\n" >> "$LAUNCH_TEST_LOG"\n')
        github_output = root / "github_output"
        env = {**os.environ, "TEST_SEED": str(seed), "LAUNCH_TEST_LOG": str(launch_log),
               "GITHUB_OUTPUT": str(github_output)}
        bindir = root / "bin"
        bindir.mkdir()
        machine = {"x64": "x86_64", "arm64": "aarch64"}[host_arch]
        (bindir / "uname").write_text(f'#!/bin/sh\nprintf "{machine}\\n"\n')
        (bindir / "uname").chmod(0o755)
        env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
        cross = platform == "linux" and arch != host_arch
        if cross:
            tools = repo / "tools"
            tools.mkdir()
            (tools / "verify_linux_bundle.py").write_text(
                'import pathlib,sys\npathlib.Path(sys.argv[2]).joinpath("static-verified").touch()\n'
                f'sys.exit({int(verifier_failure)})\n')
        result = subprocess.run(
            ["bash", str(repo / "build/posix/ci-stage.sh"), "--platform", platform,
             "--arch", arch, "--workdir", str(work)],
            env=env, capture_output=True, text=True, timeout=20)
        output = dict(line.split("=", 1) for line in github_output.read_text().splitlines())
        if corrupt:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bundle checksum verification failed", result.stderr)
            self.assertEqual(output["finished"], "false")
            self.assertFalse(launch_log.exists())
            self.assertFalse((work / "smoke").exists())
        elif unsafe_member:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsafe ZIP path", result.stderr)
            self.assertEqual(output["finished"], "false")
            self.assertFalse(launch_log.exists())
            self.assertFalse((work / "smoke/chromix/chromix").exists())
        elif verifier_failure:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cross-built bundle architecture verification failed", result.stderr)
            self.assertEqual(output["finished"], "false")
            self.assertFalse(launch_log.exists())
        elif smoke_failure:
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output["finished"], "false")
            self.assertEqual(output["runtime_verified"], "false")
            self.assertEqual(output["package_ready"], "true")
            self.assertNotIn("upload_snapshot", output)
            self.assertEqual(output["runtime_failed"], "true")
            self.assertTrue((work / "dist" / asset).is_file())
            evidence = work / "runtime-smoke-stage-1"
            self.assertTrue((evidence / "report.json").is_file())
            self.assertIn(marker, (evidence / "headless.stdout.log").read_text())
            self.assertIn("diagnostic", (evidence / "headless.stderr.log").read_text())
            self.assertFalse((work / "smoke").exists())
        elif cross:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(output["finished"], "true")
            self.assertEqual(output["runtime_verified"], "false")
            self.assertEqual(output["status"], "compiled")
            self.assertFalse(launch_log.exists())
            self.assertFalse((work / "smoke").exists())
        else:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(output["finished"], "true")
            self.assertEqual(output["runtime_verified"], "true")
            self.assertEqual(output["status"], "completed")
            calls = launch_log.read_text()
            if platform == "linux":
                self.assertLess(calls.index("sandbox-preflight"), calls.index("--version"))
            self.assertIn("--version", calls)
            self.assertIn("--headless", calls)
            self.assertFalse((work / "smoke").exists())

    def test_cross_built_arm64_bundle_defers_runtime_but_requires_static_verification(self):
        self.complete_fixture("linux", False, arch="arm64")

    def test_cross_built_arm64_wrong_architecture_does_not_finish(self):
        self.complete_fixture("linux", False, arch="arm64", verifier_failure=True)

    def test_cross_built_arm64_checksum_failure_stops_before_static_verification(self):
        self.complete_fixture("linux", True, arch="arm64")

    def test_linux_valid_bundle_runs_both_smoke_checks(self):
        self.complete_fixture("linux", False)

    def test_native_arm64_bundle_runs_both_smoke_checks(self):
        self.complete_fixture("linux", False, arch="arm64", host_arch="arm64")

    def test_unsafe_zip_member_stops_before_extraction_or_launch(self):
        for arch in ("x64", "arm64"):
            for member in ("chromix/../ignored-member", "ignored-member", "/absolute"):
                with self.subTest(arch=arch, member=member):
                    self.complete_fixture("linux", False, arch=arch, unsafe_member=member)

    def test_linux_corrupted_bundle_never_launches(self):
        self.complete_fixture("linux", True)

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_macos_valid_bundle_runs_both_smoke_checks(self):
        self.complete_fixture("macos", False)

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_macos_failure_keeps_diagnostic_bundle_and_checkpoint(self):
        for failure in ("nonzero", "marker"):
            with self.subTest(failure=failure):
                self.complete_fixture("macos", False, smoke_failure=failure)

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_macos_corrupted_bundle_never_launches(self):
        self.complete_fixture("macos", True)


@unittest.skipUnless(all(shutil.which(tool) for tool in ("bash", "tar", "zstd")),
                     "bash, tar, and zstd required")
class PosixRuntimeCheckpointTest(unittest.TestCase):
    def test_failed_smoke_checkpoint_restores_without_stale_evidence(self):
        with tempfile.TemporaryDirectory(prefix="chromix runtime checkpoint ") as temp:
            root = Path(temp)
            work = root / "work"
            nested = Path("src/runtime-smoke-stage-1/keep")
            (work / nested).parent.mkdir(parents=True)
            (work / nested).write_text("required source payload\n")
            version = (REPO / "CHROMIUM_VERSION").read_text().strip()
            launcher = work / "src/launcher"
            launcher.write_text(
                '#!/bin/sh\n'
                f'case "$1" in --version) printf "Chromix {version}\\n";;\n'
                '*) if [ "$SMOKE_FIXTURE_EXIT" -ne 0 ]; then\n'
                '     printf "first smoke failed\\n" >&2\n'
                '     exit "$SMOKE_FIXTURE_EXIT"\n'
                '   fi\n'
                '   printf "<p>chromix-smoke-ok</p>\\n";; esac\n')
            launcher.chmod(0o755)
            env = {**os.environ, "SMOKE_FIXTURE_EXIT": "7",
                   "CHROMIX_SNAPSHOT_VOLUME_BYTES": str(64 * 1024),
                   "CHROMIX_SNAPSHOT_MAX_SLOTS": "4", "CHROMIX_SNAPSHOT_MAX_VOLUMES": "8"}
            evidence_name = "runtime-smoke-stage-1"

            def smoke(directory, exit_code):
                return subprocess.run(
                    [sys.executable, "-B", str(REPO / "tools/macos_browser_smoke.py"),
                     "--launcher", str(directory / "src/launcher"),
                     "--output", str(directory / evidence_name),
                     "--profile", str(directory / "smoke/profile"),
                     "--chromium-version", version],
                    env={**env, "SMOKE_FIXTURE_EXIT": str(exit_code)},
                    capture_output=True, text=True, timeout=15)

            failed = smoke(work, 7)
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
            report_path = work / evidence_name / "report.json"
            failed_report = report_path.read_bytes()
            report = json.loads(failed_report)
            self.assertFalse(report["passed"])
            self.assertEqual(report["version"]["status"], "passed")
            self.assertEqual(report["headless"]["returncode"], 7)
            self.assertFalse((work / "smoke/profile").exists())

            parts = work / ".snapshot-stage-1"
            packed = subprocess.run(
                ["bash", str(REPO / "build/posix/ci-parts.sh"), str(work), str(parts)],
                env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(packed.returncode, 0, packed.stdout + packed.stderr)
            self.assertTrue((parts / "p1/tree.tar.zst.001").is_file())
            restored = root / "restored"
            unpacked = subprocess.run(
                ["bash", str(REPO / "build/posix/restore-snapshot.sh"), str(parts), str(restored)],
                env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(unpacked.returncode, 0, unpacked.stdout + unpacked.stderr)
            fresh_evidence = restored / evidence_name
            self.assertFalse((fresh_evidence / "report.json").exists())
            self.assertFalse(fresh_evidence.exists())
            self.assertFalse((restored / "smoke").exists())
            self.assertEqual((restored / nested).read_text(), "required source payload\n")
            self.assertEqual((restored / "src/launcher").read_bytes(), launcher.read_bytes())

            passed = smoke(restored, 0)
            self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
            report = json.loads((fresh_evidence / "report.json").read_bytes())
            self.assertTrue(report["passed"])
            self.assertEqual(report["version"]["status"], "passed")
            self.assertEqual(report["headless"]["status"], "passed")
            self.assertEqual(report["headless"]["returncode"], 0)
            self.assertEqual(Path(report["headless"]["stdout_path"]), fresh_evidence / "headless.stdout.log")
            self.assertIn("<p>chromix-smoke-ok</p>", (fresh_evidence / "headless.stdout.log").read_text())
            self.assertEqual((fresh_evidence / "headless.stderr.log").read_text(), "")
            self.assertFalse((restored / "smoke/profile").exists())
            self.assertEqual(report_path.read_bytes(), failed_report)


if __name__ == "__main__":
    unittest.main()
