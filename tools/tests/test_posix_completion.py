"""Exercise stage completion against small ZIP fixtures, not Chromium builds."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("timeout") and shutil.which("unzip"), "GNU timeout and unzip required")
class PosixCompletionTest(unittest.TestCase):
    def write_runtime_tools(self, repo, bindir):
        python_shim = textwrap.dedent('''\
            import json
            import os
            from pathlib import Path
            import sys

            root = Path(__file__).resolve().parents[1]
            repo, work = root / "repo", root / "work"
            args = sys.argv[1:]
            browser = work / "smoke" / os.environ["TEST_BROWSER_RELATIVE"]
            failure = os.environ["TEST_GATE_FAILURE"]
            if args[:2] == ["-m", "pip"]:
                kind = "pip"
                assert args == ["-m", "pip", "install", "--disable-pip-version-check",
                                "--timeout", "30", "--retries", "1", "-r",
                                str(repo / "tools/fingerprint-requirements.txt")], args
                assert Path(args[-1]).is_file()
            elif args[:1] == ["-c"]:
                kind = "hash"
                assert len(args) == 3 and args[2] == str(browser), args
                assert "hashlib" in args[1] and "sha256" in args[1], args
            elif args[:1] == [str(repo / "tools/fingerprint_acceptance.py")]:
                kind = "fingerprint"
            elif args[:1] == [str(repo / "tools/macos_browser_smoke.py")]:
                kind = "smoke"
            elif args == ["-", str(repo), str(work / "dist" / os.environ["TEST_ASSET"]),
                          str(work / "smoke")]:
                kind = "extract"
            elif args[:1] == [str(repo / "tools/verify_linux_bundle.py")]:
                kind = "static"
            else:
                raise SystemExit("unexpected Python invocation: " + repr(args))
            with (root / "runtime-calls.jsonl").open("a") as stream:
                stream.write(json.dumps({"kind": kind, "args": args,
                    "outputs": (root / "github_output").read_text().splitlines()}) + "\\n")
            if kind == "pip":
                print("fixture pip install: offline", flush=True)
                if failure == "dependencies":
                    print("fixture dependencies unavailable", file=sys.stderr)
                    sys.exit(23)
                sys.exit(0)
            if kind == "hash" and failure == "hash":
                print("fixture browser hash unavailable", file=sys.stderr)
                sys.exit(19)
            os.execv(sys.executable, [sys.executable, "-B", *args])
            ''')
        timeout_shim = textwrap.dedent('''\
            import json
            import os
            from pathlib import Path
            import sys

            root = Path(__file__).resolve().parents[1]
            args = sys.argv[1:]
            with (root / "runtime-calls.jsonl").open("a") as stream:
                stream.write(json.dumps({"kind": "timeout", "args": args}) + "\\n")
            command = list(args)
            while command[0] in ("-k", "-s"):
                del command[:2]
            del command[0]
            gate = str(root / "repo/tools/fingerprint_acceptance.py") in command
            duration = "2s" if gate and os.environ["TEST_GATE_FAILURE"] == "timeout" else "5s"
            real_timeout = os.environ["TEST_REAL_TIMEOUT"]
            os.execv(real_timeout, [real_timeout, "-k", "1s", duration, *command])
            ''')
        for name, source in (("python3", python_shim), ("timeout", timeout_shim)):
            path = bindir / name
            path.write_text(f"#!{sys.executable}\n" + source)
            path.chmod(0o755)
        tools = repo / "tools"
        tools.mkdir(exist_ok=True)
        shutil.copy2(REPO / "tools/fingerprint-requirements.txt",
                     tools / "fingerprint-requirements.txt")
        (tools / "fingerprint_acceptance.py").write_text(textwrap.dedent('''\
            import argparse
            import hashlib
            import json
            import os
            from pathlib import Path
            import re
            import subprocess
            import sys
            import time

            root = Path(__file__).resolve().parents[2]
            repo, work = root / "repo", root / "work"
            parser = argparse.ArgumentParser(allow_abbrev=False)
            for name in ("browser", "expected-sha256", "expected-version", "source-report",
                         "source-root", "output-dir"):
                parser.add_argument("--" + name, required=True)
            args = parser.parse_args()
            browser = Path(args.browser)
            assert browser == work / "smoke" / os.environ["TEST_BROWSER_RELATIVE"]
            assert browser.is_file() and os.access(browser, os.X_OK)
            digest = hashlib.sha256(browser.read_bytes()).hexdigest()
            assert args.expected_sha256 == digest
            version = (repo / "CHROMIUM_VERSION").read_text().strip()
            assert args.expected_version == version
            identified = subprocess.run([str(browser), "--version"], check=True,
                                        capture_output=True, text=True, timeout=1)
            assert identified.stdout.strip() == "Chromix " + version
            assert Path(args.source_root) == work / "src"
            assert Path(args.source_root).joinpath(".chromix-source-ready").is_file()
            assert Path(args.source_report) == work / "fingerprint-diagnostics/source-final.json"
            assert json.loads(Path(args.source_report).read_text()) == {
                "fixture": "verified", "source_root": str(work / "src")}
            output = Path(args.output_dir)
            assert output.parent == work / "fingerprint-diagnostics"
            assert re.fullmatch(r"runtime-s1-[0-9]+-[0-9]+", output.name)
            output.mkdir(exist_ok=False)
            failure = os.environ["TEST_GATE_FAILURE"]
            status = {"nonzero": "failed", "timeout": "running"}.get(failure, "passed")
            receipt = {**vars(args), "browser_sha256": digest, "browser_version": version,
                       "status": status, "ci_gate_passed": not failure}
            (output / "acceptance.json").write_text(json.dumps(receipt) + "\\n")
            print("fixture fingerprint: inputs verified", flush=True)
            if failure == "timeout":
                print("fixture fingerprint: waiting for timeout", flush=True)
                time.sleep(60)
                raise SystemExit("fixture timeout did not terminate the gate")
            if failure == "nonzero":
                print("fixture fingerprint: rejected", file=sys.stderr)
                sys.exit(17)
            print("fixture fingerprint: passed", flush=True)
            '''))

    def complete_fixture(self, platform, corrupt, *, arch="x64", host_arch="x64",
                         verifier_failure=False, unsafe_member=None, smoke_failure=None,
                         gate_failure=None, launcher_failure=None):
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
        source_report = work / "fingerprint-diagnostics/source-final.json"
        source_report.parent.mkdir()
        source_receipt = {"fixture": "verified", "source_root": str(work / "src")}
        source_report.write_text(json.dumps(source_receipt) + "\n")
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
            asset = f"chromix-mac-{arch}.zip"
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
        native_relative = ("chromix/chrome" if platform == "linux" else
                           "chromix/Chromium.app/Contents/MacOS/Chromium")
        native = (f'#!/bin/sh\n# {platform} {arch} native browser fixture\n'
                  f'case "$1" in --version) printf "Chromix {version}\\n";; *) exit 31;; esac\n')
        native_hash = hashlib.sha256(native.encode()).hexdigest()
        with zipfile.ZipFile(seed / asset, "w") as archive:
            for name, payload in (("chromix/chromix", launcher), (native_relative, native)):
                if name == "chromix/chromix" and launcher_failure == "missing":
                    continue
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (0o100644 if name == "chromix/chromix" and
                                      launcher_failure == "not_executable" else 0o100755) << 16
                archive.writestr(info, payload)
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
        env = {key: value for key, value in os.environ.items() if not key.startswith("CHROMIX_")}
        env.update({"TEST_SEED": str(seed), "LAUNCH_TEST_LOG": str(launch_log),
                    "GITHUB_OUTPUT": str(github_output), "TEST_GATE_FAILURE": gate_failure or "",
                    "TEST_REAL_TIMEOUT": shutil.which("timeout"), "TEST_ASSET": asset,
                    "TEST_BROWSER_RELATIVE": native_relative, "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONOPTIMIZE": "0", "PIP_NO_INDEX": "1", "PIP_CONFIG_FILE": os.devnull})
        bindir = root / "bin"
        bindir.mkdir()
        self.write_runtime_tools(repo, bindir)
        machine = {"x64": "x86_64", "arm64": "arm64" if platform == "macos" else "aarch64"}[host_arch]
        (bindir / "uname").write_text(f'#!/bin/sh\nprintf "{machine}\\n"\n')
        (bindir / "uname").chmod(0o755)
        env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
        cross = platform == "linux" and arch != host_arch
        if cross:
            tools = repo / "tools"
            (tools / "verify_linux_bundle.py").write_text(
                'import pathlib,sys\npathlib.Path(sys.argv[2]).joinpath("static-verified").touch()\n'
                f'sys.exit({int(verifier_failure)})\n')
        result = subprocess.run(
            ["bash", str(repo / "build/posix/ci-stage.sh"), "--platform", platform,
             "--arch", arch, "--workdir", str(work)],
            env=env, capture_output=True, text=True, timeout=20)
        output_lines = github_output.read_text().splitlines()
        output = dict(line.split("=", 1) for line in output_lines)
        diagnostic = result.stdout + result.stderr
        runtime_calls = [json.loads(line) for line in
                         (root / "runtime-calls.jsonl").read_text().splitlines()]
        expected_calls = ["timeout"]
        if not corrupt:
            expected_calls.append("extract")
            if not (unsafe_member or launcher_failure):
                if cross:
                    expected_calls.append("static")
                else:
                    expected_calls.extend(["smoke"] if platform == "macos" else ["timeout", "timeout"])
                    if not smoke_failure:
                        expected_calls.extend(["timeout", "pip"])
                        if gate_failure != "dependencies":
                            expected_calls.append("hash")
                            if gate_failure != "hash":
                                expected_calls.extend(["timeout", "fingerprint"])
        self.assertEqual([call["kind"] for call in runtime_calls], expected_calls, diagnostic)
        for call in runtime_calls:
            self.assertNotIn("finished=true", call.get("outputs", []))
            self.assertNotIn("runtime_verified=true", call.get("outputs", []))
        self.assertEqual(runtime_calls[0]["args"][:5], ["-k", "7m", "-s", "SIGTERM", "255m"])
        self.assertNotIn("upload_snapshot", output)
        self.assertEqual((work / "dist" / asset).read_bytes(), (seed / asset).read_bytes())
        self.assertEqual((work / "dist/SHA256SUMS").read_bytes(), (seed / "SHA256SUMS").read_bytes())
        self.assertEqual(json.loads(source_report.read_text()), source_receipt)
        self.assertTrue((work / "src/.chromix-source-ready").is_file())
        self.assertTrue((out / ("chrome" if platform == "linux" else "Chromium.app")).exists())
        self.assertFalse(list(work.glob(".snapshot-stage-*")))

        pip_calls = [call for call in runtime_calls if call["kind"] == "pip"]
        timeouts = [call["args"] for call in runtime_calls if call["kind"] == "timeout"]
        if pip_calls:
            self.assertIn(["-k", "15s", "300s", "python3", *pip_calls[0]["args"]], timeouts)
            self.assertIn("fixture pip install: offline", result.stdout)
        else:
            self.assertNotIn("fixture pip install", diagnostic)
        gate_calls = [call for call in runtime_calls if call["kind"] == "fingerprint"]
        receipts = list((work / "fingerprint-diagnostics").glob("runtime-*/acceptance.json"))
        if gate_calls:
            self.assertIn(["-k", "30s", "2100s", "python3", *gate_calls[0]["args"]], timeouts)
            self.assertEqual(len(receipts), 1, diagnostic)
            receipt_path = receipts[0]
            self.assertRegex(receipt_path.parent.name, r"^runtime-s1-[0-9]+-[0-9]+$")
            self.assertEqual(json.loads(receipt_path.read_text()), {
                "browser": str(work / "smoke" / native_relative),
                "expected_sha256": native_hash, "expected_version": version,
                "source_root": str(work / "src"), "source_report": str(source_report),
                "output_dir": str(receipt_path.parent), "browser_sha256": native_hash,
                "browser_version": version, "ci_gate_passed": gate_failure is None,
                "status": {"nonzero": "failed", "timeout": "running"}.get(gate_failure, "passed"),
            })
            self.assertIn("fixture fingerprint: inputs verified", result.stdout)
            if gate_failure:
                self.assertNotIn("fixture fingerprint: passed", diagnostic)
            else:
                self.assertIn("fixture fingerprint: passed", result.stdout)
        else:
            self.assertEqual(receipts, [])
            self.assertNotIn("fixture fingerprint:", diagnostic)

        if platform == "macos" and not (corrupt or unsafe_member or launcher_failure):
            evidence = work / "runtime-smoke-stage-1"
            report = json.loads((evidence / "report.json").read_text())
            self.assertEqual(report["passed"], smoke_failure is None)
            self.assertEqual(report["version"]["status"], "passed")
            self.assertEqual(report["headless"]["status"],
                             {"nonzero": "nonzero_exit", "marker": "marker_missing"}.get(
                                 smoke_failure, "passed"))
            self.assertEqual(report["headless"]["returncode"], exit_code)
            self.assertTrue(report["profile_cleaned"])
            self.assertEqual(report["profile"], str(work / "smoke/profile"))
            self.assertEqual((evidence / "headless.stdout.log").read_text(), marker + "\n")
            self.assertEqual((evidence / "headless.stderr.log").read_text(), "diagnostic\n")
            self.assertIn(json.dumps(report, sort_keys=True), result.stdout)
            self.assertFalse((work / "smoke").exists())
            if gate_calls:
                self.assertEqual(gate_calls[0]["outputs"], [
                    "status=running", "finished=false", "package_ready=true", "runtime_verified=false"])
        else:
            self.assertFalse((work / "runtime-smoke-stage-1").exists())

        if corrupt:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bundle checksum verification failed", result.stderr)
            self.assertEqual(output["finished"], "false")
            self.assertFalse(launch_log.exists())
            self.assertFalse((work / "smoke").exists())
        elif unsafe_member or launcher_failure:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsafe ZIP path" if unsafe_member else "extracted bundle launcher is missing",
                          result.stderr)
            self.assertEqual(output["finished"], "false")
            self.assertFalse(launch_log.exists())
            self.assertFalse((work / "smoke/chromix/chromix").exists())
            if platform == "macos":
                self.assertEqual(output_lines, ["status=running", "finished=false", "package_ready=true",
                                                "runtime_verified=false", "runtime_failed=true"])
                self.assertFalse((work / "smoke").exists())
                self.assertNotIn("fingerprint regression gate", diagnostic)
        elif verifier_failure:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cross-built bundle architecture verification failed", result.stderr)
            self.assertEqual(output["finished"], "false")
            self.assertFalse(launch_log.exists())
            self.assertTrue((work / "smoke/chromix/static-verified").is_file())
        elif smoke_failure or gate_failure:
            self.assertEqual(result.returncode, 1, diagnostic)
            self.assertEqual(output_lines, ["status=running", "finished=false", "package_ready=true",
                                            "runtime_verified=false", "runtime_failed=true"])
            if smoke_failure:
                code, message = 1, "extracted macOS runtime smoke test failed; see runtime-smoke-stage-1"
            else:
                code, message = {
                    "nonzero": (17, "fingerprint regression gate failed; see the separate diagnostic artifact"),
                    "timeout": (124, "fingerprint regression gate failed; see the separate diagnostic artifact"),
                    "dependencies": (23, "fingerprint audit dependencies unavailable"),
                    "hash": (19, "fingerprint browser hash failed"),
                }[gate_failure]
                stream, marker = {
                    "nonzero": (result.stderr, "fixture fingerprint: rejected"),
                    "timeout": (result.stdout, "fixture fingerprint: waiting for timeout"),
                    "dependencies": (result.stderr, "fixture dependencies unavailable"),
                    "hash": (result.stderr, "fixture browser hash unavailable"),
                }[gate_failure]
                self.assertIn(marker, stream)
            self.assertEqual(result.stderr.splitlines()[-1], f"==> ERROR: {message} (exit {code})")
            self.assertNotIn("snapshotting unfinished work", diagnostic)
            self.assertFalse((work / "smoke").exists())
        elif cross:
            self.assertEqual(result.returncode, 0, diagnostic)
            self.assertEqual(output["finished"], "true")
            self.assertEqual(output["runtime_verified"], "false")
            self.assertEqual(output["status"], "compiled")
            self.assertNotIn("runtime_failed", output)
            self.assertFalse(launch_log.exists())
            self.assertFalse((work / "smoke").exists())
        else:
            self.assertEqual(result.returncode, 0, diagnostic)
            self.assertEqual(output["finished"], "true")
            self.assertEqual(output["runtime_verified"], "true")
            self.assertEqual(output["status"], "completed")
            self.assertNotIn("runtime_failed", output)
            if platform == "macos":
                self.assertEqual(output["package_ready"], "true")
            self.assertFalse((work / "smoke").exists())
        if not (corrupt or unsafe_member or launcher_failure or verifier_failure or cross):
            calls = launch_log.read_text()
            if platform == "linux":
                self.assertLess(calls.index("sandbox-preflight"), calls.index("--version"))
            self.assertIn("--version", calls)
            self.assertIn("--headless", calls)
        if corrupt or verifier_failure or (unsafe_member or launcher_failure) and platform == "linux":
            self.assertEqual(output_lines, ["status=running", "finished=false"])

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
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                self.complete_fixture("macos", False, arch=arch, host_arch=arch)

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_macos_failure_keeps_diagnostic_bundle_and_checkpoint(self):
        for arch in ("x64", "arm64"):
            for failure in ("nonzero", "marker"):
                with self.subTest(arch=arch, failure=failure):
                    self.complete_fixture("macos", False, arch=arch, host_arch=arch,
                                          smoke_failure=failure)

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_macos_fingerprint_nonzero_preserves_bundle_and_fails_runtime(self):
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                self.complete_fixture("macos", False, arch=arch, host_arch=arch,
                                      gate_failure="nonzero")

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_macos_fingerprint_timeout_fails_runtime_instead_of_handoff(self):
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                self.complete_fixture("macos", False, arch=arch, host_arch=arch,
                                      gate_failure="timeout")

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_macos_dependency_failure_stops_before_hash_and_fingerprint(self):
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                self.complete_fixture("macos", False, arch=arch, host_arch=arch,
                                      gate_failure="dependencies")

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_macos_browser_hash_failure_stops_before_fingerprint(self):
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                self.complete_fixture("macos", False, arch=arch, host_arch=arch,
                                      gate_failure="hash")

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_macos_invalid_extraction_or_launcher_preserves_checked_bundle(self):
        for arch in ("x64", "arm64"):
            for failure in ("unsafe", "missing", "not_executable"):
                with self.subTest(arch=arch, failure=failure):
                    self.complete_fixture(
                        "macos", False, arch=arch, host_arch=arch,
                        unsafe_member="chromix/../rejected" if failure == "unsafe" else None,
                        launcher_failure=None if failure == "unsafe" else failure)

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_macos_corrupted_bundle_never_launches(self):
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                self.complete_fixture("macos", True, arch=arch, host_arch=arch)


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
